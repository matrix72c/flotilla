"""Versioned, measured deployment evidence; configuration never grants capabilities."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from flotilla.platform.base import Capabilities
from flotilla.platform.network import cluster_dns_address

SCHEMA_VERSION: Literal[2] = 2
ExternalForm = Literal["none", "any", "allowlist"]
Device = Literal["fuse", "kvm"]
SecurityProperty = Literal["inbound_isolation", "exec_auth", "no_auto_mount"]
SECURITY_FIELDS: tuple[SecurityProperty, ...] = ("inbound_isolation", "exec_auth", "no_auto_mount")


@dataclass(frozen=True)
class ExternalTarget:
    """A content-bearing external control, separate from workload egress policy."""

    host: str
    port: int
    protocol: Literal["tcp", "udp"] = "tcp"
    service: Literal["echo", "https", "ntp"] = "echo"

    def __post_init__(self) -> None:
        if not self.host or any(char.isspace() for char in self.host):
            raise ValueError("probe target host must be nonempty and contain no whitespace")
        if not 1 <= self.port <= 65535 or self.protocol not in ("tcp", "udp"):
            raise ValueError("invalid probe target port or protocol")
        if self.service not in ("echo", "https", "ntp"):
            raise ValueError("unknown probe target service")
        if self.service == "https" and self.protocol != "tcp":
            raise ValueError("https probe service requires tcp")
        if self.service == "ntp" and self.protocol != "udp":
            raise ValueError("ntp probe service requires udp")


@dataclass(frozen=True)
class ProbeSettings:
    image: str
    share_release: str
    endpoint: str
    evidence_profile: str
    deployment: str = "deployment"
    privileged_image: str = ""
    inner_image: str = ""
    link_members: int = 9
    external_targets: tuple[ExternalTarget | tuple[str, int, str], ...] = ()
    dns_name: str = ""
    create_timeout_s: float = 300.0
    hosts_recheck_s: float = 600.0
    ttl_s: int = 1800
    anchor_ttl_s: int = 600
    list_visibility_s: float = 30.0
    total_timeout_s: float = 3600.0
    cleanup_timeout_s: float = 180.0
    filesystem_supported: bool = False
    test_external_any: bool = False

    def __post_init__(self) -> None:
        import math

        # Retain the original direct ProbeSettings tuple API while keeping the runner typed.
        object.__setattr__(
            self,
            "external_targets",
            tuple(
                target
                if isinstance(target, ExternalTarget)
                else ExternalTarget(target[0], target[1], cast(Literal["tcp", "udp"], target[2]))
                for target in self.external_targets
            ),
        )
        if not 2 <= self.link_members <= 32:
            raise ValueError("probe link_members must be between 2 and 32")
        budgets = (
            self.create_timeout_s,
            self.ttl_s,
            self.anchor_ttl_s,
            self.list_visibility_s,
            self.total_timeout_s,
            self.cleanup_timeout_s,
        )
        if any(not math.isfinite(value) or value <= 0 for value in budgets):
            raise ValueError("probe budgets and TTLs must be finite and positive")
        if not math.isfinite(self.hosts_recheck_s) or self.hosts_recheck_s < 0:
            raise ValueError("hosts_recheck_s must be finite and nonnegative")

    @property
    def targets(self) -> tuple[ExternalTarget, ...]:
        """Typed controls; the frozen instance normalizes legacy inputs in __post_init__."""
        return cast(tuple[ExternalTarget, ...], self.external_targets)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Item(_Model):
    """One executed check. Only passed enables its recorded scope; no manual source."""

    status: Literal["passed", "failed", "skipped", "error"]
    evidence: str = ""
    measurements: dict[str, Any] = Field(default_factory=dict)
    scope: str = "ordinary"

    @property
    def ok(self) -> bool:
        return self.status == "passed"

    def passed_for(self, scope: str) -> bool:
        return self.ok and self.scope == scope


UNMEASURED = Item(status="skipped", evidence="not measured")


class Lifecycle(_Model):
    create_get_delete: Item = UNMEASURED
    label_list: Item = UNMEASURED
    renew: Item = UNMEASURED
    anchor_ttl: Item = UNMEASURED
    trial_ttl: Item = UNMEASURED
    max_label_value_len: Annotated[int, Field(ge=63)] = 63
    # Configured waiting budget, not an inferred platform guarantee.
    list_visibility_s: Annotated[float, Field(gt=0)] = 30.0
    max_ttl_seconds: Annotated[int, Field(gt=0)] | None = None


class Exec(_Model):
    prerequisites: Item = UNMEASURED
    exec_identity: Item = UNMEASURED
    files: Item = UNMEASURED
    placeholder_entry: Item = UNMEASURED
    background: Item = UNMEASURED


class Network(_Model):
    link: Item = UNMEASURED
    link_udp: Item = UNMEASURED
    link_max_members: Annotated[int, Field(ge=1)] = 1
    inbound_isolation: Item = UNMEASURED
    external_none: Item = UNMEASURED
    external_allowlist: Item = UNMEASURED
    external_any: Item = UNMEASURED
    dns: Item = UNMEASURED
    resolver_non_dns: Item = UNMEASURED  # Optional resolver-IP controls, not a general egress prerequisite.
    network_acceptance: Item = UNMEASURED
    internal_address: Literal["api", "exec", "none"] = "none"
    implicit_egress: tuple[str, ...] = ()  # observations, never an exhaustive declaration
    cluster_dns: tuple[str, ...] = ()

    @field_validator("cluster_dns")
    @classmethod
    def _cluster_dns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(cluster_dns_address(ip) for ip in value)

    @property
    def external_forms(self) -> frozenset[ExternalForm]:
        checks: tuple[tuple[ExternalForm, Item], ...] = (
            ("none", self.external_none),
            ("allowlist", self.external_allowlist),
            ("any", self.external_any),
        )
        return frozenset(name for name, check in checks if check.ok)


class Storage(_Model):
    read_only: Item = UNMEASURED
    shared_volume: Item = UNMEASURED
    subdir_isolation: Item = UNMEASURED
    missing_subpath: Item = UNMEASURED
    no_auto_mount: Item = UNMEASURED
    dir_management: Item = UNMEASURED
    volume_file: Item = UNMEASURED
    native_tmpfs: Item = UNMEASURED
    shm_size: Item = UNMEASURED


class Runtime(_Model):
    exec_auth: Item = UNMEASURED
    memory_limit: Item = UNMEASURED
    cpu_limit: Item = UNMEASURED
    cpu_limit_ratio: Annotated[float, Field(gt=0)] | None = None
    privileged_runtime: Item = UNMEASURED
    privileged_storage: Item = UNMEASURED
    privileged_no_auto_mount: Item = UNMEASURED
    privileged_exec_auth: Item = UNMEASURED
    privileged_network: Item = UNMEASURED
    privileged_inbound_isolation: Item = UNMEASURED
    fuse: Item = UNMEASURED
    kvm: Item = UNMEASURED
    image_digest: Item = UNMEASURED
    readonly_rootfs: Item = UNMEASURED


class CapabilityReport(_Model):
    schema_: Literal[2] = Field(default=SCHEMA_VERSION, alias="schema")
    deployment: str
    endpoint: str
    probed_at: datetime
    probe_version: str
    evidence_profile: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    lifecycle: Lifecycle = Lifecycle()
    exec: Exec = Exec()
    network: Network = Network()
    storage: Storage = Storage()
    runtime: Runtime = Runtime()
    notes: tuple[str, ...] = ()
    cleanup: Item = UNMEASURED

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    def missing_required(self) -> list[str]:
        lc, ex, net, st, rt = self.lifecycle, self.exec, self.network, self.storage, self.runtime
        required = {
            "lifecycle create / get / delete": lc.create_get_delete,
            "lifecycle list by label": lc.label_list,
            "lifecycle renew": lc.renew,
            "anchor TTL": lc.anchor_ttl,
            "trial TTL": lc.trial_ttl,
            "exec prerequisites": ex.prerequisites,
            "exec": ex.exec_identity,
            "files": ex.files,
            "placeholder entrypoint": ex.placeholder_entry,
            "background processes": ex.background,
            "egress none": net.external_none,
            "network acceptance": net.network_acceptance,
            "read-only mounts": st.read_only,
            "subdirectory isolation": st.subdir_isolation,
            "directory management": st.dir_management,
            "memory limit equals requested value": rt.memory_limit,
            "pull by digest": rt.image_digest,
        }
        missing = [name for name, item in required.items() if not item.ok]
        if not ({"any", "allowlist"} & net.external_forms):
            missing.append("egress any or allowlist")
        return missing

    def security_gaps(self) -> list[SecurityProperty]:
        values = {
            "inbound_isolation": self.network.inbound_isolation.ok,
            "exec_auth": self.runtime.exec_auth.ok,
            "no_auto_mount": self.storage.no_auto_mount.ok,
        }
        return [name for name in SECURITY_FIELDS if not values[name]]

    def to_capabilities(self) -> Capabilities:
        net, st, rt = self.network, self.storage, self.runtime
        # Empty observations are NOT evidence that all non-DNS egress is blocked.
        forms = net.external_forms
        if not net.network_acceptance.ok:
            forms = forms - {"none"}
        privileged = (
            rt.privileged_runtime.passed_for("privileged")
            and rt.privileged_storage.passed_for("privileged")
            and rt.privileged_no_auto_mount.passed_for("privileged")
            and rt.privileged_exec_auth.passed_for("privileged")
            and rt.privileged_network.passed_for("privileged")
            and rt.privileged_inbound_isolation.passed_for("privileged")
        )
        return Capabilities(
            max_label_value_len=self.lifecycle.max_label_value_len,
            list_visibility_s=self.lifecycle.list_visibility_s,
            max_ttl_seconds=self.lifecycle.max_ttl_seconds,
            link=net.link.ok and net.internal_address != "none",
            link_udp=net.link.ok and net.link_udp.ok and net.internal_address != "none",
            link_max_members=net.link_max_members,
            inbound_isolation=net.inbound_isolation.ok,
            sandbox_ingress=net.inbound_isolation.ok,
            external_forms=frozenset(forms),
            implicit_egress=net.implicit_egress,
            cluster_dns=net.cluster_dns,
            internal_address=net.internal_address,
            shared_volume=st.shared_volume.ok,
            volume_file=st.volume_file.ok,
            no_auto_mount=st.no_auto_mount.ok,
            exec_auth=rt.exec_auth.ok,
            privileged_runtime=privileged,
            devices=frozenset(
                name
                for name, item in (("fuse", rt.fuse), ("kvm", rt.kvm))
                if privileged and item.ok and item.scope == "privileged"
            ),
            readonly_rootfs=rt.readonly_rootfs.ok,
            native_tmpfs=st.native_tmpfs.ok,
            shm_size=st.shm_size.ok,
        )

    def dump_json(self) -> str:
        return self.model_dump_json(by_alias=True, indent=2) + "\n"

    def write_json(self, path: str | Path) -> None:
        """Atomically replace a candidate report; never leave a partially written JSON."""
        path = Path(path)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(self.dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @classmethod
    def load_json(cls, text: str | bytes) -> CapabilityReport:
        data = json.loads(text)
        if data.get("schema") != SCHEMA_VERSION:
            raise ValueError(
                "unsupported capability report schema; rerun probe (legacy/manual evidence is not migrated)"
            )
        return cls.model_validate(data)
