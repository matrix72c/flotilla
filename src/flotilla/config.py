"""Deployment configuration, explicit policy validation, and runtime settings."""

from __future__ import annotations

import importlib
import ipaddress
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from flotilla.backends import BUILTIN, Backend, BackendSettings, load_backend
from flotilla.capabilities import ExternalTarget, IsolationEvidence, ProbeSettings
from flotilla.core.anchor import EXPIRY_MARGIN_S, AnchorSettings
from flotilla.core.reaper import ReaperSettings
from flotilla.core.runtime import MonitorSettings
from flotilla.core.trial import TrialSettings
from flotilla.platform.base import (
    DEVICES,
    WORK_ROOT,
    Capabilities,
    Clock,
    ExternalPolicy,
    InstanceProfile,
    Platform,
    Resources,
    SharedVolume,
    check_root_name,
)
from flotilla.platform.network import cluster_dns_address
from flotilla.platform.opensandbox import ExecdSettings, NetworkSettings, StorageSettings
from flotilla.platform.opensandbox import build as build_opensandbox
from flotilla.platform.opensandbox.filesystem import FilesystemProtocol, FilesystemSettings
from flotilla.resources import cpu, memory
from flotilla.share.release import BUILTIN_RELEASE, BUILTIN_RELEASE_SHA256

if TYPE_CHECKING:
    # The module, not the package: finding a dataset must not pull the Harbor adapters in.
    from flotilla.native.dataset import Dataset

#: Anchor renewal retry interval (`AnchorSettings.retry_interval_s`); the renew interval cannot be shorter.
ANCHOR_RETRY_S = AnchorSettings(image="x").retry_interval_s
PositiveFloat = Annotated[float, Field(gt=0)]
PositiveInt = Annotated[int, Field(gt=0)]
_RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ConfigError(ValueError):
    """Config does not match the deployment; provider `start` refuses. Lists every problem, not just the first."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("configuration validation failed:\n- " + "\n- ".join(problems))
        self.problems = problems


class _Model(BaseModel):
    # hide_input_in_errors: validation errors do not echo input, so a misplaced credential (e.g. unknown field
    # credential = "ak:sk") never reaches terminals or logs. Errors still carry the field path and reason.
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, allow_inf_nan=False)


class OpenSandboxNetwork(_Model):
    link: Literal["cidr", "sandbox"] = "cidr"
    ingress: Literal["deployment", "sandbox"] = "deployment"
    sandbox_cidrs: tuple[str, ...] = ()
    platform_cidrs: tuple[str, ...] = ()
    max_rules: PositiveInt = 4096

    @field_validator("sandbox_cidrs", "platform_cidrs")
    @classmethod
    def _cidrs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for cidr in value:
            ipaddress.ip_network(cidr, strict=False)
        return value


class OpenSandboxExecd(_Model):
    protocol: Literal["current", "legacy"] = "current"
    wrapper: str = ExecdSettings().wrapper


class OpenSandbox(BackendSettings):
    """`[opensandbox]`: the built-in backend's table."""

    extensions: dict[str, Any] = Field(default_factory=dict)
    privileged_extensions: dict[str, Any] = Field(default_factory=dict)
    management_extensions: dict[str, Any] = Field(default_factory=dict)
    network: OpenSandboxNetwork = OpenSandboxNetwork()
    execd: OpenSandboxExecd = OpenSandboxExecd()
    filesystem_protocol: FilesystemProtocol = "upstream"
    create_fields: dict[str, Any] = Field(default_factory=dict)
    group_extension: str | None = (
        None  # `extensions` key for the placement group; unset if the deployment lacks group scheduling
    )
    # Connections per HTTP pool (control plane, internal commands, user commands). Requests beyond it wait in
    # flotilla before their deadlines start.
    max_connections: PositiveInt = 100


class Storage(_Model):
    volumes: Literal["host", "pvc"] = "host"
    host_path: str = ""
    claim_name: str = ""
    root_subpath: str = ""

    @model_validator(mode="after")
    def _settings(self) -> Storage:
        self.settings()  # raises ValueError
        return self

    def settings(self) -> StorageSettings:
        return StorageSettings(
            volumes=self.volumes, host_path=self.host_path, claim_name=self.claim_name, root_subpath=self.root_subpath
        )


class Registry(_Model):
    """Read-only registry access, for the image configs the platform does not read itself.

    Credentials are not here: a deployment file goes into version control with a recipe, and a
    registry password must not travel with it. Each host names an environment variable holding
    `username:password`, read when a client is constructed.
    """

    #: Registry host -> name of the environment variable holding `username:password` for it.
    credential_envs: dict[str, str] = Field(default_factory=dict)
    #: Where to keep image configs between runs. A digest names one config forever, so entries
    #: never expire; without a path they live in memory for the life of the client only.
    config_cache: Path | None = None

    def credentials(self) -> dict[str, tuple[str, str]]:
        """Each host's credential, read from its environment variable; an unset one is skipped."""
        out: dict[str, tuple[str, str]] = {}
        for host, variable in self.credential_envs.items():
            value = os.environ.get(variable)
            if not value:
                continue
            user, separator, password = value.partition(":")
            if not separator or not user:
                raise ConfigError([f"{variable} must hold `username:password` for registry {host}"])
            out[host] = (user, password)
        return out


class Share(_Model):
    """Shared tools mounted at /.flotilla. Omit `[share]` to use the built-in BusyBox release."""

    release: str = BUILTIN_RELEASE
    release_sha256: str = BUILTIN_RELEASE_SHA256

    @property
    def builtin(self) -> bool:
        """Whether this is the built-in release, which runtime and probe start publish when missing."""
        return (self.release, self.release_sha256) == (BUILTIN_RELEASE, BUILTIN_RELEASE_SHA256)

    @field_validator("release")
    @classmethod
    def _release(cls, value: str) -> str:
        if not _RELEASE.fullmatch(value):
            raise ValueError(f"share.release must be a single path segment (alphanumerics and ._-): {value!r}")
        return value

    @field_validator("release_sha256")
    @classmethod
    def _sha(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("share.release_sha256 must be 64 lowercase hex characters")
        return value


class Network(_Model):
    """`external`: `"any"` or a narrow list (IP / CIDR / domain), compiled to `allowlist`; an empty list is `none`."""

    external: Literal["any"] | tuple[str, ...] = ()

    @field_validator("external")
    @classmethod
    def _narrow(cls, value: str | tuple[str, ...]) -> str | tuple[str, ...]:
        if isinstance(value, str):
            return value
        for target in value:
            if target.lower() == "any":
                raise ValueError('unrestricted egress is written external = "any" (a string), not a list item')
            net = _as_network(target)
            if net is not None and net.prefixlen == 0:
                raise ValueError(f'narrow list contains unrestricted target {target}: use external = "any" instead')
            if net is None and not _is_hostname(target):
                raise ValueError(f"external targets must be IP / CIDR or domain names: {target!r}")
        return value

    def policy(self) -> ExternalPolicy:
        if self.external == "any":
            return ExternalPolicy(mode="any")
        if not self.external:
            return ExternalPolicy(mode="none")
        cidrs = tuple(t for t in self.external if _as_network(t) is not None)
        hosts = tuple(t for t in self.external if _as_network(t) is None)
        return ExternalPolicy(mode="allowlist", hosts=hosts, cidrs=cidrs)


class Security(_Model):
    accept_insecure: frozenset[Literal["inbound_isolation", "exec_auth", "no_auto_mount"]] = frozenset()


class AgentOverlay(_Model):
    env: dict[str, str] = {}
    services: tuple[str, ...] | None = None


class Limits(_Model):
    create_rate: PositiveFloat = 5.0
    create_concurrency: PositiveInt = 64
    trial_concurrency: PositiveInt = 512
    instance_budget: PositiveInt = 4096
    reap_concurrency: PositiveInt = 16
    exec_rate: PositiveFloat = 500.0
    exec_timeout: PositiveFloat = 21600.0
    exec_concurrency: PositiveInt = 64
    anchor_concurrency: PositiveInt = 16
    admission_timeout: PositiveFloat = 3600.0  # default wait for a trial_concurrency/instance_budget slot


class Runtime(_Model):
    startup_timeout_s: PositiveFloat = 1800.0  # default open budget, from admission to ready
    check_timeout_s: PositiveFloat = 60.0
    run_timeout_s: PositiveFloat = 21600.0
    exec_default_timeout_s: PositiveFloat = 300.0
    cancel_confirm_timeout_s: PositiveFloat = 15.0
    exec_output_limit_bytes: PositiveInt = 8 * 1024 * 1024


class Timeouts(_Model):
    create: PositiveFloat = 600.0
    # One create request. A response lost in transit is recovered by listing labels, which only starts after this.
    create_request: PositiveFloat = 60.0
    # One sandbox staying pending; it is deleted and created again once. Some platforms never start a few
    # sandboxes of a burst, and waiting would hold their quota for the whole `create` budget. The default is
    # off when it is not below `create`.
    pending: PositiveFloat | None = 300.0
    address: PositiveFloat = 30.0
    wire: PositiveFloat = 60.0
    hosts: PositiveFloat = 30.0
    prepare: PositiveFloat = 600.0
    start_max: PositiveFloat = 1800.0
    close: PositiveFloat = 60.0
    client_start: PositiveFloat = 300.0  # starting the client's anchor and checking the share, once per client

    @model_validator(mode="after")
    def _create_request(self) -> Timeouts:
        if self.create_request >= self.create:
            raise ValueError(
                "timeouts.create_request must be below timeouts.create, or lost responses are never recovered"
            )
        if self.pending is not None and self.pending >= self.create:
            if "pending" in self.model_fields_set:
                raise ValueError("timeouts.pending must be below timeouts.create, or a stuck sandbox is never replaced")
            # Preserve the effective disabled value in full snapshots. Otherwise revalidation
            # mistakes the omitted default for an explicitly configured, invalid deadline.
            object.__setattr__(self, "pending", None)
        return self

    def pending_limit(self) -> float | None:
        return self.pending if self.pending is not None and self.pending < self.create else None


class Process(_Model):
    poll_interval: PositiveFloat = 2.0


class Reaper(_Model):
    anchor_image: str = ""  # anchor image (repo@sha256:..., ships /.flotilla/bin/busybox); required by provider
    anchor_ttl: PositiveInt = 600
    renew_interval: PositiveFloat = 30.0
    grace: PositiveFloat = 90.0  # G
    gc_window: PositiveFloat = 180.0  # W
    renew_before_s: PositiveFloat = 600
    renew_margin_s: PositiveFloat = 120
    trial_ttl: PositiveInt = 1800  # platform TTL per instance; long trials renew

    @model_validator(mode="after")
    def _timing(self) -> Reaper:
        # Renew interval well below G (two renewals). G + list_visibility_s < W needs [platform]: see below.
        ok = (
            self.renew_interval >= ANCHOR_RETRY_S
            and self.renew_interval * 2 <= self.grace < self.anchor_ttl - EXPIRY_MARGIN_S
        )
        if not ok:
            raise ValueError(
                f"requires renew_interval >= {ANCHOR_RETRY_S} and "
                f"renew_interval * 2 <= grace < anchor_ttl - {EXPIRY_MARGIN_S:g}"
            )
        if self.gc_window <= self.grace:
            raise ValueError("gc_window (W) must be greater than grace (G)")
        return self


class Resource(_Model):
    cpu: str
    memory: str

    @field_validator("cpu")
    @classmethod
    def _cpu(cls, value: str) -> str:
        return cpu(value)

    @field_validator("memory")
    @classmethod
    def _memory(cls, value: str) -> str:
        return memory(value)


class TaskResources(_Model):
    services: dict[str, Resource] = {}


class ResourceDefaults(_Model):
    default: Resource = Resource(cpu="1", memory="2Gi")
    services: dict[str, Resource] = {}
    tasks: dict[str, TaskResources] = {}

    def effective(self, task: str, service: str, declared: Mapping[str, str]) -> dict[str, str]:
        values = {**self.default.model_dump(), **declared}
        if service in self.services:
            values.update(self.services[service].model_dump())
        if task in self.tasks and service in self.tasks[task].services:
            values.update(self.tasks[task].services[service].model_dump())
        return values

    def for_services(self, task: str, declared: Mapping[str, Mapping[str, str]]) -> dict[str, Resources]:
        """The effective resource allocation used by preflight and creation.

        Takes the declarations rather than a `Project` so that configuration stays free of the
        Compose model: the runtime imports this module and must not pull the parser in with it.
        """
        return {name: Resources(**self.effective(task, name, dict(values))) for name, values in declared.items()}


class Prepare(_Model):
    """The attempt's own management sandbox: where attempt-owned storage is compiled."""

    management_profile: InstanceProfile = "default"
    management_resources: Resource | None = None  # None preserves the lightweight anchor default.
    management_network: Network = Field(default_factory=Network)
    management_mounts: tuple[SharedVolume, ...] = ()
    # Nonsecret connection settings only; credentials belong in explicit RO mounts.
    management_env: dict[str, str] = Field(default_factory=dict)
    concurrency: PositiveInt = 4
    workspace_command: tuple[str, ...] = ("python3", "-I", "-m", "flotilla.integrations.workspace")
    workspace_timeout: PositiveFloat = 1800.0

    @field_validator("workspace_command")
    @classmethod
    def _command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not part for part in value):
            raise ValueError("workspace_command must be a nonempty argv")
        return value


class ProbeTarget(_Model):
    host: str
    port: Annotated[int, Field(ge=1, le=65535)]
    protocol: Literal["tcp", "udp"] = "tcp"

    service: Literal["echo", "https", "ntp"] = "echo"

    @model_validator(mode="after")
    def _service(self) -> ProbeTarget:
        ExternalTarget(self.host, self.port, self.protocol, self.service)
        return self

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        try:
            ipaddress.ip_address(value)
        except ValueError:
            if not _is_hostname(value) or value.startswith("*."):
                raise ValueError("probe target host must be an IP address or exact hostname") from None
        return value


class PlatformFacts(_Model):
    """Platform properties a request cannot discover, so the deployment states them.

    Everything a platform either does or refuses is assumed available instead: asking for a device
    it has no runtime for, or a tmpfs it cannot mount, fails at create, and a clear refusal beats a
    report that was right last month. What cannot work that way is a number a request has to be
    built around -- how long a listing lags, how large a TTL may be -- because getting one wrong
    does not refuse anything, it just misbehaves later.
    """

    #: How long after a create request a listing can still first reveal the instance. GC waits
    #: this out before collecting, so a value below the truth can collect a live launch.
    list_visibility_s: PositiveFloat = 30.0
    #: The largest TTL the platform grants an instance, if it caps them. A trial's TTL is clamped
    #: to it, so a value above the truth lets a sandbox expire while its trial is still running.
    max_ttl_s: PositiveInt | None = None
    #: Longest label value the platform stores; longer values are truncated to fit.
    max_label_value_len: Annotated[int, Field(ge=63)] = 63
    #: Most instances the platform will put in one connected group.
    link_max_members: Annotated[int, Field(ge=2)] = 9
    #: Egress the platform allows regardless of policy, as `host:port/protocol`. A task asking for
    #: no network is refused when anything here is not just cluster DNS.
    implicit_egress: tuple[str, ...] = ()
    #: Resolver addresses the platform's own DNS uses; only TCP/UDP 53 to these may be implicit.
    cluster_dns: tuple[str, ...] = ()

    @field_validator("cluster_dns")
    @classmethod
    def _cluster_dns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(cluster_dns_address(ip) for ip in value)


class Probe(_Model):
    image: str = ""
    privileged_image: str = ""
    total_timeout_s: PositiveFloat = 3600.0
    cleanup_timeout_s: PositiveFloat = 180.0
    external_targets: tuple[ProbeTarget, ...] = ()
    dns_name: str = ""

    @field_validator("image", "privileged_image")
    @classmethod
    def _image(cls, value: str) -> str:
        if value and not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", value):
            raise ValueError("probe images must be pinned repo@sha256:<64 lowercase hex> references")
        return value


class DatasetBinding(_Model):
    """Where one published dataset is, on both sides; no I/O at load time.

    The dataset is an input someone else publishes. Both views are declared because neither side
    can infer the other: a client path says nothing about how the platform mounts the storage.
    """

    #: Absolute: a spec is shared between processes that do not share a working directory.
    local_root: Path
    platform_root: str = Field(min_length=1)
    # Name of the [inputs.<name>] root holding the dataset; None means the work root ([storage]).
    input: str | None = None


class RuntimeLayerBinding(_Model):
    """Location of one agent runtime layer on its storage root; Flotilla mounts it read-only, nothing more.

    `local_root` is an optional host copy for consumers that want one; Flotilla never reads it.
    """

    platform_root: str = Field(min_length=1)
    #: Absolute when given, for the same reason as a dataset's.
    local_root: Path | None = None


class AgentRuntime(_Model):
    """Typed agent runtime bindings: which layers must exist stays the consumer's contract.

    Flotilla validates the shape, layer names and that `input` names a declared read-only root;
    it does not interpret layer names, count layers, or learn anything about the agent.
    """

    input: str | None = None  # name of the [inputs.<name>] root holding the layers; None means the work root
    layers: dict[str, RuntimeLayerBinding] = Field(default_factory=dict)


class FlotillaConfig(_Model):
    backend: str = BUILTIN
    opensandbox: OpenSandbox | None = None  # built-in backend table
    backends: dict[str, dict[str, Any]] = {}  # third-party backend tables, validated by the selected backend
    storage: Storage
    # Named read-only roots for published datasets and external read-only payloads (e.g. an owner's
    # storage shared read-only with this deployment). Launch, attempt and share state always stay on `storage`.
    inputs: dict[str, Storage] = Field(default_factory=dict)
    datasets: dict[str, DatasetBinding] = Field(default_factory=dict)
    # Typed agent runtime layer bindings (see AgentRuntime); never interpreted beyond shape and roots.
    agent_runtime: AgentRuntime | None = None
    prepare: Prepare = Prepare()
    platform: PlatformFacts = PlatformFacts()
    probe: Probe = Probe()
    share: Share = Share()
    registry: Registry = Registry()
    network: Network = Network()
    security: Security = Security()
    runtime_params: dict[str, str] = {}
    agent_overlay: AgentOverlay = AgentOverlay()
    runtime: Runtime = Runtime()
    limits: Limits = Limits()
    timeouts: Timeouts = Timeouts()
    process: Process = Process()
    reaper: Reaper = Reaper()
    resources: ResourceDefaults = ResourceDefaults()

    @model_validator(mode="after")
    def _local_roots(self) -> FlotillaConfig:
        """A client-side path must be absolute: a spec outlives the directory it was written in.

        A relative one used to resolve against the deployment file's directory. Without that file
        there is no such anchor, and resolving against the working directory would make one spec
        mean different things in the client, a Harbor worker and `flotilla gc`.
        """
        relative = [f"datasets.{n}" for n, b in self.datasets.items() if not b.local_root.is_absolute()]
        if self.agent_runtime is not None:
            relative += [
                f"agent_runtime.layers.{n}"
                for n, layer in self.agent_runtime.layers.items()
                if layer.local_root is not None and not layer.local_root.is_absolute()
            ]
        if relative:
            raise ValueError(f"local_root must be an absolute path: {', '.join(sorted(relative))}")
        return self

    @model_validator(mode="after")
    def _timing_against_platform(self) -> FlotillaConfig:
        """Reaper timing has to fit the platform's own numbers, which `[platform]` states."""
        problems = []
        visibility = self.platform.list_visibility_s
        if not self.reaper.grace + visibility < self.reaper.gc_window:
            problems.append(
                f"requires reaper.grace + platform.list_visibility_s < reaper.gc_window ({self.reaper.grace} + "
                f"{visibility} >= {self.reaper.gc_window})"
            )
        max_ttl = self.platform.max_ttl_s
        if max_ttl is not None and self.reaper.anchor_ttl > max_ttl:
            problems.append(f"reaper.anchor_ttl {self.reaper.anchor_ttl} exceeds platform.max_ttl_s {max_ttl}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @model_validator(mode="before")
    @classmethod
    def _removed(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and "preparation" in data:
            raise ValueError(
                "[preparation] was removed: open no longer builds images. Move concurrency to "
                "[prepare] (the management anchor) and timeout_s to runtime.startup_timeout_s"
            )
        if isinstance(data, Mapping) and "voyager" in data:
            raise ValueError(
                "[voyager] was replaced by the typed [agent_runtime] section: move runtime layer bindings to "
                "[agent_runtime.layers.<name>] with `input` naming an [inputs.<name>] root; agent identity and "
                "runtime paths belong to the consumer, not the deployment"
            )
        return data

    @model_validator(mode="after")
    def _local(self) -> FlotillaConfig:
        for name in self.inputs:
            check_root_name(name)
            if name == WORK_ROOT:
                raise ValueError(f"[inputs.{WORK_ROOT}] is reserved for [storage]")
        for label, binding in self.datasets.items():
            if binding.input is not None and binding.input not in self.inputs:
                raise ValueError(f"datasets.{label}.input names no [inputs.{binding.input}]")
        if self.agent_runtime is not None:
            for name in self.agent_runtime.layers:
                check_root_name(name)
            selected = self.agent_runtime.input
            if selected is not None and selected not in self.inputs:
                raise ValueError(f"agent_runtime.input names no [inputs.{selected}]")
        backend = load_backend(self.backend)
        if self.backend == BUILTIN:
            if self.backends:
                raise ValueError("[backends.*] tables are only read for third-party backends; use [opensandbox]")
            if self.opensandbox is None:
                raise ValueError("[opensandbox] is required (or select another backend with backend = ...)")
        else:
            if self.opensandbox is not None:
                raise ValueError(f"[opensandbox] is set but backend = {self.backend!r}")
            if self.backend not in self.backends:
                raise ValueError(f"backend = {self.backend!r} requires a [backends.{self.backend}] table")
            if unused := sorted(set(self.backends) - {self.backend}):
                raise ValueError(f"[backends.*] tables for unselected backends: {', '.join(unused)}")
        problems = backend.check(self.backend_settings, self)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @classmethod
    def load(cls, target: str) -> FlotillaConfig:
        """Import a spec a caller already wrote, named `package.module:attribute`.

        A caller constructs `FlotillaConfig` directly, so there is no deployment file to parse --
        and nothing worth inventing a second format for. What a separate process still needs is a
        way to name the spec it should use, which is what this is: `flotilla gc` exists precisely
        because a crashed launch leaves nobody to call the SDK, and it has to reach the same
        deployment that crashed. Importing is as trusted as the recipe that holds the spec.
        """
        module_name, separator, attribute = target.partition(":")
        if not separator or not module_name or not attribute:
            raise ConfigError([f"a deployment must be named package.module:attribute, not {target!r}"])
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ConfigError([f"cannot import {module_name}: {exc}"]) from exc
        try:
            spec = getattr(module, attribute)
        except AttributeError:
            raise ConfigError([f"{module_name} defines no {attribute}"]) from None
        if callable(spec) and not isinstance(spec, FlotillaConfig):
            spec = spec()
        if not isinstance(spec, FlotillaConfig):
            raise ConfigError([f"{target} is {type(spec).__name__}, not a FlotillaConfig"])
        return spec

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> FlotillaConfig:
        """Validate a mapping as a spec, the inverse of `model_dump(mode="json")`.

        For a caller holding a spec as plain data: a recipe reading its own file, or a process
        receiving one over a wire. A spec names no relative path, so nothing has to be resolved
        on the way in or out.
        """
        return cls.model_validate(data)

    @property
    def backend_settings(self) -> BackendSettings:
        """The selected backend's validated table: `[opensandbox]` or `[backends.<name>]`."""
        if self.backend == BUILTIN:
            return self.opensandbox_settings()
        table = self.backends[self.backend]
        try:
            return load_backend(self.backend).settings_model.model_validate(table)
        except ValidationError as exc:
            # Re-raise with the TOML path; `hide_input_in_errors` keeps values out of the message.
            prefix = f"backends.{self.backend}"
            raise ValueError(
                "; ".join(f"{'.'.join([prefix, *map(str, e['loc'])])}: {e['msg']}" for e in exc.errors())
            ) from None

    def opensandbox_settings(self) -> OpenSandbox:
        if self.opensandbox is None:
            raise ConfigError([f"backend {self.backend!r} has no [opensandbox] settings"])
        return self.opensandbox

    def resolve_datasets(self) -> dict[str, Dataset]:
        """The declared datasets, each resolved against the storage root it lives on."""
        from flotilla.native.dataset import datasets as resolve

        bindings = {}
        for name, binding in self.datasets.items():
            bindings[name] = {
                "local_root": binding.local_root,
                "platform_root": binding.platform_root,
                "root": binding.input or WORK_ROOT,
            }
        return resolve(bindings, storage=self.storage.settings(), inputs=self.input_settings())

    def platform_capabilities(self) -> Capabilities:
        """What this deployment may be asked for.

        Everything a platform either does or refuses is assumed available, because asking and
        being refused is a better error than a report that was accurate last month. The values
        that cannot work that way are the `[platform]` facts: numbers a request has to be built
        around, where a wrong value misbehaves later instead of refusing now.
        """
        facts = self.platform
        caps = Capabilities(
            max_label_value_len=facts.max_label_value_len,
            list_visibility_s=facts.list_visibility_s,
            max_ttl_seconds=facts.max_ttl_s,
            link=True,
            link_udp=True,
            link_max_members=facts.link_max_members,
            inbound_isolation=True,
            external_forms=frozenset({"none", "any", "allowlist"}),
            implicit_egress=facts.implicit_egress,
            shared_volume=True,
            volume_file=True,
            privileged_runtime=True,
            devices=frozenset(DEVICES),
            cluster_dns=facts.cluster_dns,
            sandbox_ingress=True,
            readonly_rootfs=True,
            native_tmpfs=True,
            shm_size=True,
        )
        return load_backend(self.backend).capabilities(self.backend_settings, self, caps)

    def management_capabilities(self) -> Capabilities:
        """A bounded management client, NOT a capability report usable by normal tasks."""
        return Capabilities(
            max_label_value_len=63,
            list_visibility_s=self.platform.list_visibility_s,
            max_ttl_seconds=None,
            link=False,
            link_udp=False,
            link_max_members=1,
            inbound_isolation=False,
            external_forms=frozenset(),
            implicit_egress=(),
            shared_volume=False,
            volume_file=False,
            privileged_runtime=False,
            devices=frozenset(),
        )

    def probe_settings(self) -> ProbeSettings:

        if not self.probe.image:
            raise ConfigError(["probe.image must be a pinned image reference"])
        return ProbeSettings(
            **self.probe.model_dump(exclude={"external_targets"}),
            external_targets=tuple(ExternalTarget(**t.model_dump()) for t in self.probe.external_targets),
            share_release=self.share.release,
            endpoint=self.backend_settings.endpoint,
            create_timeout_s=self.timeouts.create,
            ttl_s=self.reaper.trial_ttl,
            anchor_ttl_s=self.reaper.anchor_ttl,
            list_visibility_s=self.platform.list_visibility_s,
            test_external_any=self.network.external == "any",
        )

    def filesystem_settings(self) -> FilesystemSettings:
        return FilesystemSettings(self.opensandbox_settings().filesystem_protocol)

    def credential(self, environ: Mapping[str, str] | None = None) -> str:
        """Read the platform credential from the environment; the error names only the variable."""
        env = os.environ if environ is None else environ
        name = self.backend_settings.credential_env
        value = env.get(name, "")
        if not value:
            raise ConfigError([f"environment variable {name} is not set (platform credential)"])
        return value

    def build_platform(
        self, *, clock: Clock, caps: Capabilities, command_gate: Callable[[], Awaitable[None]] | None = None
    ) -> Platform:
        """Construct the selected backend's platform client; the caller owns it and calls `aclose()`."""
        return load_backend(self.backend).build(
            self.backend_settings, self, clock=clock, credential=self.credential(), caps=caps, command_gate=command_gate
        )

    def check_isolation(self, evidence: IsolationEvidence) -> None:
        """What `flotilla probe` measured, against what this deployment accepts missing.

        This runs where the probe runs, not at client start: the evidence is values in memory, and
        a client in another process has no way to re-derive them. So isolation is a step an
        operator takes against a deployment, and its result is this call's exit status.
        """
        problems: list[str] = []
        gaps: set[str] = set(evidence.gaps())
        accepted: set[str] = set(self.security.accept_insecure)
        for name in sorted(gaps - accepted):
            problems.append(
                f"deployment did not demonstrate isolation property {name} "
                f"({getattr(evidence, name).observed or 'not measured'}); "
                "accept it explicitly in security.accept_insecure to run anyway"
            )
        for name in sorted(accepted - gaps):
            logging.getLogger(__name__).warning("Unused security waiver: %s", name)
        if not evidence.cleanup.held:
            problems.append(f"probe could not confirm cleanup: {evidence.cleanup.observed or 'not measured'}")

        form = self.network.policy().mode
        if form != "none" and form not in evidence.external_forms:
            problems.append(
                f"network.external form {form} was not exercised; probe observed {sorted(evidence.external_forms)}"
            )

        if problems:
            raise ConfigError(problems)

    def anchor_settings(self, *, workspace: bool = False) -> AnchorSettings:
        if not self.reaper.anchor_image:
            raise ConfigError(["reaper.anchor_image is not set"])
        settings = AnchorSettings(
            image=self.reaper.anchor_image,
            ttl_s=self.reaper.anchor_ttl,
            renew_interval_s=self.reaper.renew_interval,
            grace_s=self.reaper.grace,
            external=self.prepare.management_network.policy() if workspace else ExternalPolicy(mode="none"),
            management_mounts=self.prepare.management_mounts if workspace else (),
            management_env=dict(self.prepare.management_env) if workspace else {},
            profile=self.prepare.management_profile if workspace else "default",
            inputs=tuple(sorted(self.inputs)),
            concurrency=self.limits.anchor_concurrency,
        )
        if workspace and self.prepare.management_resources is not None:
            settings = replace(settings, resources=Resources(**self.prepare.management_resources.model_dump()))
        return settings

    def reaper_settings(self) -> ReaperSettings:
        return ReaperSettings(
            create_rate=self.limits.create_rate,
            create_concurrency=self.limits.create_concurrency,
            create_request_timeout_s=self.timeouts.create_request,
            close_timeout_s=self.timeouts.close,
        )

    def trial_settings(self) -> TrialSettings:
        t = self.timeouts
        return TrialSettings(
            share_release=self.share.release,
            external=self.network.policy(),
            ttl_s=self.reaper.trial_ttl,
            create_timeout_s=t.create,
            pending_timeout_s=t.pending_limit(),
            address_timeout_s=t.address,
            wire_timeout_s=t.wire,
            hosts_timeout_s=t.hosts,
            prepare_timeout_s=t.prepare,
            start_max_s=t.start_max,
            process_poll_s=self.process.poll_interval,
        )

    def monitor_settings(self) -> MonitorSettings:
        ttl = self.reaper.trial_ttl
        before = min(self.reaper.renew_before_s, ttl / 2)
        return MonitorSettings(
            check_timeout_s=self.runtime.check_timeout_s,
            ttl_s=ttl,
            renew_before_s=before,
            renew_margin_s=min(self.reaper.renew_margin_s, before / 2),
        )

    def network_settings(self) -> NetworkSettings:
        n = self.opensandbox_settings().network
        return NetworkSettings(
            sandbox_cidrs=n.sandbox_cidrs,
            platform_cidrs=n.platform_cidrs,
            max_rules=n.max_rules,
            link=n.link,
            ingress=n.ingress,
        )

    def storage_settings(self) -> StorageSettings:
        return self.storage.settings()

    def input_settings(self) -> dict[str, StorageSettings]:
        return {name: root.settings() for name, root in self.inputs.items()}

    def storage_settings_for(self, root: str | None) -> StorageSettings:
        """Settings of the work root (`None`/`"work"`) or of a named read-only input root."""
        if root is None or root == WORK_ROOT:
            return self.storage.settings()
        if root not in self.inputs:
            raise ConfigError([f"no [inputs.{root}] storage root is configured"])
        return self.inputs[root].settings()

    def execd_settings(self) -> ExecdSettings:
        e = self.opensandbox_settings().execd
        return ExecdSettings(
            protocol=e.protocol, wrapper=e.wrapper, output_limit_bytes=self.runtime.exec_output_limit_bytes
        )


def _as_network(target: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(target, strict=False)
    except ValueError:
        return None


_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _is_hostname(target: str) -> bool:
    """Domain name (optional leading `*.` wildcard): valid DNS labels, at least two, a non-numeric TLD (rejects
    partial IPs like `1.2.3`), and two labels after the wildcard (`*.com` would allow a whole TLD)."""
    name = target.removeprefix("*.")
    labels = name.split(".")
    if len(name) > 253 or len(labels) < 2 or labels[-1].isdigit():
        return False
    return all(_LABEL.fullmatch(label) for label in labels)


__all__ = ["ConfigError", "FlotillaConfig"]


class OpenSandboxBackend(Backend[OpenSandbox]):
    """The built-in backend, configured by `[opensandbox]`."""

    settings_model = OpenSandbox

    def check(self, settings: OpenSandbox, cfg: FlotillaConfig) -> list[str]:
        problems = []
        if cfg.prepare.management_profile == "management" and not settings.management_extensions:
            problems.append("prepare.management_profile = management requires opensandbox.management_extensions")
        net = settings.network
        external = cfg.network.external
        if net.ingress == "sandbox" and net.link != "sandbox":
            problems.append("ingress = sandbox requires link = sandbox")
        narrow = () if external == "any" else external
        if net.link == "cidr" and not net.sandbox_cidrs:
            problems.append("opensandbox.network.link = cidr requires sandbox_cidrs")
        if net.link == "sandbox" and external == "any":
            problems.append("link = sandbox supports only external = [] or an allowlist, not external = any")
        if net.link == "cidr" and external == "any" and not net.platform_cidrs:
            problems.append("network.external = any requires opensandbox.network.platform_cidrs")
        if net.link == "cidr" and any(_as_network(t) for t in narrow) and not net.sandbox_cidrs:
            problems.append("network.external with CIDRs requires opensandbox.network.sandbox_cidrs")
        return problems

    def capabilities(self, settings: OpenSandbox, cfg: FlotillaConfig, caps: Capabilities) -> Capabilities:
        caps = replace(caps, sandbox_ingress=caps.sandbox_ingress and settings.network.ingress == "sandbox")
        return FilesystemSettings(settings.filesystem_protocol).capabilities(caps)

    def build(
        self,
        settings: OpenSandbox,
        cfg: FlotillaConfig,
        *,
        clock: Clock,
        credential: str,
        caps: Capabilities,
        command_gate: Callable[[], Awaitable[None]] | None = None,
    ) -> Platform:
        return build_opensandbox(
            endpoint=settings.endpoint,
            credential=credential,
            clock=clock,
            settings=cfg.network_settings(),
            storage=cfg.storage_settings(),
            inputs=cfg.input_settings(),
            caps=caps,
            execd=cfg.execd_settings(),
            filesystem=cfg.filesystem_settings(),
            command_gate=command_gate,
            extensions=settings.extensions,
            create_fields=settings.create_fields,
            group_extension=settings.group_extension,
            privileged_extensions=settings.privileged_extensions,
            management_extensions=settings.management_extensions,
            create_timeout_s=cfg.timeouts.create_request,
            max_connections=settings.max_connections,
        )
