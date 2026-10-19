"""Deployment capability evidence and startup requirements."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flotilla.platform.base import Capabilities

SCHEMA_VERSION = 1

Source = Literal["probed", "declared"]
ExternalForm = Literal["none", "any", "allowlist"]
Device = Literal["fuse", "kvm"]
#: Security capabilities: when false, provider `start` refuses by default; each must be accepted in `accept_insecure`.
SECURITY_FIELDS = ("inbound_isolation", "exec_auth", "no_auto_mount")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Item(_Model):
    """Verdict for one capability. `evidence` (output summary, timing, doc reference) is informational only."""

    ok: bool
    source: Source
    evidence: str = ""


class Lifecycle(_Model):
    """Lifecycle. `list_visibility_s` is declared by the deployment and rechecked by probe (max delay in evidence)."""

    create_get_delete: Item
    label_list: Item
    renew: Item
    max_label_value_len: Annotated[int, Field(ge=63)]
    list_visibility_s: Annotated[float, Field(gt=0)]
    max_ttl_seconds: Annotated[int, Field(gt=0)] | None


class Exec(_Model):
    """Exec, files and background processes."""

    exec_identity: Item  # argv, uid / gid, cwd, env, timeout
    files: Item  # upload, download, /etc/hosts writable and not rewritten
    placeholder_entry: Item  # placeholder entrypoint
    background: Item  # background processes


class Network(_Model):
    """Links, isolation, egress and internal addressing."""

    link: Item  # TCP link between members
    link_udp: Item  # UDP over links
    link_max_members: Annotated[int, Field(ge=1)]
    inbound_isolation: Item  # security
    external_forms: frozenset[ExternalForm]  # supported egress forms
    external_none: Item  # required
    internal_address: Literal["api", "exec", "none"]  # how the internal address is obtained
    implicit_egress: tuple[str, ...]  # implicit egress declared by the deployment (target:port/proto)
    implicit_egress_declared: Item  # required: declaration is complete


class Storage(_Model):
    """Volumes and mounts."""

    read_only: Item  # required
    shared_volume: Item  # feature
    subdir_isolation: Item  # required
    missing_subpath: Item  # degraded
    no_auto_mount: Item  # security
    dir_management: Item  # required
    volume_file: Item  # feature


class Runtime(_Model):
    """Exec auth, resource limits, diagnostics, privileges, images and multi-container."""

    exec_auth: Item  # security
    memory_limit: Item  # required: memory limit equals the requested value
    cpu_limit: Item  # degraded: CPU limit >= requested; actual ratio in evidence and cpu_limit_ratio
    cpu_limit_ratio: Annotated[float, Field(gt=0)] | None  # actual CPU limit / requested; None if unreadable
    diagnostics: Item  # degraded
    privileged_runtime: Item  # privileged containers
    devices: frozenset[Device]  # devices exposed when privileged
    image_digest: Item  # required
    multi_container: Item  # recorded only; not supported by the current interface


class CapabilityReport(_Model):
    schema_: Annotated[int, Field(alias="schema")] = SCHEMA_VERSION
    deployment: str  # human-readable deployment name
    endpoint: str  # lifecycle API base URL; checked against config at provider `start`
    probed_at: datetime
    probe_version: str  # flotilla version
    lifecycle: Lifecycle
    exec: Exec
    network: Network
    storage: Storage
    runtime: Runtime
    notes: tuple[str, ...] = ()

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _consistent(self) -> CapabilityReport:
        if "none" not in self.network.external_forms and self.network.external_none.ok:
            raise ValueError("external_forms must include none when external_none.ok is true")
        if self.network.internal_address == "none" and self.network.link.ok:
            raise ValueError(
                "link cannot be true without an internal address (internal_address = none); links require one"
            )
        return self

    def missing_required(self) -> list[str]:
        """Unmet required capabilities. If non-empty the report is "unusable" and provider `start` refuses."""
        lc, ex, net, st, rt = self.lifecycle, self.exec, self.network, self.storage, self.runtime
        required = {
            "lifecycle create / get / delete": lc.create_get_delete,
            "lifecycle list by label": lc.label_list,
            "lifecycle renew": lc.renew,
            "exec": ex.exec_identity,
            "files": ex.files,
            "placeholder entrypoint": ex.placeholder_entry,
            "background processes": ex.background,
            "egress none": net.external_none,
            "read-only mounts": st.read_only,
            "subdirectory isolation": st.subdir_isolation,
            "directory management": st.dir_management,
            "memory limit equals requested value": rt.memory_limit,
            "implicit egress declaration": net.implicit_egress_declared,
            "pull by digest": rt.image_digest,
        }
        missing = [name for name, item in required.items() if not item.ok]
        if not ({"any", "allowlist"} & net.external_forms):
            missing.append("egress any or allowlist")
        return missing

    def security_gaps(self) -> list[str]:
        """Security capabilities that are false (subset of `SECURITY_FIELDS`); must be accepted explicitly in config."""
        values = {
            "inbound_isolation": self.network.inbound_isolation.ok,
            "exec_auth": self.runtime.exec_auth.ok,
            "no_auto_mount": self.storage.no_auto_mount.ok,
        }
        return [name for name in SECURITY_FIELDS if not values[name]]

    def to_capabilities(self) -> Capabilities:
        net = self.network
        return Capabilities(
            max_label_value_len=self.lifecycle.max_label_value_len,
            list_visibility_s=self.lifecycle.list_visibility_s,
            max_ttl_seconds=self.lifecycle.max_ttl_seconds,
            link=net.link.ok,
            link_udp=net.link.ok and net.link_udp.ok,
            link_max_members=net.link_max_members,
            inbound_isolation=net.inbound_isolation.ok,
            external_forms=frozenset(net.external_forms),
            implicit_egress=net.implicit_egress,
            internal_address=net.internal_address,
            shared_volume=self.storage.shared_volume.ok,
            volume_file=self.storage.volume_file.ok,
            no_auto_mount=self.storage.no_auto_mount.ok,
            exec_auth=self.runtime.exec_auth.ok,
            privileged_runtime=self.runtime.privileged_runtime.ok,
            devices=frozenset(self.runtime.devices) if self.runtime.privileged_runtime.ok else frozenset(),
        )

    def dump_json(self) -> str:
        return self.model_dump_json(by_alias=True, indent=2) + "\n"

    @classmethod
    def load_json(cls, text: str | bytes) -> CapabilityReport:
        report = cls.model_validate_json(text)
        if report.schema_ != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported capability report schema version {report.schema_} (expected {SCHEMA_VERSION})"
            )
        return report
