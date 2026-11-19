"""The isolation checks and the order they run in. Each failure keeps its own outcome.

A restriction holding for an ordinary sandbox says nothing about one on the privileged runtime, so
the storage, egress, inbound and exec-channel checks run twice: once ordinary, once privileged.
"""

from __future__ import annotations

import re
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from flotilla.capabilities import PRIVILEGED_PROPERTIES, Evidence, IsolationEvidence
from flotilla.platform.base import FlotillaError, InstanceHandle, SharedVolume
from flotilla.platform.opensandbox.platform import OpenSandboxPlatform
from flotilla.probe import _mounted_subdir, _Run, _sh
from flotilla.probe_opensandbox import authentication


async def attempt[T](operation: Callable[[], Awaitable[T]]) -> T | Evidence:
    """Run one check. An exception means the restriction was not demonstrated, not that it is absent."""
    try:
        return await operation()
    except NotImplementedError:
        return Evidence(False, "configured backend cannot express this check")
    except Exception as exc:
        # Keep structured diagnostics, never remote messages that may contain credentials/output.
        detail: dict[str, Any] = {
            "frames": [
                f"{Path(frame.filename).name}:{frame.lineno}" for frame in traceback.extract_tb(exc.__traceback__)[-4:]
            ]
        }
        if isinstance(exc, FlotillaError):
            detail.update(stage=exc.stage, category=exc.category.value)
            if match := re.search(r"\bHTTP ([1-5][0-9]{2})\b", str(exc)):
                detail["http_status"] = int(match.group(1))
        return Evidence(False, f"check incomplete ({type(exc).__name__}): {detail}")


def _held(ok: bool, observed: str) -> Evidence:
    return Evidence(ok, observed)


async def storage(run: _Run, *, privileged: bool = False) -> dict[str, Evidence]:
    """Read-only means read-only, one subdirectory means one subdirectory, and nothing else is mounted."""
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
    writable = await run._create(spec)
    token = run.run_id.encode()
    await p.write_file(writable, "/probe/marker", token, mode=0o644, uid=0, gid=0)

    async def read_only_mount() -> dict[str, Evidence]:
        unit = await run._create(
            replace(spec, volumes=(*spec.volumes[:1], SharedVolume(base + "/visible", "/probe", True)))
        )
        result = await p.exec(
            unit,
            _sh("test -f /probe/marker && ! touch /probe/forbidden && test ! -e /probe/../visible"),
        )
        read_only = _held(
            result.exit_code == 0 and not result.timed_out,
            "read-only mount: readable, root write rejected, sibling path absent"
            if result.exit_code == 0 and not result.timed_out
            else f"read-only mount accepted a write or exposed a sibling (exit {result.exit_code})",
        )

        async def subdir() -> Evidence:
            mountinfo = await _mountinfo(run, unit)
            isolated = False
            source = ""
            for line in mountinfo.splitlines():
                fields = line.split()
                if len(fields) > 6 and fields[4] == "/probe" and "-" in fields:
                    source = fields[fields.index("-") + 2] + "|" + fields[3]
                    isolated = _mounted_subdir(source, base + "/visible")
            return _held(
                isolated,
                f"mounted source {source or 'unknown'} "
                + ("is exactly the requested subdirectory" if isolated else "is not the requested subdirectory"),
            )

        return {"read_only": read_only, "subdir_isolation": await attempt(subdir)}

    results: dict[str, Evidence] = {"no_auto_mount": await attempt(lambda: no_auto_mount(run, writable, privileged))}
    outcome = await attempt(read_only_mount)
    results.update({"read_only": outcome, "subdir_isolation": outcome} if isinstance(outcome, Evidence) else outcome)
    # The run owns every allocation, including partially created checks, and cleans them in its finalizer.
    return results


async def _mountinfo(run: _Run, unit: InstanceHandle) -> str:
    # procfs is generated content, not a downloadable regular file. Inspect through authenticated exec.
    result = await run.platform.exec(unit, _sh("cat /proc/self/mountinfo"))
    if result.exit_code != 0 or result.timed_out:
        raise ValueError("mount table inspection failed")
    return result.stdout.decode()


async def no_auto_mount(run: _Run, mounted: InstanceHandle, privileged: bool) -> Evidence:
    """A sandbox that asked for no storage must not find the shared root mounted anyway."""
    from flotilla.probe_opensandbox import unmounted_mountinfo

    p = run.platform
    before = await _mountinfo(run, mounted)
    backing = set()
    for line in before.splitlines():
        fields = line.split()
        if len(fields) > 6 and fields[4] in ("/.flotilla", "/probe") and "-" in fields:
            backing.add(fields[fields.index("-") + 2])
    if not backing:
        return Evidence(False, "shared mount backing could not be identified")
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
        return Evidence(False, "no-mount inspection returned no valid mount table")
    return _held(
        not extras,
        "a sandbox requesting no storage had none mounted"
        if not extras
        else f"shared backing or project data mounted anyway at {extras}",
    )


async def privileged_isolation(run: _Run) -> dict[str, Evidence]:
    """The same restrictions, measured again inside the privileged runtime."""
    from flotilla.probe_network import measure_isolation

    if not run.settings.privileged_image:
        return {}
    p = run.platform
    if not isinstance(p, OpenSandboxPlatform):
        return {}
    unit = await run._create(replace(run._spec("privileged"), image=run.settings.privileged_image, privileged=True))
    # Address readiness uses the exact configured protocol and real privileged create path.
    await p.internal_address(unit)
    auth = await attempt(lambda: authentication(p, unit, run_id=run.run_id))
    store = await attempt(lambda: storage(run, privileged=True))
    if isinstance(store, Evidence):
        storage_item = no_mount = store
    else:
        storage_item = _held(
            store["read_only"].held and store["subdir_isolation"].held,
            "privileged read-only and subdirectory isolation: "
            f"{store['read_only'].observed}; {store['subdir_isolation'].observed}",
        )
        no_mount = store["no_auto_mount"]
    run.privileged = True
    try:
        net = await attempt(lambda: measure_isolation(run, unit))
    finally:
        run.privileged = False
    if isinstance(net, Evidence):
        network_item = ingress_item = net
    else:
        network_item = _held(
            net.external_none.held and net.network_acceptance.held,
            f"privileged egress controls (ordinary evidence is not reused): {net.external_none.observed}",
        )
        ingress_item = net.inbound_isolation
    await run._delete(unit.iid)
    return {
        "privileged_storage": storage_item,
        "privileged_no_auto_mount": no_mount,
        "privileged_exec_auth": auth,
        "privileged_network": network_item,
        "privileged_inbound_isolation": ingress_item,
    }


async def measure(run: _Run) -> IsolationEvidence:
    """Run every isolation check, ordinary phase first, and collect what held."""
    from flotilla.probe_network import measure_isolation

    unit = await run._create(run._spec("isolation"))
    evidence = IsolationEvidence(exec_auth=await attempt(lambda: authentication(run.platform, unit, run_id=run.run_id)))

    network = await attempt(lambda: measure_isolation(run, unit))
    if isinstance(network, Evidence):
        evidence = replace(evidence, external_none=network, network_acceptance=network, inbound_isolation=network)
    else:
        evidence = replace(
            evidence,
            external_none=network.external_none,
            network_acceptance=network.network_acceptance,
            inbound_isolation=network.inbound_isolation,
            external_forms=network.external_forms,
            implicit_egress=network.implicit_egress,
            cluster_dns=network.cluster_dns,
        )
    await run._delete(unit.iid)

    store = await attempt(lambda: storage(run))
    if isinstance(store, Evidence):
        store = {"read_only": store, "subdir_isolation": store, "no_auto_mount": store}
    evidence = replace(
        evidence,
        read_only=store["read_only"],
        subdir_isolation=store["subdir_isolation"],
        no_auto_mount=store["no_auto_mount"],
    )

    # Finish the ordinary phase before creating privileged instances.
    for iid in list(run._created):
        await run._delete(iid)
    optional = await attempt(lambda: privileged_isolation(run))
    if isinstance(optional, Evidence):
        optional = dict.fromkeys(PRIVILEGED_PROPERTIES, optional)
    if not optional:  # no privileged image configured, or a backend that cannot express the checks
        return evidence
    return replace(
        evidence,
        privileged_storage=optional["privileged_storage"],
        privileged_no_auto_mount=optional["privileged_no_auto_mount"],
        privileged_exec_auth=optional["privileged_exec_auth"],
        privileged_network=optional["privileged_network"],
        privileged_inbound_isolation=optional["privileged_inbound_isolation"],
    )


__all__ = ["attempt", "measure"]
