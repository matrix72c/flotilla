"""What a deployment's capabilities must honor for a trial plan to open.

One list, so the manager's admission at `open` and `flotilla inspect` report the same verdicts:
the messages are what `open` raises and inspect prints. `disable_internet` is an open-time
request, not a property of the plan, so it is passed in by the caller that knows it.
"""

from __future__ import annotations

from collections.abc import Iterator

from flotilla.core import network
from flotilla.models.plan import TrialPlan
from flotilla.platform.base import Capabilities


def admission_errors(plan: TrialPlan, caps: Capabilities, *, disable_internet: bool = False) -> Iterator[str]:
    """Every way this deployment's capabilities fall short of the plan, per unit first, then the plan as a whole."""
    members = network.link_members(plan)
    for name, unit in sorted(plan.units.items()):
        try:
            caps.require_filesystem(
                unit.readonly_rootfs,
                unit.tmpfs_mounts,
                privileged=unit.privileged_runtime,
                memory=unit.resources.memory,
                volume_paths=tuple(m.target for m in unit.mounts)
                + tuple(m.mount_path for m in plan.external_mounts.get(name, ()))
                + tuple(m.mount_path for m in plan.workspace_mounts.get(name, ())),
                shm_size_bytes=unit.shm_size_bytes,
            )
            if unit.readonly_rootfs and not caps.shared_volume:
                raise ValueError("readonly_rootfs requires shared storage for framework scratch space")
        except ValueError as exc:
            yield f"{name}: {exc}"
        if unit.mounts and not caps.shared_volume:
            yield f"{name}: file-backed mounts require attempt-owned storage"
        if unit.udp_ports and not caps.link_udp:
            yield f"{name}: UDP ports declared, but this deployment lacks link_udp"
        if missing := sorted(unit.devices - caps.devices):
            yield f"{name}: deployment does not support devices {missing}"
        if unit.privileged and not caps.privileged_runtime:
            yield f"{name}: deployment has no privileged-runtime isolation evidence"
    if network.topology(plan) is not None and (not caps.link or len(members) > caps.link_max_members):
        yield "deployment cannot connect this task's services"
    if (any(plan.external_mounts.values()) or any(plan.workspace_mounts.values())) and not caps.shared_volume:
        yield "deployment does not support external shared volumes"
    if plan.volumes and not caps.shared_volume:
        yield "deployment does not support shared trial volumes"
    if (disable_internet or set(plan.units) - plan.external_units) and not caps.supports_no_network:
        yield "no-network requires deny-all egress with only declared cluster DNS exceptions"
    if set(plan.units) - members and not caps.supports_network_none:
        yield "network_mode none requires inbound isolation or sandbox ingress enforcement"
