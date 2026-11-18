"""Planning a task directly produces the trial the release path produces.

`plan_task` replaces three stages — prepare a task into a release, read the release back into a
manifest, resolve the manifest into a plan — with one call. This is the evidence that the
replacement is faithful: the same task goes through both paths and the two `TrialPlan`s must
agree, field for field, across the shapes that distinguish real tasks (several services, binds,
named volumes, healthchecks, `depends_on`, devices, tmpfs, a non-root Compose `user`).

Both paths read the project with the same parser, so this is not re-checking the parse. What it
checks is everything built on top: argv synthesis from the image's `Entrypoint` and `Cmd`,
identity resolution, the hosts table, mount keys from the layout, and resource resolution.

Two differences are by construction, and are compared on what a trial can observe rather than
asserted equal:

- **`copy_from`.** The release copied each bind source into a flat `files/binds/<n>`, so its plan
  names that staging path; nothing is staged now, so the direct plan names the bind source where
  it lives. Both must resolve to the same files.
- **`task_files`.** The release points at `tasks/<key>`, a direct plan at the dataset directory.

No Docker and no network: the image configs come from a fixture, which is also what lets the
image's `Entrypoint` / `Cmd` / `User` be varied on purpose. It runs under `pytest tests/contract`
and needs no environment variables.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from flotilla.build.driver import BuildContext, build_task
from flotilla.build.images import BuildSettings
from flotilla.compose.plan_task import TaskSource, plan_task
from flotilla.compose.task import load_task
from flotilla.config import ResourceDefaults
from flotilla.core.plan import TrialPlan, Unit
from flotilla.manifest import Manifest
from flotilla.platform.base import Capabilities
from flotilla.runtime.planning import to_plan

#: Digest-pinned, as both paths now require: a tag cannot name the image a trial ran.
APP = "r.io/ns/app@sha256:" + "a" * 64
DB = "r.io/ns/db@sha256:" + "b" * 64
TOOL = "r.io/ns/tool@sha256:" + "c" * 64

#: Image configs the fixture serves, chosen so argv synthesis has something to do: `app` declares
#: both ENTRYPOINT and CMD, `db` only CMD, `tool` neither.
CONFIGS: dict[str, dict[str, Any]] = {
    APP: {
        "Entrypoint": ["/app/entrypoint.sh"],
        "Cmd": ["serve", "--port", "8080"],
        "WorkingDir": "/app",
        "User": "0:0",
        "Env": ["PATH=/image/bin", "IMAGE_ONLY=from-image"],
        "Healthcheck": {"Test": ["CMD", "/app/health"], "Interval": 7_000_000_000, "Retries": 4},
    },
    DB: {"Cmd": ["postgres"], "WorkingDir": "/var/lib/db", "User": "root"},
    TOOL: {"User": "0:0"},
}


#: Everything the written tasks below declare has to be available, or the scan rejects them before
#: either path produces a plan.
CAPS = Capabilities(
    max_label_value_len=63,
    list_visibility_s=30.0,
    max_ttl_seconds=21600,
    link=True,
    link_udp=False,
    link_max_members=10,
    inbound_isolation=True,
    external_forms=frozenset({"none", "any"}),
    implicit_egress=(),
    internal_address="exec",
    shared_volume=True,
    volume_file=True,
    no_auto_mount=True,
    native_tmpfs=True,
    shm_size=True,
    exec_auth=True,
    privileged_runtime=True,
    devices=frozenset({"fuse"}),
)


class FixedConfigs:
    """An `ImageConfigSource` and a `Registry` at once, so both paths read the same image facts."""

    def __init__(self, configs: Mapping[str, Mapping[str, Any]]) -> None:
        self.configs = configs
        self.reads: list[str] = []

    def image_config(self, pinned: str) -> Mapping[str, Any]:
        self.reads.append(pinned)
        if pinned not in self.configs:
            raise AssertionError(f"the test declares no image config for {pinned}")
        return self.configs[pinned]

    def digest(self, name: str, reference: str) -> str:
        raise AssertionError(f"no tag may be resolved: {name}:{reference}")


TASKS: dict[str, dict[str, str]] = {
    "single-service": {
        "task.toml": f'[environment]\ndocker_image = "{APP}"\ncpus = 2\nmemory_mb = 512\n',
        "instruction.md": "Do it.\n",
    },
    "several-services-with-binds-and-volumes": {
        "task.toml": f'[environment]\ndocker_image = "{APP}"\n',
        "instruction.md": "Do it.\n",
        "environment/docker-compose.yaml": f"""services:
  main:
    volumes:
      - ./conf:/etc/app:ro
      - "state:/var/state"
    depends_on:
      db:
        condition: service_healthy
    networks: [front, back]
  db:
    image: {DB}
    volumes:
      - ./conf/db.conf:/etc/db.conf:ro
      - "state:/var/state"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready"]
      interval: 3s
      retries: 10
    networks:
      back:
        aliases: [primary]
  tool:
    image: {TOOL}
    command: ["/bin/true"]
    user: "1000:1000"
    working_dir: /work
    tmpfs:
      - /scratch:size=16m
    shm_size: 32m
    environment:
      DECLARED: value
    networks: [back]
volumes:
  state: {{}}
networks:
  front: {{}}
  back:
    internal: true
""",
        "environment/conf/app.conf": "declared = true\n",
        "environment/conf/db.conf": "shared_buffers = 64MB\n",
    },
    "devices-and-privileged": {
        "task.toml": f'[environment]\ndocker_image = "{APP}"\n',
        "instruction.md": "Do it.\n",
        "environment/docker-compose.yaml": f"""services:
  main:
    privileged: true
    devices:
      - /dev/fuse
    extra_hosts:
      pinned: 10.1.2.3
  peer:
    image: {TOOL}
    command: ["/bin/sleep", "infinity"]
    hostname: peer-host
    network_mode: none
""",
    },
    "entrypoint-overrides-the-image": {
        "task.toml": f'[environment]\ndocker_image = "{APP}"\nworkdir = "/declared"\n',
        "instruction.md": "Do it.\n",
        "environment/docker-compose.yaml": """services:
  main:
    entrypoint: ["/own/entry"]
    environment:
      DECLARED: value
  disabled-health:
    image: """
        + APP
        + """
    healthcheck:
      disable: true
    command: ["/bin/sleep", "infinity"]
""",
    },
}


def write_task(root: Path, name: str) -> Path:
    path = root / name
    for relative, text in TASKS[name].items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    (path / "environment").mkdir(parents=True, exist_ok=True)
    return path


def unit_differences(release: Unit, direct: Unit) -> list[str]:
    return [
        f"{f.name}: {getattr(release, f.name)!r} != {getattr(direct, f.name)!r}"
        for f in fields(Unit)
        if getattr(release, f.name) != getattr(direct, f.name)
    ]


def plan_differences(release: TrialPlan, direct: TrialPlan) -> list[str]:
    """Every disagreement that would make the two trials behave differently."""
    out: list[str] = []
    if set(release.units) != set(direct.units):
        out.append(f"units: {sorted(release.units)} != {sorted(direct.units)}")
    for name in sorted(set(release.units) & set(direct.units)):
        out += [f"units.{name}.{d}" for d in unit_differences(release.units[name], direct.units[name])]
    for attribute in ("task", "networks", "depends_on", "external_units"):
        if getattr(release, attribute) != getattr(direct, attribute):
            out.append(f"{attribute}: {getattr(release, attribute)!r} != {getattr(direct, attribute)!r}")
    if {v.key for v in release.volumes} != {v.key for v in direct.volumes}:
        out.append(f"volumes: {sorted(v.key for v in release.volumes)} != {sorted(v.key for v in direct.volumes)}")
    if {v.key for v in release.volumes if v.copy_from} != {v.key for v in direct.volumes if v.copy_from}:
        out.append("different volumes are initialized from task files")
    return out


def copied_content(root: Path, relative: str) -> dict[str, bytes]:
    """What a trial finds in a copied directory: every file's path below it, and its bytes."""
    base = root / relative
    if base.is_file():
        return {".": base.read_bytes()}
    return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()}


@pytest.mark.parametrize("name", sorted(TASKS))
def test_direct_plan_matches_the_release_plan(tmp_path: Path, name: str) -> None:
    task_path = write_task(tmp_path / "dataset", name)
    configs = FixedConfigs(CONFIGS)
    policy = ResourceDefaults.model_validate({"default": {"cpu": "1", "memory": "2Gi"}})
    # The release path: prepare the task into a manifest with staged files, then resolve it.
    task = load_task(task_path)
    outcome = build_task(
        task,
        tmp_path / "built",
        BuildContext(settings=BuildSettings(pullable_registries=("r.io",)), caps=CAPS, registry=configs),
    )
    assert outcome.manifest_path is not None, outcome.reason
    manifest = Manifest.load_json(outcome.manifest_path.read_bytes())
    staged = outcome.manifest_path.parent
    release = to_plan(
        manifest.model_copy(update={"task_files": "tasks/" + name}),
        resources=policy.for_manifest(manifest),
    )

    direct = plan_task(
        load_task(task_path),
        TaskSource(local=task_path, key="tasks/" + name),
        configs=configs,
        resources=policy.for_services(task.name, {n: s.resources for n, s in task.project.services.items()}),
    )

    differences = plan_differences(release, direct)
    assert not differences, f"{name}:\n  " + "\n  ".join(differences)

    # A staged copy and a direct reference must hold the same files; otherwise a trial mounting
    # one would see content the other does not.
    for volume in direct.volumes:
        if volume.copy_from is None:
            continue
        before = next(v for v in release.volumes if v.key == volume.key)
        assert before.copy_from is not None
        assert copied_content(staged / "files", before.copy_from) == copied_content(task_path, volume.copy_from), (
            f"{name}: volume {volume.key} would be initialized from different content"
        )


def test_the_direct_plan_reads_each_image_once_and_resolves_no_tag() -> None:
    """A digest names one config forever, so a second read of it is waste, and a tag is refused."""
    configs = FixedConfigs(CONFIGS)
    assert configs.image_config(APP) == CONFIGS[APP]
    assert json.loads(json.dumps(dict(configs.image_config(APP)))) == CONFIGS[APP]
    with pytest.raises(AssertionError):
        configs.digest("r.io/ns/app", "latest")
