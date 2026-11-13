"""Probe phases and common storage/runtime checks. Each failure retains its own outcome."""

from __future__ import annotations

import re
import shlex
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from flotilla.capabilities import Exec, Item, Lifecycle, Network, Runtime, Storage
from flotilla.platform.base import (
    ErrorCategory,
    FlotillaError,
    InstanceHandle,
    ProcessSpec,
    SharedVolume,
    TaskDevice,
)
from flotilla.platform.opensandbox.platform import OpenSandboxPlatform
from flotilla.probe import _probed, _Run, _sh
from flotilla.probe_opensandbox import authentication


async def attempt[T](operation: Callable[[], Awaitable[T]], *, scope: str = "ordinary") -> T | Item:
    try:
        result = await operation()
        return result.model_copy(update={"scope": scope}) if isinstance(result, Item) else result
    except NotImplementedError:
        return Item(status="skipped", evidence="configured backend cannot express this check", scope=scope)
    except Exception as exc:
        # Keep structured diagnostics, never remote messages that may contain credentials/output.
        measurements: dict[str, Any] = {
            "frames": [
                {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                for frame in traceback.extract_tb(exc.__traceback__)[-8:]
            ]
        }
        if isinstance(exc, FlotillaError):
            measurements.update(stage=exc.stage, category=exc.category.value, retryable=exc.retryable)
            if match := re.search(r"\bHTTP ([1-5][0-9]{2})\b", str(exc)):
                measurements["http_status"] = int(match.group(1))
        return Item(
            status="error",
            evidence=f"check incomplete ({type(exc).__name__})",
            measurements=measurements,
            scope=scope,
        )


async def prerequisites(run: _Run, unit: InstanceHandle) -> Item:
    result = await run.platform.exec(
        unit,
        _sh(
            "for tool in sh id pwd stat cat ls touch awk timeout grep dd python3; do "
            "command -v $tool >/dev/null || exit 42; done; python3 -I -c 'import socket,threading,struct'"
        ),
    )
    tools_ok = result.exit_code == 0 and not result.timed_out
    if tools_ok and isinstance(run.platform, OpenSandboxPlatform):
        wrapper = run.platform._execd.wrapper
        encoded = await run.platform.exec(
            unit,
            ProcessSpec(
                argv=(wrapper, "sh", "-c", f"printf probe | {wrapper} base64"),
                uid=0,
                gid=0,
                cwd="/",
                env={},
                timeout_s=15,
            ),
        )
        if encoded.exit_code != 0 or encoded.timed_out or encoded.stdout.strip() != b"cHJvYmU=":
            return Item(status="error", evidence="configured execd wrapper base64 applet positive control failed")
    return Item(
        status="passed" if tools_ok else "error",
        evidence="configured execd wrapper and required shell/Python socket tools (no installation/fallback)",
    )


async def storage_extra(run: _Run, *, privileged: bool = False) -> dict[str, Item]:
    p = run.platform
    scope = "privileged" if privileged else "ordinary"
    base = f"launches/{run.anchor.launch_id}/{run.run_id}/shared-{scope}"
    await run.anchor.mkdir(base + "/visible", stage="prepare")
    if privileged:  # the runtime path does the same for writable mounts of privileged-runtime units
        await run.anchor.open_permissions(base + "/visible", stage="prepare")
    spec = replace(
        run._spec("shared-" + scope, (SharedVolume(base + "/visible", "/probe", False),)),
        privileged=privileged,
        image=run.settings.privileged_image if privileged else run.settings.image,
    )
    a, b = await run._create(spec), await run._create(spec)
    token = run.run_id.encode()
    # Establish fixtures first; failures of individual checks below must not erase other evidence.
    await p.write_file(a, "/probe/from-a", token, mode=0o644, uid=0, gid=0)
    await p.write_file(b, "/probe/from-b", token + b"b", mode=0o644, uid=0, gid=0)

    async def shared_contents() -> Item:
        first = await p.read_file(b, "/probe/from-a") == token
        second = await p.read_file(a, "/probe/from-b") == token + b"b"
        return _probed(first and second, "two-instance bidirectional shared contents; no all-node claim")

    async def file_mount() -> Item:
        file_spec = replace(
            spec, volumes=(*spec.volumes[:1], SharedVolume(base + "/visible/from-a", "/probe-file", True))
        )
        file_unit = await run._create(file_spec)
        result = await p.exec(file_unit, _sh("test -f /probe-file && ! printf bad >> /probe-file"))
        return _probed(
            result.exit_code == 0 and not result.timed_out and await p.read_file(file_unit, "/probe-file") == token,
            "single-file subPath content and root write rejection",
        )

    async def readonly_mount() -> dict[str, Item]:
        ro = await run._create(
            replace(spec, volumes=(*spec.volumes[:1], SharedVolume(base + "/visible", "/probe", True)))
        )
        result = await p.exec(
            ro,
            _sh(
                "test -f /probe/from-a && test -f /probe/from-b && ! touch /probe/forbidden "
                "&& test ! -e /probe/../visible"
            ),
        )
        readonly = _probed(
            result.exit_code == 0 and not result.timed_out,
            "readable subdirectory, root write rejection, sibling path absent",
        )

        async def subdir() -> Item:
            from flotilla.probe import _mounted_subdir

            mountinfo = await _mountinfo(run, ro)
            isolated = False
            for line in mountinfo.splitlines():
                fields = line.split()
                if len(fields) > 6 and fields[4] == "/probe" and "-" in fields:
                    isolated = _mounted_subdir(fields[fields.index("-") + 2] + "|" + fields[3], base + "/visible")
            return _probed(isolated, "mounted source/root identifies exactly the requested subdirectory")

        return {"read_only": readonly, "subdir_isolation": await attempt(subdir, scope=scope)}

    async def missing_subpath() -> Item:
        try:
            absent = await run._create(
                replace(spec, volumes=(*spec.volumes[:1], SharedVolume(base + "/absent", "/missing", True)))
            )
        except FlotillaError as exc:
            if exc.category is ErrorCategory.INVALID:
                return _probed(True, "missing subPath creation explicitly rejected")
            raise
        listing = await p.exec(absent, _sh('test -d /missing && test -z "$(ls -A /missing)"'))
        return _probed(False, "missing subPath was created/accepted; empty=" + str(listing.exit_code == 0))

    results = {
        "shared_volume": await attempt(shared_contents, scope=scope),
        "volume_file": await attempt(file_mount, scope=scope),
        "missing_subpath": await attempt(missing_subpath, scope=scope),
        "no_auto_mount": await attempt(lambda: no_auto_mount(run, a, privileged=privileged), scope=scope),
    }
    readonly = await attempt(readonly_mount, scope=scope)
    results.update({"read_only": readonly, "subdir_isolation": readonly} if isinstance(readonly, Item) else readonly)
    # The run owns all allocations, including partially created checks, and cleans them in its finalizer.
    return results


async def _mountinfo(run: _Run, unit: InstanceHandle) -> str:
    # procfs is generated content, not a downloadable regular file. Inspect through authenticated exec.
    result = await run.platform.exec(unit, _sh("cat /proc/self/mountinfo"))
    if result.exit_code != 0 or result.timed_out:
        raise ValueError("mount table inspection failed")
    return result.stdout.decode()


async def no_auto_mount(run: _Run, mounted: InstanceHandle, *, privileged: bool) -> Item:
    from flotilla.probe_opensandbox import unmounted_mountinfo

    p = run.platform
    before = await _mountinfo(run, mounted)
    backing = set()
    for line in before.splitlines():
        fields = line.split()
        if len(fields) > 6 and fields[4] in ("/.flotilla", "/probe") and "-" in fields:
            backing.add(fields[fields.index("-") + 2])
    if not backing:
        return Item(status="error", evidence="shared mount backing could not be identified")
    spec = replace(
        run._spec("unmounted"),
        volumes=(),
        privileged=privileged,
        image=run.settings.privileged_image if privileged else run.settings.image,
        entrypoint=("/bin/sh", "-c", "sleep 3600"),
    )
    unit = await run._create(spec)
    data = await unmounted_mountinfo(p, unit)
    extras = []
    root_seen = False
    for line in data.splitlines():
        fields = line.split()
        if len(fields) > 6 and "-" in fields:
            source, path = fields[fields.index("-") + 2], fields[4]
            root_seen |= path == "/"
            if source in backing or path in ("/sandbox-data", "/data"):
                extras.append(path)
    await run._delete(unit.iid)
    if not root_seen:
        return Item(status="error", evidence="no-mount inspection returned no valid mount table")
    return Item(
        status="failed" if extras else "passed",
        evidence="no-mount create checked for shared backing and project data paths",
        measurements={"extra_mounts": extras},
        scope="privileged" if privileged else "ordinary",
    )


async def privileged(run: _Run) -> dict[str, Item]:
    if not run.settings.privileged_image:
        return {
            "privileged_runtime": Item(
                status="skipped", evidence="probe privileged_image not configured", scope="privileged"
            )
        }
    p = run.platform
    if not isinstance(p, OpenSandboxPlatform):
        return {"privileged_runtime": Item(status="skipped", evidence="backend has no privileged runtime probe")}
    unit = await run._create(replace(run._spec("privileged"), image=run.settings.privileged_image, privileged=True))
    # Address readiness uses the exact configured protocol and real privileged create path.
    await p.internal_address(unit)
    # Optional Docker measurement (DinD daemon and inner container limits); recorded, never required for admission.
    docker = await attempt(lambda: _docker(run, unit), scope="privileged")
    auth = await attempt(lambda: authentication(p, unit, run_id=run.run_id, scope="privileged"), scope="privileged")
    fuse_item = await attempt(lambda: fuse(run, unit), scope="privileged")
    storage = await attempt(lambda: storage_extra(run, privileged=True), scope="privileged")
    if isinstance(storage, Item):
        storage_item = no_mount = storage
    else:
        storage_item = _probed(
            all(storage[name].ok for name in ("read_only", "shared_volume", "volume_file", "subdir_isolation")),
            "privileged read-only, shared and single-file mount behavior",
        ).model_copy(update={"scope": "privileged"})
        no_mount = storage["no_auto_mount"]
    from flotilla.probe_network import measure_network

    run.privileged = True
    try:
        pre = await attempt(lambda: prerequisites(run, unit), scope="privileged")
        net = await attempt(lambda: measure_network(run, unit), scope="privileged") if pre.ok else pre
    finally:
        run.privileged = False
    if isinstance(net, Item):
        network_item = ingress_item = net
    else:
        network_item = Item(
            status="passed"
            if (net.link.ok and net.link_udp.ok and net.network_acceptance.ok and net.external_allowlist.ok)
            else "error",
            evidence="privileged network controls (ordinary evidence is not reused)",
            scope="privileged",
            measurements=net.model_dump(mode="json"),
        )
        ingress_item = net.inbound_isolation.model_copy(update={"scope": "privileged"})
    gone = await run._delete(unit.iid)
    kvm_item = await attempt(lambda: device(run, "kvm"), scope="privileged")
    tun_item = await attempt(lambda: device(run, "tun"), scope="privileged")
    return {
        "privileged_runtime": Item(
            status="passed" if gone else "failed",
            scope="privileged",
            evidence="configured privileged create and address readiness; outer deletion; Docker in measurements",
            measurements={
                "docker": {"status": docker.status, "evidence": docker.evidence},
                "extensions": sorted(p._privileged_extensions),
                "inner_memory_bytes": 64 * 1024 * 1024,
                "inner_cpu": 0.5,
            },
        ),
        "privileged_storage": storage_item,
        "privileged_no_auto_mount": no_mount,
        "privileged_exec_auth": auth,
        "fuse": fuse_item,
        "kvm": kvm_item,
        "tun": tun_item,
        "privileged_network": network_item,
        "privileged_inbound_isolation": ingress_item,
    }


async def _docker(run: _Run, unit: InstanceHandle) -> Item:
    """Docker daemon positive control plus a digest-pinned inner container's output, exit and resource limits."""
    if not run.settings.inner_image:
        return Item(status="skipped", evidence="probe inner_image not configured", scope="privileged")
    p = run.platform
    tool = await p.exec(unit, _sh("command -v docker >/dev/null"))
    if tool.exit_code != 0:
        return Item(status="error", evidence="privileged image lacks docker", scope="privileged")
    info = await p.exec(unit, _sh("docker info >/dev/null", timeout_s=60))
    if info.exit_code != 0:
        return Item(status="error", evidence="Docker daemon positive control unavailable", scope="privileged")
    image = shlex.quote(run.settings.inner_image)
    token = shlex.quote(run.run_id)
    command = f"docker run --rm --network none --memory 64m --cpus 0.5 {image} /bin/sh -c " + shlex.quote(
        f"printf '%s\\n' {token}; cat /sys/fs/cgroup/memory.max /sys/fs/cgroup/cpu.max"
    )
    result = await p.exec(unit, _sh(command, timeout_s=180))
    fields = result.stdout.decode().split()
    good = (
        result.exit_code == 0
        and not result.timed_out
        and fields[:2] == [run.run_id, str(64 * 1024 * 1024)]
        and len(fields) == 4
        and fields[2].isdigit()
        and fields[3].isdigit()
        and int(fields[2]) / max(int(fields[3]), 1) == 0.5
    )
    return Item(
        status="passed" if good else "failed",
        evidence="docker info; digest inner container output/exit/resource limits",
        scope="privileged",
    )


# Root opens the device node and exercises it: KVM_GET_API_VERSION (must be 12) then KVM_CREATE_VM; TUNSETIFF
# creates a TAP interface (IFF_TAP | IFF_NO_PI), which needs CAP_NET_ADMIN in the sandbox network namespace.
# The interface is looked up by name; sysbox's sysfs does not resolve /sys/class/net/<name> links.
_DEVICE_SCRIPTS: dict[TaskDevice, str] = {
    "kvm": (
        "import fcntl, os\n"
        "fd = os.open('/dev/kvm', os.O_RDWR | os.O_CLOEXEC)\n"
        "assert fcntl.ioctl(fd, 0xAE00) == 12\n"
        "os.close(fcntl.ioctl(fd, 0xAE01, 0))\n"
        "os.close(fd)\n"
    ),
    "tun": (
        "import fcntl, os, socket, struct\n"
        "fd = os.open('/dev/net/tun', os.O_RDWR | os.O_CLOEXEC)\n"
        "fcntl.ioctl(fd, 0x400454CA, struct.pack('16sH22x', b'flotilla-tap0', 0x1002))\n"
        "socket.if_nametoindex('flotilla-tap0')\n"
        "os.close(fd)\n"
    ),
}

_DEVICE_EVIDENCE: dict[TaskDevice, str] = {
    "kvm": "create with only the kvm extension; root opens /dev/kvm, API version 12, creates a VM; outer deletion",
    "tun": "create with only the tun extension; root creates a TAP interface; outer deletion",
}


async def device(run: _Run, name: TaskDevice) -> Item:
    """A privileged instance requesting exactly one device; the device must be usable by root, then deleted."""
    spec = replace(
        run._spec(f"device-{name}"), image=run.settings.privileged_image, privileged=True, devices=frozenset({name})
    )
    unit = await run._create(spec)
    result = await run.platform.exec(unit, _sh("python3 -I -c " + shlex.quote(_DEVICE_SCRIPTS[name]), timeout_s=60))
    gone = await run._delete(unit.iid)
    return Item(
        status="passed" if result.exit_code == 0 and not result.timed_out and gone else "failed",
        evidence=_DEVICE_EVIDENCE[name],
        scope="privileged",
    )


async def fuse(run: _Run, unit: InstanceHandle) -> Item:
    p = run.platform
    if not isinstance(p, OpenSandboxPlatform):
        raise NotImplementedError("backend has no FUSE runtime probe")
    extensions = {**p._extensions, **p._privileged_extensions}
    if str(extensions.get("fuse", "")).lower() != "true":
        return Item(status="skipped", evidence="actual runtime extensions do not enable FUSE", scope="privileged")
    tools = await p.exec(unit, _sh("command -v fuse-overlayfs >/dev/null && command -v fusermount3 >/dev/null"))
    if tools.exit_code != 0:
        return Item(
            status="error", evidence="local FUSE probe needs fuse-overlayfs and fusermount3", scope="privileged"
        )
    base = "/tmp/flotilla-fuse-" + run.run_id
    script = (
        f"set -e; test -r /dev/fuse; test -w /dev/fuse; mkdir -p {base}/lower {base}/upper {base}/work {base}/merged; "
        f"printf probe > {base}/lower/marker; "
        f"trap 'fusermount3 -u {base}/merged 2>/dev/null || true' EXIT; "
        f"fuse-overlayfs -o lowerdir={base}/lower,upperdir={base}/upper,workdir={base}/work {base}/merged; "
        f'test "$(cat {base}/merged/marker)" = probe; printf write > {base}/merged/write; '
        f'test "$(cat {base}/merged/write)" = write; fusermount3 -u {base}/merged; '
        f'test "$(cat {base}/upper/write)" = write; trap - EXIT'
    )
    result = await p.exec(unit, _sh(script))
    return Item(
        status="passed" if result.exit_code == 0 and not result.timed_out else "failed",
        evidence="local FUSE mount, read, write and unmount",
        scope="privileged",
    )


async def measure(run: _Run, results: dict[str, Any]) -> None:
    from flotilla.probe_network import measure_network

    lc = await attempt(lambda: run.lifecycle(run.settings.list_visibility_s, run.settings.ttl_s))
    if isinstance(lc, Item):
        results["lifecycle"] = Lifecycle(create_get_delete=lc, label_list=lc, renew=lc)
    else:
        ttl = lc.pop("ttl")
        anchor = await attempt(lambda: run.lifecycle(run.settings.list_visibility_s, run.settings.anchor_ttl_s))
        results["lifecycle"] = Lifecycle(
            create_get_delete=lc["create_get_delete"],
            label_list=lc["label_list"],
            renew=lc["renew"],
            trial_ttl=ttl,
            anchor_ttl=anchor if isinstance(anchor, Item) else anchor["ttl"],
            list_visibility_s=run.settings.list_visibility_s,
        )
    unit_result = await attempt(run.create_unit)
    if isinstance(unit_result, Item):
        results["exec"] = Exec(prerequisites=unit_result, placeholder_entry=unit_result)
        return  # do not create the same resource group repeatedly after a prerequisite failure
    unit, placeholder = unit_result
    pre = await attempt(lambda: prerequisites(run, unit))
    results["exec"] = Exec(prerequisites=pre, placeholder_entry=placeholder)
    if not pre.ok:
        return
    execution = await attempt(lambda: run.exec_items(unit))
    if isinstance(execution, Item):
        execution = {key: execution for key in ("exec_identity", "files", "background")}
    results["exec"] = Exec(prerequisites=pre, placeholder_entry=placeholder, **execution)
    resource = await attempt(lambda: run.resources(unit))
    mem, cpu, ratio = (resource, resource, None) if isinstance(resource, Item) else resource
    runtime: dict[str, Any] = {
        "memory_limit": mem,
        "cpu_limit": cpu,
        "cpu_limit_ratio": ratio,
        "image_digest": _probed(
            placeholder.ok and "@sha256:" in run.settings.image, "create with configured digest image"
        ),
        "exec_auth": await attempt(lambda: authentication(run.platform, unit, run_id=run.run_id)),
    }
    results["runtime"] = Runtime(**runtime)
    address = await attempt(lambda: run.address(unit))
    if address.ok:
        network = await attempt(lambda: measure_network(run, unit))
        results["network"] = (
            Network(link=network, link_udp=network, inbound_isolation=network, network_acceptance=network)
            if isinstance(network, Item)
            else network
        )
    await run._delete(unit.iid)
    storage = await attempt(run.storage)
    if isinstance(storage, Item):
        storage = {key: storage for key in ("read_only", "subdir_isolation", "dir_management")}
    more = await attempt(lambda: storage_extra(run))
    if isinstance(more, Item):
        storage.update({key: more for key in ("shared_volume", "volume_file", "missing_subpath", "no_auto_mount")})
    else:
        storage.update(more)
    fs = await attempt(lambda: run.filesystem(run.settings.filesystem_supported, run.settings.filesystem_supported))
    readonly, tmpfs = (fs, fs) if isinstance(fs, Item) else fs
    runtime["readonly_rootfs"] = readonly
    storage["native_tmpfs"] = tmpfs
    storage["shm_size"] = await attempt(lambda: run.shm_size(run.settings.filesystem_supported))
    results["storage"] = Storage(**storage)
    # Finish ordinary phase before creating privileged instances.
    for iid in list(run._created):
        await run._delete(iid)
    optional = await attempt(lambda: privileged(run), scope="privileged")
    runtime.update({"privileged_runtime": optional} if isinstance(optional, Item) else optional)
    results["runtime"] = Runtime(**runtime)
