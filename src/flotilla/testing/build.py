"""Task file export and the offline build driver used by `flotilla prepare`.

Fake registry / builder / image exporter for driver tests; no network or docker. They verify ordering, dispositions,
skips and error attribution.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from flotilla.build.builder import BuildRequest
from flotilla.build.driver import BuildContext, build_task
from flotilla.build.images import BuildSettings
from flotilla.compose.task import HarborTask
from flotilla.config import FlotillaConfig
from flotilla.manifest import Manifest
from flotilla.platform.base import Capabilities
from flotilla.prepare.inspect import main_only
from flotilla.prepared import ROLES, PreparedEnvironment, PreparedTask, dump_row

DIGEST = "sha256:" + "ab" * 32
DIGEST2 = "sha256:" + "cd" * 32
PULLABLE = ("r.io",)


class FakeRegistry:
    """Registry double with a publication ledger shared with :class:`FakeBuilder`.

    Copies and pushes record the digest a real registry would serve afterwards, so
    digest reads after a publication resolve to the published manifest.
    """

    def __init__(
        self,
        *,
        missing: Sequence[str] = (),
        digests: Mapping[str, str] | None = None,
        user: str = "0:0",
        published: dict[str, str] | None = None,
    ) -> None:
        self._missing = set(missing)
        self.configs = 0
        self.digests = dict(digests or {})
        self.user = user
        self.published = published if published is not None else {}
        self.resolved: list[tuple[str, str]] = []
        self.copied: list[tuple[str, str]] = []

    def digest(self, name: str, reference: str) -> str:
        from flotilla.build.images import ImageError

        key = f"{name}@{reference}" if reference.startswith("sha256:") else f"{name}:{reference}"
        pushed = self.published.get(key)
        if pushed is not None:
            return pushed
        if reference.startswith("sha256:"):
            return reference  # digest references are self-describing
        if any(m in f"{name}:{reference}" for m in self._missing):
            raise ImageError(f"not found: {name}:{reference}")
        self.resolved.append((name, reference))
        return self.digests.get(f"{name}:{reference}", DIGEST)

    def image_config(self, pinned: str) -> Mapping[str, Any]:
        self.configs += 1
        return {"Cmd": ["/bin/bash"], "WorkingDir": "/app", "User": self.user}

    def copy(self, source: str, target: str) -> None:
        self.copied.append((source, target))
        self.published[target] = source.rpartition("@")[2]


class FakeBuilder:
    def __init__(self, *, digests: Sequence[str] = (), published: dict[str, str] | None = None) -> None:
        self.requests: list[BuildRequest] = []
        self._digests = iter(digests)
        #: tag -> manifest digest, shared with the registry when wired by `_ctx`.
        self.published = published if published is not None else {}

    def build(self, request: BuildRequest) -> str:
        self.requests.append(request)
        digest = next(self._digests, DIGEST)
        self.published[request.tag] = digest
        return digest


class FakeExport:
    """Image content exporter: `contents` maps mount point -> {file name: content}.

    `links` names container paths that exist as symlinks; `validate_mount_paths`
    rejects mount targets under them exactly like the Docker exporter would.
    """

    def __init__(
        self,
        contents: Mapping[str, Mapping[str, str]] | None = None,
        *,
        links: Sequence[str] = (),
    ) -> None:
        self.contents = contents or {}
        self.links = frozenset(links)
        self.inspected: list[tuple[str, tuple[str, ...]]] = []

    def validate_mount_paths(self, image: str, paths: tuple[str, ...]) -> None:
        from flotilla.build.images import ImageError

        self.inspected.append((image, tuple(paths)))
        for value in paths:
            parts = value.split("/")[1:] if value.startswith("/") else ()
            for index in range(1, len(parts) + 1):
                component = "/" + "/".join(parts[:index])
                if component in self.links:
                    raise ImageError(f"image mount path is a symlink or non-directory: {component}")

    def extract(
        self, image: str, path: str, dest: Path, *, identities: dict[str, tuple[int, int, int]] | None = None
    ) -> bool:
        files = self.contents.get(path)
        if not files:
            return False
        for name, text in files.items():
            (dest / name).write_text(text)
            if identities is not None:
                identities[name] = (1000, 1000, 0o644)
        return True


def _caps() -> Capabilities:
    return Capabilities(
        max_label_value_len=63,
        list_visibility_s=30.0,
        max_ttl_seconds=21600,
        link=True,
        link_udp=True,
        link_max_members=10,
        inbound_isolation=True,
        external_forms=frozenset({"none", "allowlist"}),
        implicit_egress=(),
        internal_address="exec",
        shared_volume=True,
        volume_file=True,
        no_auto_mount=True,
        exec_auth=True,
        privileged_runtime=False,
        devices=frozenset(),
    )


def _task(root: Path, name: str, *, toml: str = "", compose: str | None = None, **files: str) -> Path:
    path = root / name
    (path / "environment").mkdir(parents=True)
    (path / "task.toml").write_text(f'[task]\nname = "ds/{name}"\n{toml}')
    if compose is not None:
        (path / "environment" / "docker-compose.yaml").write_text(compose)
    for rel, text in files.items():
        target = path / "environment" / rel.replace("__", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return path


def _ctx(**kw: Any) -> BuildContext:
    builder = kw.get("builder", FakeBuilder())
    registry = kw.get("registry", FakeRegistry())
    export = kw.get("image_export", FakeExport())
    if isinstance(builder, FakeBuilder) and isinstance(registry, FakeRegistry):
        # One publication ledger per context: builder pushes are visible to digest reads.
        registry.published = builder.published
    defaults: dict[str, Any] = {
        "settings": BuildSettings(pullable_registries=PULLABLE, target="r.io/ns/out"),
        "caps": _caps(),
        "registry": registry,
        "builder": builder,
        "image_export": export,
    }
    inspector = getattr(export, "validate_mount_paths", None)
    if inspector is not None:
        defaults["validate_mount_paths"] = inspector
    return BuildContext(**{**defaults, **kw})


# Public factory for downstream neutral environment contracts.
build_context = _ctx


def prepared_task(
    path: Path,
    cfg: FlotillaConfig | None = None,
    *,
    environments: Callable[[Path], Sequence[HarborTask]] = main_only,
    **ctx: Any,
) -> PreparedTask:
    """Prepare a task directory into a row with the fakes above, as `flotilla prepare` would.

    For fixtures without task files; ones with files go through `flotilla.prepare.prepare` to be published.
    """
    caps = cfg.platform_capabilities() if cfg is not None else _caps()
    prepared: dict[str, PreparedEnvironment] = {}
    with TemporaryDirectory(prefix="flotilla-prepared-") as out:
        for role, env in zip(ROLES, environments(path), strict=False):
            outcome = build_task(env, Path(out) / role, _ctx(caps=caps, **ctx))
            if outcome.manifest_path is None:
                raise ValueError(f"environment preparation {outcome.status}: {outcome.reason}")
            if outcome.needs_publish:
                raise ValueError("fixture has task files; prepare it with flotilla.prepare.prepare")
            prepared[role] = PreparedEnvironment(Manifest.load_json(outcome.manifest_path.read_bytes()))
    resolved = path.resolve()
    return PreparedTask(f"{resolved.parent.name}/{resolved.name}", str(resolved), prepared)


def prepared_environment(path: Path, cfg: FlotillaConfig | None = None, **ctx: Any) -> PreparedEnvironment:
    """The main environment of `prepared_task(path, cfg)`."""
    return prepared_task(path, cfg, **ctx).environment()


def write_prepared(out: Path, rows: Sequence[PreparedTask]) -> Path:
    out.write_text("".join(dump_row(row) + "\n" for row in rows))
    return out
