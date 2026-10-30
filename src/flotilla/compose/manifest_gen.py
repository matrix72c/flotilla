"""Translate validated Compose projects and image metadata into IPC manifests."""

from __future__ import annotations

from collections.abc import Mapping

from flotilla.compose.interpolate import interpolate
from flotilla.compose.layout import Layout, classify
from flotilla.compose.model import MAIN, ImageMeta, Project, Service
from flotilla.manifest import (
    Context,
    Dependency,
    Finding,
    Manifest,
    Mount,
    Network,
    Resources,
    RuntimeParam,
    TrialVolume,
    Unit,
)
from flotilla.manifest import Healthcheck as ManifestHealthcheck


def build_manifest(
    *,
    task: str,
    build_key: str,
    project: Project,
    images: Mapping[str, str],
    meta: Mapping[str, ImageMeta],
    resources: Mapping[str, Resources],
    users: Mapping[str, tuple[int, int]] | None = None,
    seeds: Mapping[str, str] | None = None,
) -> Manifest:
    """Write the task manifest. `users`: numeric identities from Compose `user` (service -> (uid, gid)), per image.

    `seeds`: volumes whose initial content was actually exported (name -> tar relative to task files), passed by
    the builder after exporting task files; omitted means no volume has initial content.

    Must not be called when the project has rejections (scan rejects first).
    """
    if project.rejected:
        raise ValueError("project has rejected findings; cannot write a manifest")
    layout = classify(project.services)
    units = {
        name: _unit(svc, images[name], meta[name], resources[name], (users or {}).get(name), layout)
        for name, svc in project.services.items()
    }
    return Manifest(
        task=task,
        build_key=build_key,
        agent_service=MAIN,
        units=units,
        networks=_networks(project),
        depends_on={
            n: {d: Dependency(condition=dep.condition, required=dep.required) for d, dep in s.depends_on.items()}
            for n, s in project.services.items()
            if s.depends_on
        },
        trial_volumes=_trial_volumes(layout, seeds or {}),
        task_files=None,  # written by publish
        runtime_params=[
            RuntimeParam(service=svc, var=var, expr=expr, param=param, default=default)
            for svc, params in sorted(project.runtime_params.items())
            for var, (expr, _) in sorted(params.items())
            for param, default in interpolate(expr, {}).unresolved.items()
        ],
        external_units=sorted(
            n for n, s in project.services.items() if any(not project.networks[net].internal for net in s.networks)
        ),
        findings=[Finding(path=f.path, kind=f.kind, reason=f.reason) for f in project.findings],
    )


def synthesize_command(svc: Service, meta: ImageMeta) -> tuple[str, ...] | None:
    """Docker rules: Compose `entrypoint` overrides the image ENTRYPOINT and also drops the image CMD;
    `command` overrides CMD. When both are empty there is no main process (returns None)."""
    if svc.entrypoint is not None:
        entrypoint = svc.entrypoint
        cmd = svc.command or ()
    else:
        entrypoint = meta.entrypoint or ()
        cmd = svc.command if svc.command is not None else (meta.cmd or ())
    argv = (*entrypoint, *cmd)
    return argv or None


def _unit(
    svc: Service,
    image: str,
    meta: ImageMeta,
    resources: Resources,
    user: tuple[int, int] | None,
    layout: Layout,
) -> Unit:
    uid, gid = user if user is not None else (meta.uid, meta.gid)
    env = {**meta.env, **svc.environment}  # svc.environment includes env_file; it overrides image ENV.
    hc = None if svc.healthcheck_disabled else (svc.healthcheck or meta.healthcheck)
    return Unit(
        image=image,
        resources=resources,
        context=Context(env=env, uid=uid, gid=gid, cwd=svc.working_dir or meta.workdir or "/"),
        command=list(c) if (c := synthesize_command(svc, meta)) is not None else None,
        healthcheck=None
        if hc is None
        else ManifestHealthcheck(
            test=list(hc.test),
            interval_s=hc.interval_s,
            timeout_s=hc.timeout_s,
            retries=hc.retries,
            start_period_s=hc.start_period_s,
            start_interval_s=hc.start_interval_s,
        ),
        restart=svc.restart,
        mounts=[
            Mount(scope=m.scope, key=m.key, target=m.target, read_only=m.read_only)
            for m in layout.mounts.get(svc.name, ())
        ],
        privileged=svc.privileged,
        readonly_rootfs=svc.readonly_rootfs,
        tmpfs_mounts=list(svc.tmpfs),
        shm_size_bytes=svc.shm_size_bytes,
    )


def _networks(project: Project) -> dict[str, Network]:
    """Members and name table per network: service names, aliases, and `hostname` are all names."""
    out: dict[str, Network] = {}
    for net, definition in sorted(project.networks.items()):
        members = sorted(n for n, s in project.services.items() if net in s.networks)
        names: dict[str, set[str]] = {}
        for n in members:
            svc = project.services[n]
            for name in (n, *svc.networks[net].aliases, *filter(None, (svc.hostname, svc.container_name))):
                names.setdefault(name, set()).add(n)
        out[net] = Network(
            internal=definition.internal,
            members=members,
            names={k: sorted(v) for k, v in sorted(names.items())},
        )
    return out


def _trial_volumes(layout: Layout, seeds: Mapping[str, str]) -> list[TrialVolume]:
    """Directories this trial must prepare. `seeds` holds volumes that **actually exported** a seed (name -> tar).

    No seed is produced when the image has nothing under the mount point (matching Docker's empty volume), and the
    manifest must not declare one, or prepare would unpack a nonexistent tar.
    """
    out: list[TrialVolume] = []
    for v in layout.volumes:
        if v.mode == "trial":
            out.append(TrialVolume(key=v.name, seed=None if v.nocopy else seeds.get(v.name)))
    for g in layout.binds:
        if g.mode == "trial":
            out.append(TrialVolume(key=f"binds/{g.index}", copy_from=f"binds/{g.index}"))
    return out
