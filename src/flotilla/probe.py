"""Measure implemented deployment contracts; preserve unmeasured items as declarations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flotilla import __version__
from flotilla.capabilities import (
    CapabilityReport,
    Device,
    Exec,
    ExternalForm,
    Item,
    Lifecycle,
    Network,
    Runtime,
    Storage,
)
from flotilla.core import labels
from flotilla.core.anchor import Anchor
from flotilla.core.waiting import wait_running, within
from flotilla.platform.base import (
    Capabilities,
    Clock,
    ErrorCategory,
    ExternalPolicy,
    FlotillaError,
    InstanceHandle,
    InstanceSpec,
    Platform,
    ProcessSpec,
    Resources,
    SharedVolume,
)

PROBE_LABEL = "flotilla/probe"  # run id; with the launch and unit labels it identifies a probed instance
_EXEC_TIMEOUT_S = 30.0
_MEMORY = "512Mi"
_MEMORY_BYTES = 512 * 1024 * 1024
_PUBLIC_IP = "1.1.1.1"  # egress none: a public address outside any deployment
_NONE = ExternalPolicy(mode="none")
_INBOUND_PORT = 48080  # port the member listens on
_INBOUND_TOKEN = "__flotilla_inbound__"  # listener's reply; also what the connector sends
_PATH = "/usr/local/bin:/usr/bin:/bin"


class Declared(BaseModel):
    """Deployer declarations: items that probe does not measure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deployment: str
    max_ttl_seconds: Annotated[int, Field(gt=0)] | None = None
    list_visibility_s: Annotated[float, Field(gt=0)]  # declared deadline; measured visibility delay must not exceed it
    link: Item
    link_udp: Item
    link_max_members: Annotated[int, Field(ge=1)] = 1
    inbound_isolation: Item
    external_forms: frozenset[ExternalForm]
    implicit_egress: tuple[str, ...]
    implicit_egress_declared: Item
    shared_volume: Item
    missing_subpath: Item
    no_auto_mount: Item
    volume_file: Item
    exec_auth: Item
    diagnostics: Item
    privileged_runtime: Item
    devices: frozenset[Device] = frozenset()
    multi_container: Item
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> Declared:
        """Check report consistency before probing, so a bad declaration is not found after a 10+ minute run."""
        if "none" not in self.external_forms:
            raise ValueError("external_forms must include none: probe always tests none (a required capability)")
        if any(d.source != "declared" for d in self._declared_items()):
            raise ValueError("items in the declared file must have source declared")
        return self

    def _declared_items(self) -> tuple[Item, ...]:
        return (
            self.link,
            self.link_udp,
            self.inbound_isolation,
            self.implicit_egress_declared,
            self.shared_volume,
            self.missing_subpath,
            self.no_auto_mount,
            self.volume_file,
            self.exec_auth,
            self.diagnostics,
            self.privileged_runtime,
            self.multi_container,
        )

    def bootstrap_capabilities(self) -> Capabilities:
        """`Capabilities` for the backend during probe: no report yet, so use declarations and the most conservative
        values for measured items.

        probe itself only uses `list_visibility_s` (the anchor waits to appear in listings) and the label length.
        """
        return Capabilities(
            max_label_value_len=63,
            list_visibility_s=self.list_visibility_s,
            max_ttl_seconds=self.max_ttl_seconds,
            link=False,
            link_udp=False,
            link_max_members=1,
            inbound_isolation=False,
            external_forms=frozenset({"none"}),
            implicit_egress=self.implicit_egress,
            internal_address="exec",
            shared_volume=False,
            volume_file=False,
            no_auto_mount=False,
            exec_auth=False,
            privileged_runtime=False,
            devices=frozenset(),
        )


@dataclass(frozen=True)
class ProbeSettings:
    #: Image under test (repo@sha256:...); needs /bin/sh, id, pwd, stat, cat, ls, touch, awk, timeout, nc (`-z`).
    image: str
    share_release: str  # each unit mounts share read-only like real units (the exec wrapper needs its busybox)
    endpoint: str  # written to the report; checked against config at provider `start`
    create_timeout_s: float = 300.0
    #: Delay before re-reading /etc/hosts. Acceptance runs use 10 minutes; routine probes may use less.
    hosts_recheck_s: float = 30.0
    ttl_s: int = 900


async def probe(
    platform: Platform,
    clock: Clock,
    anchor: Anchor,
    declared: Declared,
    settings: ProbeSettings,
) -> CapabilityReport:
    """Measure, merge declarations, return the report. `anchor` must be started; instances are deleted on exit."""
    run = _Run(platform, clock, anchor, settings, run_id=f"p{uuid.uuid4().hex[:10]}")
    try:
        lifecycle = await run.lifecycle(declared.list_visibility_s, declared.max_ttl_seconds)
        unit, placeholder = await run.create_unit()
        digest = _probed(
            placeholder.ok and "@sha256:" in settings.image,
            f"create with {settings.image} "
            + ("succeeded" if placeholder.ok else "failed")
            + ("" if "@sha256:" in settings.image else "; image is not repo@sha256:..., digest pull not verified"),
        )
        exec_items = await run.exec_items(unit)
        address = await run.address(unit)
        memory_limit, cpu_limit, cpu_ratio = await run.resources(unit)
        external_none = await run.external_none(unit)
        inbound = await run.inbound(declared.inbound_isolation)
        storage = await run.storage()
    finally:
        leaked = await run.cleanup()
    if leaked:  # labeled flotilla/probe with finite TTL, so platform TTL reclaims them; report notes it
        notes_extra: tuple[str, ...] = (f"probed instances not confirmed deleted (left to platform TTL): {leaked}",)
    else:
        notes_extra = ()

    return CapabilityReport(
        deployment=declared.deployment,
        endpoint=settings.endpoint,
        probed_at=clock.now(),
        probe_version=__version__,
        lifecycle=Lifecycle(
            create_get_delete=lifecycle["create_get_delete"],
            label_list=lifecycle["label_list"],
            renew=lifecycle["renew"],
            max_label_value_len=63,
            list_visibility_s=declared.list_visibility_s,
            max_ttl_seconds=declared.max_ttl_seconds,
        ),
        exec=Exec(
            exec_identity=exec_items["exec_identity"],
            files=exec_items["files"],
            placeholder_entry=placeholder,
            background=exec_items["background"],
        ),
        network=Network(
            link=declared.link,
            link_udp=declared.link_udp,
            link_max_members=declared.link_max_members,
            inbound_isolation=inbound,
            external_forms=declared.external_forms,
            external_none=external_none,
            internal_address="exec" if address.ok else "none",  # no address: report validation rejects link = True
            implicit_egress=declared.implicit_egress,
            implicit_egress_declared=declared.implicit_egress_declared,
        ),
        storage=Storage(
            read_only=storage["read_only"],
            shared_volume=declared.shared_volume,
            subdir_isolation=storage["subdir_isolation"],
            missing_subpath=declared.missing_subpath,
            no_auto_mount=declared.no_auto_mount,
            dir_management=storage["dir_management"],
            volume_file=declared.volume_file,
        ),
        runtime=Runtime(
            exec_auth=declared.exec_auth,
            memory_limit=memory_limit,
            cpu_limit=cpu_limit,
            cpu_limit_ratio=cpu_ratio,
            diagnostics=declared.diagnostics,
            privileged_runtime=declared.privileged_runtime,
            devices=declared.devices,
            image_digest=digest,
            multi_container=declared.multi_container,
        ),
        notes=(*declared.notes, f"probe run {run.run_id}; internal address: {address.evidence}", *notes_extra),
    )


def _cpu_limit(fields: list[str]) -> float | None:
    """cgroup CPU limit in cores: v2 `cpu.max` is "quota period", v1 has quota and period files; `max` / -1 = none."""
    if len(fields) != 2 or fields[0] in ("max", "-1"):
        return None
    try:
        return int(fields[0]) / int(fields[1])
    except (ValueError, ZeroDivisionError):
        return None


def _mounted_subdir(entry: str, subdir: str) -> bool:
    """Whether mountinfo "source|root" mounts `subdir`: the source, or source joined with root, ends with it."""
    source, _, root = entry.partition("|")
    joined = source.rstrip("/") + ("" if root in ("", "/") else root)
    return any(path.rstrip("/").endswith("/" + subdir) for path in (source, joined))


def _probed(ok: bool, evidence: str) -> Item:
    return Item(ok=ok, source="probed", evidence=evidence)


def _sh(
    script: str, *, uid: int = 0, gid: int = 0, cwd: str = "/", timeout_s: float = _EXEC_TIMEOUT_S, **env: str
) -> ProcessSpec:
    return ProcessSpec(
        argv=("/bin/sh", "-c", script), uid=uid, gid=gid, cwd=cwd, env={"PATH": _PATH, **env}, timeout_s=timeout_s
    )


@dataclass
class _Run:
    platform: Platform
    clock: Clock
    anchor: Anchor
    settings: ProbeSettings
    run_id: str

    def __post_init__(self) -> None:
        self._created: list[str] = []

    def _spec(
        self,
        name: str,
        volumes: tuple[SharedVolume, ...] = (),
        *,
        external: ExternalPolicy = _NONE,
        group: str | None = None,
    ) -> InstanceSpec:
        share = SharedVolume(
            key=f"share/releases/{self.settings.share_release}", mount_path="/.flotilla", read_only=True
        )
        return InstanceSpec(
            image=self.settings.image,
            entrypoint=("/.flotilla/bin/busybox", "sleep", "2147483647"),
            labels={labels.LAUNCH: self.anchor.launch_id, PROBE_LABEL: self.run_id, labels.UNIT: name},
            timeout_seconds=self.settings.ttl_s,
            resources=Resources(cpu="1", memory=_MEMORY),
            volumes=(share, *volumes),
            external=external,
            group=group,
        )

    async def _create(self, spec: InstanceSpec, *, wait: bool = True) -> InstanceHandle:
        handle = await self.platform.create(spec)
        self._created.append(handle.iid)
        if wait:
            await self._wait_running(handle)
        return handle

    async def _wait_running(self, handle: InstanceHandle) -> None:
        running = wait_running(self.platform, self.clock, handle.iid, stage="create")
        await within(self.clock, self.settings.create_timeout_s, running)

    async def _delete(self, iid: str, *, timeout_s: float = 120.0) -> bool:
        """Delete and confirm `not_found` via `get`. Returns whether it was confirmed in time."""
        await self.platform.delete(iid)
        began = self.clock.monotonic()
        while self.clock.monotonic() - began < timeout_s:
            try:
                await self.platform.get(iid)
            except FlotillaError as exc:
                if exc.category is ErrorCategory.NOT_FOUND:
                    if iid in self._created:
                        self._created.remove(iid)
                    return True
                raise
            await self.clock.sleep(2.0)
        return False

    async def cleanup(self) -> list[str]:
        """Delete all probed instances; one failure neither stops the rest nor masks the caller's error.

        Returns the iids not confirmed deleted.
        """
        leaked = []
        for iid in list(self._created):
            try:
                if not await self._delete(iid):
                    leaked.append(iid)
            except FlotillaError:
                leaked.append(iid)
        return leaked

    async def lifecycle(self, declared_visibility_s: float, max_ttl_s: int | None) -> dict[str, Item]:
        # Visibility is timed from the create request: list right after create returns, without waiting for Running.
        sent = self.clock.monotonic()
        handle = await self._create(self._spec("lifecycle"), wait=False)
        seen_after = await self._listed_after(handle.iid, sent, declared_visibility_s)
        await self._wait_running(handle)
        expires = (await self.platform.get(handle.iid)).expires_at
        label_item = _probed(
            seen_after is not None and seen_after <= declared_visibility_s,
            f"listed by label {seen_after:.1f}s after create request (declared limit {declared_visibility_s}s)"
            if seen_after is not None
            else f"did not appear in label listing within declared limit {declared_visibility_s}s",
        )

        extend = self.settings.ttl_s + 600 if max_ttl_s is None else min(self.settings.ttl_s + 600, max_ttl_s)
        target = self.clock.now() + timedelta(seconds=extend)
        await self.platform.renew(handle.iid, target)
        renewed = (await self.platform.get(handle.iid)).expires_at
        renew_ok = renewed is not None and abs((renewed - target).total_seconds()) < 5
        renew_item = _probed(renew_ok, f"renewed to {target.isoformat()}, get returned {renewed}")

        gone = await self._delete(handle.iid)
        await self.platform.delete(f"flotilla-probe-missing-{uuid.uuid4().hex[:8]}")  # a missing id must not raise
        crud_item = _probed(
            expires is not None and gone,
            f"get returned expiry {expires}; after delete {'not_found confirmed' if gone else 'still visible'}; "
            "deleting a missing id succeeded",
        )
        return {"create_get_delete": crud_item, "label_list": label_item, "renew": renew_item}

    async def _listed_after(self, iid: str, sent: float, limit_s: float) -> float | None:
        """List by label every second from `sent`; return the delay until first seen, or None after `limit_s`."""
        query = {PROBE_LABEL: self.run_id, labels.UNIT: "lifecycle"}
        while True:
            if any(s.iid == iid for s in await self.platform.list(query)):
                return self.clock.monotonic() - sent
            if self.clock.monotonic() - sent > limit_s:
                return None
            await self.clock.sleep(1.0)

    async def create_unit(self) -> tuple[InstanceHandle, Item]:
        """Create from a digest image with a placeholder entrypoint; passes if a command can be executed."""
        handle = await self._create(self._spec("unit"))
        result = await self.platform.exec(handle, _sh("true"))
        ok = result.exit_code == 0
        return handle, _probed(
            ok, f"{self.settings.image} created with placeholder entrypoint; true exited {result.exit_code}"
        )

    async def exec_items(self, unit: InstanceHandle) -> dict[str, Item]:
        p = self.platform
        ident = await p.exec(
            unit, _sh('id -u; id -g; pwd; echo "$PROBE_VAR"', uid=1000, gid=1000, cwd="/tmp", PROBE_VAR="x y")
        )
        got = ident.stdout.decode().splitlines()[:4]
        slow = await p.exec(unit, _sh("sleep 30", timeout_s=2.0))
        exec_item = _probed(
            ident.exit_code == 0 and got == ["1000", "1000", "/tmp", "x y"] and slow.timed_out,
            f"exec as 1000:1000 in /tmp read back {got}; sleep 30 with 2s timeout "
            + ("was killed" if slow.timed_out else f"was not killed (exit code {slow.exit_code})"),
        )

        original = await p.read_file(unit, "/etc/hosts")
        marker = f"# flotilla-probe {self.run_id}\n".encode()
        await p.write_file(unit, "/etc/hosts", original + marker, mode=0o644, uid=0, gid=0)
        first = await p.read_file(unit, "/etc/hosts")
        await self.clock.sleep(self.settings.hosts_recheck_s)
        later = await p.read_file(unit, "/etc/hosts")
        await p.write_file(unit, "/tmp/probe-owned", b"owned\n", mode=0o640, uid=1000, gid=1000)
        owned = (await p.exec(unit, _sh("stat -c '%u:%g %a' /tmp/probe-owned; cat /tmp/probe-owned"))).stdout
        owned_ok = owned.decode().split() == ["1000:1000", "640", "owned"]
        hosts_ok = first == original + marker and later == first
        files_item = _probed(
            hosts_ok and owned_ok,
            f"/etc/hosts read-back after write {'matched' if first == original + marker else 'mismatched'}, "
            f"{'not rewritten' if later == first else 'rewritten'} after {self.settings.hosts_recheck_s:.0f}s; "
            f"file written as 1000:1000 read back {owned.decode().split()}",
        )

        background = ProcessSpec(
            argv=("/bin/sh", "-c", "sleep 3; exit 7"), uid=0, gid=0, cwd="/", env={"PATH": _PATH}, timeout_s=None
        )
        pid = await p.start_process(unit, background)
        seen_running = (await p.process_status(unit, pid)).running
        status = await p.process_status(unit, pid)
        for _ in range(30):
            if not status.running:
                break
            await self.clock.sleep(1.0)
            status = await p.process_status(unit, pid)
        bg_item = _probed(
            seen_running and status.found and not status.running and status.exit_code == 7,
            f"at start running={seen_running}; after exit found={status.found} running={status.running} "
            f"exit_code={status.exit_code} (expected 7)",
        )
        return {"exec_identity": exec_item, "files": files_item, "background": bg_item}

    async def address(self, unit: InstanceHandle) -> Item:
        """Address is obtainable and stable across two reads (lifetime stability is tested by the deployer)."""
        first = await self.platform.internal_address(unit)
        second = await self.platform.internal_address(unit)
        return _probed(bool(first) and first == second, first)

    async def resources(self, unit: InstanceHandle) -> tuple[Item, Item, float | None]:
        """Resource limits (cgroup v2 or v1), requesting cpu=1 memory=512Mi.

        Memory limit must equal the request (OOM behavior matches Compose). CPU limit must be at least the request;
        the actual ratio is recorded, since extra CPU affects only cross-deployment comparability.
        Returns (memory item, CPU item, CPU ratio).
        """
        script = (
            "if [ -f /sys/fs/cgroup/memory.max ]; then cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/cpu.max; "
            "else cat /sys/fs/cgroup/memory/memory.limit_in_bytes /sys/fs/cgroup/cpu/cpu.cfs_quota_us "
            "/sys/fs/cgroup/cpu/cpu.cfs_period_us; fi"
        )
        out = (await self.platform.exec(unit, _sh(script))).stdout.decode().split()
        memory_ok = out[:1] == [str(_MEMORY_BYTES)]
        cpus = _cpu_limit(out[1:3])  # 1 core requested, so cores equal the ratio; unlimited (max / -1) is None
        verdict = "match" if memory_ok else "mismatch"
        memory = _probed(memory_ok, f"requested memory={_MEMORY}; cgroup reads {out[:1]} ({verdict})")
        if cpus is None:
            cpu = _probed(True, f"requested cpu=1; cgroup reads {out[1:3]}, CPU unlimited")
        else:
            cpu = _probed(cpus >= 1.0, f"requested cpu=1; cgroup reads {out[1:3]}, i.e. {cpus:g} cores ({cpus:g}x)")
        return memory, cpu, cpus

    async def external_none(self, unit: InstanceHandle) -> Item:
        """Under `none`, TCP to a public IP fails (5 s timeout). Implicit egress (DNS etc.) is declared separately.

        Only `nc -z` results count: a missing tool or rejected usage is a test error (raised), not "unreachable".
        `nc -z` reports "cannot connect" with exit code 1, so first confirm that path on a closed loopback port.
        """
        script = (
            "command -v nc >/dev/null && command -v timeout >/dev/null || { echo NOTOOL; exit 0; }; "
            'timeout 5 nc -z 127.0.0.1 1 2>/dev/null; echo "self=$?"; '
            f'timeout 5 nc -z {_PUBLIC_IP} 443 2>/dev/null; echo "public=$?"'
        )
        out = (await self.platform.exec(unit, _sh(script, timeout_s=_EXEC_TIMEOUT_S))).stdout.decode().split()
        if out[:1] == ["NOTOOL"] or not (len(out) == 2 and out[0] == "self=1"):
            raise FlotillaError(
                f"cannot test egress none: image under test needs working nc -z and timeout (got {out})",
                stage="run",
                category=ErrorCategory.INVALID,
                retryable=False,
            )
        reachable = out[1] == "public=0"
        return _probed(
            not reachable,
            f"TCP {_PUBLIC_IP}:443 under none policy is {'reachable' if reachable else 'unreachable'} ({out[1]})",
        )

    async def inbound(self, declared: Item) -> Item:
        # An outsider's egress policy blocks peers too: that cannot prove ingress isolation.
        return declared.model_copy(update={"source": "declared"})

    async def storage(self) -> dict[str, Item]:
        """Anchor creates sibling dirs and a marker; an instance mounts one read-only; the anchor deletes them."""
        base = f"launches/{self.anchor.launch_id}/{self.run_id}"
        await self.anchor.mkdir(f"{base}/visible/inner", stage="prepare")
        await self.anchor.mkdir(f"{base}/sibling", stage="prepare")
        try:
            vol = SharedVolume(key=f"{base}/visible", mount_path="/probe-ro", read_only=True)
            unit = await self._create(self._spec("storage", (vol,)))
            # Isolation: the mount shows only the subdirectory's content (`inner`) and mounts that subdirectory. In
            # mountinfo it appears as the mount root (field 4, bind / K8s subPath) or source (2nd field after "-",
            # e.g. NFS server:/export/<subpath>); one must end with the subpath, not the shared root (_mounted_subdir).
            script = (
                "ls -A /probe-ro; touch /probe-ro/w 2>/dev/null && echo WROTE; "
                'awk \'$5 == "/probe-ro" { for (i = 7; $i != "-"; i++); print "ROOT=" $(i + 2) "|" $4 }\' '
                "/proc/self/mountinfo"
            )
            out = (await self.platform.exec(unit, _sh(script))).stdout.decode().split()
            await self._delete(unit.iid)
        finally:
            await self.anchor.remove(base, stage="run")
        remaining = await self.anchor.listdir(f"launches/{self.anchor.launch_id}", stage="run")
        root = next((w.removeprefix("ROOT=") for w in out if w.startswith("ROOT=")), "")
        listing = [w for w in out if not w.startswith("ROOT=") and w != "WROTE"]
        read_only = "inner" in listing and "WROTE" not in out
        isolated = listing == ["inner"] and _mounted_subdir(root, f"{self.run_id}/visible")
        removed = self.run_id not in remaining
        return {
            "read_only": _probed(
                read_only,
                f"mount readable (marker dir {'seen' if 'inner' in out else 'not seen'}), root write "
                + ("failed" if "WROTE" not in out else "succeeded"),
            ),
            "subdir_isolation": _probed(
                isolated,
                f"mount contents {listing}, source {root or 'unknown'}"
                + ("" if isolated else " (not the mounted subdirectory)"),
            ),
            "dir_management": _probed(
                removed,
                "anchor create and delete of a shared-root subdirectory "
                + ("succeeded" if removed else "left it in place"),
            ),
        }


__all__ = ["PROBE_LABEL", "Declared", "ProbeSettings", "probe"]
