"""Docker/buildx adapters with owned export containers and numeric archive metadata."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flotilla.build.builder import BuildRequest
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

    def copy(self, source: str, target: str) -> None:
        self._run(["buildx", "imagetools", "create", "--tag", target, source])

    def _run(self, args: Sequence[str]) -> str:
        return _docker(args, docker_cmd=self.docker_cmd, timeout_s=self.timeout_s)


@dataclass(frozen=True)
class DockerBuilder:
    """`Builder` implemented with `docker buildx build --push`.

    Pushes on build; the digest is read from `--metadata-file` (`containerimage.digest`), not parsed from logs.
    """

    docker_cmd: Sequence[str] = field(default_factory=lambda: ("docker",))
    timeout_s: float = 1800.0
    platform: str = "linux/amd64"

    def build(self, request: BuildRequest) -> str:
        with tempfile.TemporaryDirectory(prefix="flotilla-build-") as tmp:
            metadata = Path(tmp) / "metadata.json"
            args = [
                "buildx",
                "build",
                "--push",
                "--platform",
                self.platform,
                "--metadata-file",
                str(metadata),
                "--tag",
                request.tag,
            ]
            if request.dockerfile_text is not None:
                dockerfile = Path(tmp) / "Dockerfile"
                dockerfile.write_text(request.dockerfile_text)
                args += ["--file", str(dockerfile)]
            else:
                assert request.dockerfile is not None  # BuildRequest enforces exactly one
                args += ["--file", str(request.context / request.dockerfile)]
            for key, value in sorted(request.args.items()):
                args += ["--build-arg", f"{key}={value}"]
            for source, replacement in sorted(request.contexts.items()):
                args += ["--build-context", f"{source}=docker-image://{replacement}"]
            args.append(str(request.context))
            self._run(args)
            return self._digest_from(metadata, request)

    @staticmethod
    def _digest_from(metadata: Path, request: BuildRequest) -> str:
        try:
            data = json.loads(metadata.read_text())
        except (OSError, ValueError) as exc:
            raise ImageError(f"service {request.service}: cannot read build metadata: {exc}") from exc
        digest = data.get("containerimage.digest")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ImageError(f"service {request.service}: build metadata has no usable digest: {data!r}")
        return digest

    def _run(self, args: Sequence[str]) -> str:
        return _docker(args, docker_cmd=self.docker_cmd, timeout_s=self.timeout_s)


@dataclass(frozen=True)
class DockerImageExport:
    """`ImageExport` implemented with `docker create` + `docker cp`.

    Initial volume content comes from the image filesystem. `docker cp` copies from a container that is **never
    started**, so no image code runs; the container is removed in finally. `docker cp` preserves owner and mode.
    """

    docker_cmd: Sequence[str] = field(default_factory=lambda: ("docker",))
    timeout_s: float = 600.0
    ownership_dir: Path | None = None

    @contextmanager
    def _container(self, image: str) -> Iterator[str]:
        # Register before create: an interrupted or lost create response still
        # leaves the parent an exact, operation-owned cleanup target.
        name = "flotilla-export-" + uuid.uuid4().hex
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
                    original.add_note(f"export container {name} removal failed")
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

    def extract(
        self, image: str, path: str, dest: Path, *, identities: dict[str, tuple[int, int, int]] | None = None
    ) -> bool:
        with (
            self._container(image) as container,
            tempfile.TemporaryFile() as archive,
            tempfile.TemporaryFile() as errors,
        ):
            try:
                result = subprocess.run(
                    [*self.docker_cmd, "cp", f"{container}:{path.rstrip('/')}/.", "-"],
                    stdout=archive,
                    stderr=errors,
                    timeout=self.timeout_s,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise ImageError(f"image export failed: {type(error).__name__}") from None
            if result.returncode:
                errors.seek(0)
                diagnostic = errors.read(4096).decode(errors="replace")
                if "Could not find the file" in diagnostic and "in container" in diagnostic:
                    return False
                detail = " ".join(diagnostic.split())[:300]
                raise ImageError(f"image export failed with status {result.returncode}: {detail}")
            archive.seek(0)
            return extract_archive(archive, dest, identities if identities is not None else {})


def extract_archive(archive: Any, dest: Path, identities: dict[str, tuple[int, int, int]]) -> bool:
    """Keep numeric identities in metadata; extraction never needs host chown privileges."""
    directories: list[tuple[Path, int]] = []
    populated = False
    with tarfile.open(fileobj=archive, mode="r|*") as tar:
        for member in tar:
            if not (member.isfile() or member.isdir()):
                raise ImageError("image volume contains an unsupported link or special file")
            if member.size > 512 * 1024 * 1024 or min(member.uid, member.gid, member.mode) < 0 or member.mode > 0o7777:
                raise ImageError("image volume contains invalid metadata or an oversized file")
            if Path(member.name).is_absolute() or ".." in Path(member.name).parts:
                raise ImageError("image volume path escapes the export directory")
            safe = tarfile.data_filter(member, str(dest))
            target = dest / safe.name
            if not target.resolve().is_relative_to(dest.resolve()):
                raise ImageError("image volume path escapes the export directory")
            relative = str(target.relative_to(dest))
            identities[relative] = (member.uid, member.gid, member.mode)
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(exist_ok=True)
                directories.append((target, 0o700))
            else:
                source = tar.extractfile(member)
                assert source is not None
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o600)
            populated |= relative != "."
    for directory, mode in reversed(directories):
        directory.chmod(mode)
    return populated
