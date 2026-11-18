"""Docker adapters: registry reads and the image account databases, with owned containers."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flotilla.build.images import _DIGEST, ImageError
from flotilla.build.meta import ImageAccounts


def _docker(args: Sequence[str], *, docker_cmd: Sequence[str], timeout_s: float) -> str:
    """Run a docker command and return stdout. Failures raise `ImageError` (with a stderr excerpt)."""
    cmd = [*docker_cmd, *args]
    # Build arguments can contain credentials. Hide their values in both the
    # rendered command and Docker's echoed diagnostics.
    private = [args[i + 1].partition("=")[2] for i, arg in enumerate(args[:-1]) if arg == "--build-arg"]

    def redact(text: str) -> str:
        for value in sorted(filter(None, private), key=len, reverse=True):
            text = text.replace(value, "<redacted>")
        return text

    command = redact(" ".join(args))
    try:
        # Arguments are assembled here without a shell; image refs come from parsed ImageRefs and the target.
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImageError(f"docker command failed ({command}): {redact(str(exc))}") from None
    if result.returncode != 0:
        stderr = redact(result.stderr.strip())
        detail = stderr if len(stderr) <= 1800 else stderr[:300] + "\n...\n" + stderr[-1500:]
        raise ImageError(f"docker command exited {result.returncode} ({command}): {detail}")
    return result.stdout


@dataclass(frozen=True)
class DockerRegistry:
    """`Registry` implemented with `docker buildx imagetools`.

    `docker_cmd` is the docker executable plus leading args, e.g. `("sudo", "docker", "--config", "/home/u/.docker")`.
    `timeout_s` applies per command.
    """

    docker_cmd: Sequence[str] = field(default_factory=lambda: ("docker",))
    timeout_s: float = 300.0

    def digest(self, name: str, reference: str) -> str:
        ref = f"{name}@{reference}" if reference.startswith("sha256:") else f"{name}:{reference}"
        out = self._run(["buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest.Digest}}"])
        digest = out.strip().strip('"')
        if not _DIGEST.fullmatch(digest):
            raise ImageError(f"cannot parse digest from {ref} (imagetools output: {out[:200]!r})")
        return digest

    def image_config(self, pinned: str) -> Mapping[str, Any]:
        """The `config` section of the image config. Reads only the manifest and config blob, not layers."""
        config = self._image(pinned).get("config")
        if not isinstance(config, Mapping):
            raise ImageError(f"image config of {pinned} has no config section")
        return config

    def _image(self, pinned: str) -> Mapping[str, Any]:
        """The full image config (with `config` and `history`). Picks linux/amd64 for multi-arch images."""
        out = self._run(["buildx", "imagetools", "inspect", pinned, "--format", "{{json .Image}}"])
        try:
            image = json.loads(out)
        except ValueError as exc:
            raise ImageError(f"cannot parse image config of {pinned}: {exc}") from exc
        # For multi-arch images `.Image` maps platform -> config; pick linux/amd64 (build and run are x86_64).
        if isinstance(image, Mapping) and "config" not in image:
            entry = image.get("linux/amd64")
            if not isinstance(entry, Mapping) or "config" not in entry:
                raise ImageError("image has no linux/amd64 configuration")
            image = entry
        if not isinstance(image, Mapping):
            raise ImageError(f"malformed image config of {pinned}: {out[:200]!r}")
        if image.get("architecture", "amd64") != "amd64" or image.get("os", "linux") != "linux":
            raise ImageError("image has no linux/amd64 configuration")
        return image

    def _run(self, args: Sequence[str]) -> str:
        return _docker(args, docker_cmd=self.docker_cmd, timeout_s=self.timeout_s)


@dataclass(frozen=True)
class DockerImageAccounts:
    """Reads an image's `/etc/passwd` and `/etc/group` with `docker create` + `docker cp`.

    Named users in Compose `user:` resolve to numeric ids through them. `docker cp` copies from a
    container that is **never started**, so no image code runs; the container is removed in finally.
    """

    docker_cmd: Sequence[str] = field(default_factory=lambda: ("docker",))
    timeout_s: float = 600.0
    ownership_dir: Path | None = None

    @contextmanager
    def _container(self, image: str) -> Iterator[str]:
        # Register before create: an interrupted or lost create response still
        # leaves the parent an exact, operation-owned cleanup target.
        name = "flotilla-accounts-" + uuid.uuid4().hex
        record = None
        if self.ownership_dir is not None:
            self.ownership_dir.mkdir(parents=True, exist_ok=True)
            record = self.ownership_dir / name
            record.touch()
        try:
            _docker(["create", "--name", name, image, "true"], docker_cmd=self.docker_cmd, timeout_s=self.timeout_s)
            yield name
        finally:
            # Never suppress a cleanup failure and remove its journal entry.
            original = sys.exception()
            removed = True
            try:
                _docker(["rm", "-f", "-v", name], docker_cmd=self.docker_cmd, timeout_s=self.timeout_s)
            except ImageError as error:
                if "No such container" not in str(error):
                    if original is None:
                        raise
                    original.add_note(f"container {name} removal failed")
                    removed = False
            if record is not None and removed:
                record.unlink(missing_ok=True)

    def accounts(self, image: str) -> ImageAccounts:
        with self._container(image) as container, tempfile.TemporaryDirectory(prefix="flotilla-identity-") as temporary:
            values = []
            for name in ("passwd", "group"):
                target = Path(temporary) / name
                try:
                    _docker(
                        ["cp", f"{container}:/etc/{name}", str(target)],
                        docker_cmd=self.docker_cmd,
                        timeout_s=self.timeout_s,
                    )
                except ImageError as exc:
                    if "Could not find the file" in str(exc) and "in container" in str(exc):
                        values.append("")
                        continue
                    raise
                if target.is_symlink() or not target.is_file():
                    raise ImageError(f"image /etc/{name} must be a regular file")
                values.append(target.read_text())
            return ImageAccounts(*values)
