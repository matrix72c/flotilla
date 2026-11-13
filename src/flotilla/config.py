"""Deployment configuration, explicit policy validation, and runtime settings."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import tomllib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from flotilla.backends import BUILTIN, Backend, BackendSettings, load_backend
from flotilla.capabilities import SECURITY_FIELDS, CapabilityReport, ExternalTarget, ProbeSettings
from flotilla.core.anchor import AnchorSettings
from flotilla.core.reaper import ReaperSettings
from flotilla.core.runtime import MonitorSettings
from flotilla.core.trial import TrialSettings
from flotilla.manifest import Manifest
from flotilla.platform.base import (
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
from flotilla.platform.opensandbox import ExecdSettings, NetworkSettings, StorageSettings
from flotilla.platform.opensandbox import build as build_opensandbox
from flotilla.platform.opensandbox.filesystem import FilesystemProtocol, FilesystemSettings
from flotilla.resources import cpu, memory
from flotilla.share.release import BUILTIN_RELEASE, BUILTIN_RELEASE_SHA256

if TYPE_CHECKING:
    from flotilla.build.images import BuildSettings
    from flotilla.native import ReleaseBindings

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


class Build(_Model):
    """Dataset builder config `[build]`, used by `flotilla prepare`."""

    pullable_registries: tuple[str, ...] = ()
    mirror_images: bool = False
    oci_mediatypes: bool = False
    attestations: bool = False
    target: str = ""
    image_replacements: dict[str, str] = {}

    def settings(self) -> BuildSettings:
        from flotilla.build.images import BuildSettings

        return BuildSettings(
            pullable_registries=self.pullable_registries,
            mirror_images=self.mirror_images,
            oci_mediatypes=self.oci_mediatypes,
            attestations=self.attestations,
            target=self.target,
            image_replacements=self.image_replacements,
        )


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
        # Renew interval well below G (two renewals). G + list_visibility_s < W needs the report: check_against.
        ok = self.renew_interval >= ANCHOR_RETRY_S and self.renew_interval * 2 <= self.grace < self.anchor_ttl
        if not ok:
            raise ValueError(
                f"requires renew_interval >= {ANCHOR_RETRY_S} and renew_interval * 2 <= grace < anchor_ttl"
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

    def for_manifest(self, manifest: Manifest) -> dict[str, Resources]:
        """The same effective resource allocation used by preflight and creation."""
        return {
            name: Resources(**self.effective(manifest.task, name, unit.resources.model_dump(exclude_none=True)))
            for name, unit in manifest.units.items()
        }


class Prepare(_Model):
    """Attempt-local compilation and optional image-cache prewarming."""

    management_profile: InstanceProfile = "default"
    management_resources: Resource | None = None  # None preserves the lightweight anchor default.
    management_network: Network = Field(default_factory=Network)
    management_mounts: tuple[SharedVolume, ...] = ()
    # Nonsecret connection settings only; credentials belong in explicit RO mounts.
    management_env: dict[str, str] = Field(default_factory=dict)
    concurrency: PositiveInt = 4
    docker_command: tuple[str, ...] = ("docker",)
    workspace_command: tuple[str, ...] = ("python3", "-I", "-m", "flotilla.integrations.workspace")
    compose_command: tuple[str, ...] = ("docker", "compose")
    compose_version: str = "2.39.4"
    workspace_timeout: PositiveFloat = 1800.0
    export_timeout: PositiveFloat = 600.0  # per `docker cp` of an image volume's initial content

    @field_validator("docker_command", "workspace_command", "compose_command")
    @classmethod
    def _command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _argv(value)


def _argv(value: tuple[str, ...]) -> tuple[str, ...]:
    if not value or any(not part for part in value):
        raise ValueError("docker_command must be a nonempty argv")
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


class Probe(_Model):
    deployment: str = "deployment"
    image: str = ""
    privileged_image: str = ""
    inner_image: str = ""
    link_members: Annotated[int, Field(ge=2, le=32)] = 9
    hosts_recheck_s: Annotated[float, Field(ge=0)] = 600.0
    list_visibility_s: PositiveFloat = 30.0
    total_timeout_s: PositiveFloat = 3600.0
    cleanup_timeout_s: PositiveFloat = 180.0
    external_targets: tuple[ProbeTarget, ...] = ()
    dns_name: str = ""

    @field_validator("image", "privileged_image", "inner_image")
    @classmethod
    def _image(cls, value: str) -> str:
        if value and not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", value):
            raise ValueError("probe images must be pinned repo@sha256:<64 lowercase hex> references")
        return value


class TaskRelease(_Model):
    """Deployment locations, not immutable task identity; no I/O at load time."""

    local_root: Path
    platform_root: str = Field(min_length=1)
    # Name of the [inputs.<name>] root holding the release; None means the work root ([storage]).
    input: str | None = None


class RuntimeLayerBinding(_Model):
    """Location of one agent runtime layer on its storage root; Flotilla mounts it read-only, nothing more.

    `local_root` is an optional host copy for consumers that want one; Flotilla never reads it.
    """

    platform_root: str = Field(min_length=1)
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
    # Named read-only roots for published task releases and external read-only payloads (e.g. an owner's
    # storage shared read-only with this deployment). Launch, attempt and share state always stay on `storage`.
    inputs: dict[str, Storage] = Field(default_factory=dict)
    task_releases: dict[str, TaskRelease] = Field(default_factory=dict)
    # Typed agent runtime layer bindings (see AgentRuntime); never interpreted beyond shape and roots.
    agent_runtime: AgentRuntime | None = None
    build: Build = Build()
    prepare: Prepare = Prepare()
    probe: Probe = Probe()
    share: Share = Share()
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

    #: Config file directory (set by `load`) for resolving relative paths; not part of the TOML.
    base_dir: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _removed(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and "preparation" in data:
            raise ValueError(
                "[preparation] was removed: open no longer builds images. Move docker_command/concurrency to "
                "[prepare] (used by `flotilla prepare`) and timeout_s to runtime.startup_timeout_s"
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
        for label, release in self.task_releases.items():
            if release.input is not None and release.input not in self.inputs:
                raise ValueError(f"task_releases.{label}.input names no [inputs.{release.input}]")
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
    def load(cls, path: str | Path) -> FlotillaConfig:
        path = Path(path)
        try:
            text = path.read_text()
        except OSError as exc:
            # An unreadable config file is a usage error: report it as a config error, without a traceback.
            raise ConfigError([f"cannot read deployment configuration {path}: {exc.strerror or exc}"]) from exc
        if path.suffix == ".json":
            # A frozen copy written by Deployment.freeze.
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ConfigError([f"{path} is not valid JSON: {exc}"]) from exc
        else:
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError([f"{path} is not valid TOML: {exc}"]) from exc
        return cls.model_validate({**data, "base_dir": path.resolve().parent})

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, base_dir: Path | None = None) -> FlotillaConfig:
        return cls.model_validate({**data, "base_dir": base_dir})

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

    def to_mapping(self) -> dict[str, Any]:
        """JSON-compatible config with an absolute report path, for a child process without `base_dir`."""
        data = self.model_dump(mode="json", exclude={"base_dir"})
        table = data["opensandbox"] if self.backend == BUILTIN else data["backends"][self.backend]
        table["capabilities"] = str(self.capabilities_path().resolve())
        for name, binding in self.task_releases.items():
            root = binding.local_root
            if not root.is_absolute() and self.base_dir is not None:
                root = self.base_dir / root
            data["task_releases"][name]["local_root"] = str(root.absolute())
        if self.agent_runtime is not None:
            for layer_name, layer in self.agent_runtime.layers.items():
                if layer.local_root is None:
                    continue
                local = layer.local_root
                if not local.is_absolute() and self.base_dir is not None:
                    local = self.base_dir / local
                data["agent_runtime"]["layers"][layer_name]["local_root"] = str(local.absolute())
        return data

    def release_bindings(self) -> ReleaseBindings:
        """Open releases only for consume/train, not probe/select/prepare."""
        from flotilla.native import ReleaseBinding, ReleaseBindings

        bindings = {}
        for name, binding in self.task_releases.items():
            root = binding.local_root
            if not root.is_absolute() and self.base_dir is not None:
                root = self.base_dir / root
            bindings[name] = ReleaseBinding(
                local_root=root, platform_root=binding.platform_root, root=binding.input or WORK_ROOT
            )
        return ReleaseBindings(bindings, storage=self.storage.settings(), inputs=self.input_settings())

    def capabilities_path(self) -> Path:
        path = Path(self.backend_settings.capabilities)
        if not path.is_absolute() and self.base_dir is not None:
            path = self.base_dir / path
        return path

    def load_report(self) -> CapabilityReport:
        return CapabilityReport.load_json(self.capabilities_path().read_bytes())

    def platform_capabilities(self, report: CapabilityReport | None = None) -> Capabilities:
        """Capabilities measured by the probe, adjusted for what this deployment enforces."""
        report = report or self.load_report()
        caps = report.to_capabilities()
        rt = report.runtime
        caps = replace(
            caps,
            privileged_runtime=(
                rt.privileged_runtime.passed_for("privileged")
                and rt.privileged_storage.passed_for("privileged")
                and rt.privileged_network.passed_for("privileged")
                and (
                    rt.privileged_no_auto_mount.passed_for("privileged")
                    or "no_auto_mount" in self.security.accept_insecure
                )
                and (rt.privileged_exec_auth.passed_for("privileged") or "exec_auth" in self.security.accept_insecure)
                and (
                    rt.privileged_inbound_isolation.passed_for("privileged")
                    or "inbound_isolation" in self.security.accept_insecure
                )
            ),
        )
        caps = replace(
            caps,
            devices=frozenset(
                name
                for name, item in (("fuse", rt.fuse), ("kvm", rt.kvm))
                if caps.privileged_runtime and item.ok and item.scope == "privileged"
            ),
        )
        return load_backend(self.backend).capabilities(self.backend_settings, self, caps)

    def evidence_profile(self) -> str:
        """Bind relevant operational choices, not build concurrency or image replacements."""
        settings = self.backend_settings.model_dump(mode="json")
        for key in ("capabilities", "credential_env", "max_connections"):
            settings.pop(key, None)
        if self.backend == BUILTIN:
            # Workspace-only selection is not measured by task/probe capability checks.
            settings.pop("management_extensions", None)
        data = {
            "protocol": "native-task-v1-missing-mount-targets",
            "backend": self.backend,
            "settings": settings,
            "storage": self.storage.model_dump(mode="json"),
            **(
                {"inputs": {name: root.model_dump(mode="json") for name, root in sorted(self.inputs.items())}}
                if self.inputs
                else {}
            ),
            "share": self.share.model_dump(mode="json"),
            "network": self.network.model_dump(mode="json"),
            "anchor_image": self.reaper.anchor_image,
            "anchor_ttl": self.reaper.anchor_ttl,
            "trial_ttl": self.reaper.trial_ttl,
            "list_visibility_s": self.probe.list_visibility_s,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def management_capabilities(self) -> Capabilities:
        """A bounded management client, NOT a capability report usable by normal tasks."""
        return Capabilities(
            max_label_value_len=63,
            list_visibility_s=self.probe.list_visibility_s,
            max_ttl_seconds=None,
            link=False,
            link_udp=False,
            link_max_members=1,
            inbound_isolation=False,
            external_forms=frozenset(),
            implicit_egress=(),
            internal_address="none",
            shared_volume=False,
            volume_file=False,
            no_auto_mount=False,
            exec_auth=False,
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
            evidence_profile=self.evidence_profile(),
            test_external_any=self.network.external == "any",
            filesystem_supported=(
                self.backend == BUILTIN and self.opensandbox_settings().filesystem_protocol == "sandbox-system-v1"
            ),
        )

    def filesystem_settings(self) -> FilesystemSettings:
        return FilesystemSettings(self.opensandbox_settings().filesystem_protocol)

    def filesystem_profile(self) -> str:
        """Evidence applies to these creation/runtime settings, never just a hostname."""
        return load_backend(self.backend).profile(self.backend_settings, self)

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

    def check_against(self, report: CapabilityReport) -> None:
        """Startup validation without platform calls. Raises `ConfigError` listing every problem."""
        problems: list[str] = []
        if report.evidence_profile != self.evidence_profile():
            # Informational only: the report describes the platform, not this exact deployment text.
            logging.getLogger(__name__).warning(
                "capability report was measured with different deployment settings; rerun probe if the platform "
                "behavior changed"
            )
        endpoint = self.backend_settings.endpoint
        if report.endpoint.rstrip("/") != endpoint:
            problems.append(f"capability report endpoint {report.endpoint} does not match config {endpoint}")
        problems += [f"deployment lacks required capability: {name}" for name in report.missing_required()]

        form = self.network.policy().mode
        if form not in report.network.external_forms:
            problems.append(
                f"network.external form {form} is not among supported {sorted(report.network.external_forms)}"
            )

        gaps = set(report.security_gaps())
        for name in sorted(gaps - self.security.accept_insecure):
            problems.append(
                f"deployment lacks security capability {name}; accept it explicitly in security.accept_insecure"
            )
        for name in sorted(self.security.accept_insecure - gaps):
            logging.getLogger(__name__).warning("Unused security waiver: %s", name)

        visibility = self.probe.list_visibility_s
        if not self.reaper.grace + visibility < self.reaper.gc_window:
            problems.append(
                f"requires grace + list_visibility_s < gc_window ({self.reaper.grace} + {visibility} >= "
                f"{self.reaper.gc_window})"
            )
        max_ttl = report.lifecycle.max_ttl_seconds
        if max_ttl is not None and self.reaper.anchor_ttl > max_ttl:
            problems.append(f"reaper.anchor_ttl {self.reaper.anchor_ttl} exceeds the deployment TTL limit {max_ttl}")

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

    def trial_settings(self, *, extra_labels: Mapping[str, str] | None = None) -> TrialSettings:
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
            extra_labels=dict(extra_labels or {}),
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


__all__ = ["SECURITY_FIELDS", "ConfigError", "FlotillaConfig"]


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

    def profile(self, settings: OpenSandbox, cfg: FlotillaConfig) -> str:
        # Fixed field list: existing capability reports record this exact digest. management_extensions
        # affect only workspace anchors, not the task/probe runtime this evidence describes.
        data = [
            settings.filesystem_protocol,
            settings.extensions,
            settings.privileged_extensions,
            settings.create_fields,
        ]
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

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
