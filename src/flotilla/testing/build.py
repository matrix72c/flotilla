"""Task file export and the `flotilla build` driver.

Fake registry / builder / image exporter for driver tests; no network or docker. They verify ordering, dispositions,
skips and error attribution.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from flotilla.build.builder import BuildRequest
from flotilla.build.driver import BuildContext
from flotilla.build.images import BuildSettings
from flotilla.platform.base import Capabilities

DIGEST = "sha256:" + "ab" * 32
DIGEST2 = "sha256:" + "cd" * 32
PULLABLE = ("r.io",)


class FakeRegistry:
    def __init__(
        self,
        *,
        history: Sequence[Mapping[str, Any]] = (),
        missing: Sequence[str] = (),
        digests: Mapping[str, str] | None = None,
        user: str = "0:0",
    ) -> None:
        self._history = list(history)
        self._missing = set(missing)
        self.configs = 0
        self.digests = dict(digests or {})
        self.user = user
        self.resolved: list[tuple[str, str]] = []
        self.copied: list[tuple[str, str]] = []
        self.histories: list[str] = []

    def digest(self, name: str, reference: str) -> str:
        from flotilla.build.images import ImageError

        ref = f"{name}:{reference}"
        if any(m in ref for m in self._missing):
            raise ImageError(f"not found: {ref}")
        self.resolved.append((name, reference))
        return self.digests.get(ref, DIGEST)

    def image_config(self, pinned: str) -> Mapping[str, Any]:
        self.configs += 1
        return {"Cmd": ["/bin/bash"], "WorkingDir": "/app", "User": self.user}

    def image_history(self, pinned: str) -> Sequence[Mapping[str, Any]]:
        from flotilla.build.images import ImageError

        if any(m in pinned for m in self._missing):
            raise ImageError(f"not found: {pinned}")
        self.histories.append(pinned)
        return self._history

    def copy(self, source: str, target: str) -> None:
        self.copied.append((source, target))


class FakeBuilder:
    def __init__(self, *, digests: Sequence[str] = ()) -> None:
        self.requests: list[BuildRequest] = []
        self._digests = iter(digests)

    def build(self, request: BuildRequest) -> str:
        self.requests.append(request)
        return next(self._digests, DIGEST)


class FakeExport:
    """Image content exporter: `contents` maps mount point -> {file name: content}."""

    def __init__(self, contents: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self.contents = contents or {}

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
    defaults: dict[str, Any] = {
        "settings": BuildSettings(pullable_registries=PULLABLE, target="r.io/ns/out"),
        "caps": _caps(),
        "registry": FakeRegistry(),
        "builder": FakeBuilder(),
        "image_export": FakeExport(),
    }
    return BuildContext(**{**defaults, **kw})


# Public factory for downstream neutral environment contracts.
build_context = _ctx
