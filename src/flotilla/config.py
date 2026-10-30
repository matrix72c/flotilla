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
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from flotilla.backends import BUILTIN, Backend, BackendSettings, load_backend
from flotilla.build.images import BuildSettings
from flotilla.capabilities import SECURITY_FIELDS, CapabilityReport
from flotilla.compose.resources import cpu, memory
from flotilla.core.anchor import AnchorSettings
from flotilla.core.reaper import ReaperSettings
from flotilla.core.runtime import MonitorSettings
from flotilla.core.trial import TrialSettings
from flotilla.manifest import Manifest
from flotilla.platform.base import Capabilities, Clock, ExternalPolicy, Platform, Resources
from flotilla.platform.opensandbox import ExecdSettings, NetworkSettings, StorageSettings
from flotilla.platform.opensandbox import build as build_opensandbox
from flotilla.platform.opensandbox.filesystem import FilesystemProtocol, FilesystemSettings

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
    network: OpenSandboxNetwork = OpenSandboxNetwork()
    execd: OpenSandboxExecd = OpenSandboxExecd()
    filesystem_protocol: FilesystemProtocol = "upstream"
    create_fields: dict[str, Any] = Field(default_factory=dict)
    group_extension: str | None = (
        None  # `extensions` key for the placement group; unset if the deployment lacks group scheduling
    )


class Build(_Model):
    """Dataset builder config `[build]`, used by the automatic preparation build subprocess."""

    pullable_registries: tuple[str, ...] = ()
    mirror_images: bool = False
    target: str = ""
    image_replacements: dict[str, str] = {}

    def settings(self) -> BuildSettings:
        return BuildSettings(
            pullable_registries=self.pullable_registries,
            mirror_images=self.mirror_images,
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
    release: str
    release_sha256: str

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


class Runtime(_Model):
    check_timeout_s: PositiveFloat = 60.0
    run_timeout_s: PositiveFloat = 21600.0
    exec_default_timeout_s: PositiveFloat = 300.0
    cancel_confirm_timeout_s: PositiveFloat = 15.0
    exec_output_limit_bytes: PositiveInt = 8 * 1024 * 1024


class Timeouts(_Model):
    create: PositiveFloat = 600.0
    address: PositiveFloat = 30.0
    wire: PositiveFloat = 60.0
    hosts: PositiveFloat = 30.0
    prepare: PositiveFloat = 600.0
    start_max: PositiveFloat = 1800.0
    close: PositiveFloat = 60.0


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


class Preparation(_Model):
    """Automatic preparation for each open; reuse is owned by Docker/BuildKit."""

    timeout_s: PositiveFloat = 3600
    concurrency: PositiveInt = 4
    docker_command: tuple[str, ...] = ("docker",)

    @field_validator("docker_command")
    @classmethod
    def _command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _argv(value)


class Prepare(_Model):
    """Offline `flotilla prepare`: image builds/derivations and task-file publication."""

    concurrency: PositiveInt = 4
    docker_command: tuple[str, ...] = ("docker",)

    @field_validator("docker_command")
    @classmethod
    def _command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _argv(value)


def _argv(value: tuple[str, ...]) -> tuple[str, ...]:
    if not value or any(not part for part in value):
        raise ValueError("docker_command must be a nonempty argv")
    return value


class FlotillaConfig(_Model):
    backend: str = BUILTIN
    opensandbox: OpenSandbox | None = None  # built-in backend table
    backends: dict[str, dict[str, Any]] = {}  # third-party backend tables, validated by the selected backend
    storage: Storage
    build: Build = Build()
    preparation: Preparation = Preparation()
    prepare: Prepare = Prepare()
    share: Share
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

    @model_validator(mode="after")
    def _local(self) -> FlotillaConfig:
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
        return data

    def capabilities_path(self) -> Path:
        path = Path(self.backend_settings.capabilities)
        if not path.is_absolute() and self.base_dir is not None:
            path = self.base_dir / path
        return path

    def load_report(self) -> CapabilityReport:
        return CapabilityReport.load_json(self.capabilities_path().read_bytes())

    def platform_capabilities(self, report: CapabilityReport | None = None) -> Capabilities:
        """Combine recorded evidence with explicitly configured backend enforcement, retaining the report."""
        report = report or self.load_report()
        caps = report.to_capabilities()
        matched = report.filesystem_profile == self.filesystem_profile()
        caps = replace(
            caps,
            readonly_rootfs=caps.readonly_rootfs and matched,
            native_tmpfs=caps.native_tmpfs and matched,
            shm_size=caps.shm_size and matched,
        )
        return load_backend(self.backend).capabilities(self.backend_settings, self, caps)

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

        visibility = report.lifecycle.list_visibility_s
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

    def anchor_settings(self) -> AnchorSettings:
        if not self.reaper.anchor_image:
            raise ConfigError(["reaper.anchor_image is not set"])
        return AnchorSettings(
            image=self.reaper.anchor_image,
            ttl_s=self.reaper.anchor_ttl,
            renew_interval_s=self.reaper.renew_interval,
            grace_s=self.reaper.grace,
        )

    def reaper_settings(self) -> ReaperSettings:
        return ReaperSettings(
            create_rate=self.limits.create_rate,
            create_concurrency=self.limits.create_concurrency,
            create_request_timeout_s=self.timeouts.create,
            close_timeout_s=self.timeouts.close,
        )

    def trial_settings(self, *, extra_labels: Mapping[str, str] | None = None) -> TrialSettings:
        t = self.timeouts
        return TrialSettings(
            share_release=self.share.release,
            external=self.network.policy(),
            ttl_s=self.reaper.trial_ttl,
            create_timeout_s=t.create,
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
        caps = replace(caps, sandbox_ingress=settings.network.ingress == "sandbox")
        return FilesystemSettings(settings.filesystem_protocol).capabilities(caps)

    def profile(self, settings: OpenSandbox, cfg: FlotillaConfig) -> str:
        # Fixed field list: existing capability reports record this exact digest.
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
            caps=caps,
            execd=cfg.execd_settings(),
            filesystem=cfg.filesystem_settings(),
            command_gate=command_gate,
            extensions=settings.extensions,
            create_fields=settings.create_fields,
            group_extension=settings.group_extension,
            privileged_extensions=settings.privileged_extensions,
            create_timeout_s=cfg.timeouts.create,
        )
