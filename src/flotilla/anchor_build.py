"""Build a self-contained management Anchor from an installed Flotilla distribution.

This is a CPU-side API, not part of task execution. No checkout, package index,
Harbor extra, or Docker-in-Docker is used. The caller supplies an immutable Python
3.12 Linux/amd64 base, Docker access, and (only for publishing) skopeo/registry auth.
The installed runtime dependency closure is copied, not resolved or upgraded.
Editable installs are deliberately rejected: install a wheel before building.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import metadata, resources
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

BUSYBOX_URL = "https://dl-cdn.alpinelinux.org/alpine/v3.20/main/x86_64/busybox-static-1.36.1-r31.apk"
BUSYBOX_CONTROL = "Q1hZXWQJ7C5Eu/FFlBqrTGUqcycsM="
BUSYBOX_SHA256 = "6d4ae568988ee24beb9dac4afdac4df67f90bbaed6ca47628da35ff5eb632a4c"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class AnchorBuildOptions:
    """Build options; commands are argv prefixes, never shell command strings.

    ``python_image`` must be a repository@sha256 reference with Python 3.12 and
    its stdlib venv module. ``busybox_url`` can select a mirror of the same pinned
    APK; its content hashes cannot be overridden. Only linux/amd64 is supported.
    Authentication stays outside the build context and is passed only to skopeo.
    """

    image: str
    python_image: str
    push: bool = False
    docker: tuple[str, ...] = ("docker",)
    skopeo: tuple[str, ...] = ("skopeo",)
    authfile: Path | None = None
    manifest_format: str = "v2s2"
    busybox_url: str = BUSYBOX_URL


@dataclass(frozen=True)
class AnchorBuildResult:
    image: str
    version: str
    image_id: str
    remote_digest: str | None
    published_image: str | None

    @property
    def deployment_toml(self) -> str | None:
        """Only remotely published digests are suitable for deployment pins."""
        if self.published_image is None:
            return None
        return f"[reaper]\nanchor_image = {json.dumps(self.published_image)}\n"


def _run(args: Sequence[str], *, cwd: Path) -> str:
    result = subprocess.run(list(args), cwd=cwd, check=True, text=True, stdout=subprocess.PIPE)
    return result.stdout.strip()


def _validate(options: AnchorBuildOptions) -> None:
    repo, sep, digest = options.python_image.partition("@")
    if not repo or not sep or not _DIGEST.fullmatch(digest) or any(c.isspace() for c in repo):
        raise ValueError("python_image must be an immutable repository@sha256:<64 lowercase hex> reference")
    # Require an explicit non-latest output tag, so publishing cannot move latest.
    image_repo, sep, tag = options.image.rpartition(":")
    if (
        not sep
        or not image_repo
        or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag)
        or tag.lower() == "latest"
        or "@" in options.image
        or options.image.startswith("-")
        or any(c.isspace() for c in options.image)
    ):
        raise ValueError("image must have an explicit version tag (not latest or a digest)")
    if options.manifest_format not in {"v2s2", "oci", "v2s1"}:
        raise ValueError("manifest_format must be v2s2, oci, or v2s1")
    for name, command in (("docker", options.docker), ("skopeo", options.skopeo)):
        if isinstance(command, str) or not command or any(not arg for arg in command):
            raise ValueError(f"{name} must be a nonempty argv sequence")
    if not options.busybox_url.startswith("https://"):
        raise ValueError("busybox_url must use HTTPS; the pinned hashes are always checked")
    if options.authfile is not None and not options.authfile.is_file():
        raise ValueError("authfile must be an existing file")


def _installed_distributions() -> list[metadata.Distribution]:
    # Build-only dependency. Runtime imports never need the packaging resolver.
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    pending = ["flotilla"]
    found: dict[str, metadata.Distribution] = {}
    while pending:
        name = canonicalize_name(pending.pop())
        if name in found:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError as exc:
            raise ValueError(f"Anchor build requires installed runtime dependency {name!r}") from exc
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
        if direct.get("dir_info", {}).get("editable"):
            raise ValueError(f"{name} is editable; install a non-editable wheel before building an Anchor")
        if name == "flotilla" and not any(str(p) == "flotilla/anchor_build.py" for p in dist.files or ()):
            raise ValueError("installed flotilla is not this framework; install its fixed Git revision or wheel")
        found[name] = dist
        for text in dist.requires or ():
            requirement = Requirement(text)
            if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
                continue
            if requirement.extras:
                raise ValueError(f"runtime dependency extras are not supported by Anchor snapshots: {text}")
            installed = metadata.version(requirement.name)
            if requirement.specifier and not requirement.specifier.contains(installed, prereleases=True):
                raise ValueError(f"installed {requirement.name}=={installed} does not satisfy {text}")
            pending.append(requirement.name)
    return [found[name] for name in sorted(found)]


def _snapshot_installed(target: Path) -> dict[str, object]:
    """Copy RECORD-owned files, verifying hashes; never walk an installation root.

    Console scripts are not copied (their shebangs refer to the host environment).
    Python entry points remain in metadata. Generated bytecode and installer-local
    bookkeeping are omitted; RECORD is regenerated for the installed snapshot.
    """
    if sys.version_info[:2] != (3, 12) or sys.platform != "linux" or os.uname().machine != "x86_64":
        raise ValueError("Anchor snapshots currently require Linux x86_64 and Python 3.12")
    target.mkdir()
    distributions: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for dist in _installed_distributions():
        name = dist.metadata["Name"]
        distributions[name] = dist.version
        records = dist.read_text("RECORD")
        if not records:
            raise ValueError(f"{name} has no wheel RECORD; install a non-editable wheel")
        root = Path(str(dist.locate_file(""))).resolve()
        output_records: list[tuple[str, str, str]] = []
        record_path: str | None = None
        for rel, digest, size in csv.reader(io.StringIO(records)):
            path = PurePosixPath(rel)
            if path.is_absolute() or ".." in path.parts:
                # pip's generated console scripts live outside site-packages.
                if not path.is_absolute() and path.parts[:3] == ("..", "..", "..") and path.parts[3:4] == ("bin",):
                    continue
                raise ValueError(f"unsupported installed data outside site-packages: {name}: {rel}")
            if path.as_posix() != rel or not path.parts:
                raise ValueError(f"noncanonical installed path: {name}: {rel}")
            if path.name == "RECORD" and path.parent.name.endswith(".dist-info"):
                record_path = rel
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if not digest:
                # pip-generated, not part of the wheel's installed content.
                if path.parent.name.endswith(".dist-info") and path.name in {"INSTALLER", "REQUESTED"}:
                    continue
                raise ValueError(f"unhashed installed file: {name}: {rel}")
            source = Path(str(dist.locate_file(rel)))
            if source.is_symlink() or not source.resolve().is_relative_to(root):
                raise ValueError(f"installed file escapes its distribution root: {name}: {rel}")
            blob = source.read_bytes()
            algorithm, expected = digest.split("=", 1)
            if algorithm not in {"sha256", "sha384", "sha512"}:
                raise ValueError(f"unsupported RECORD hash algorithm: {algorithm}")
            actual = base64.urlsafe_b64encode(hashlib.new(algorithm, blob).digest()).rstrip(b"=").decode()
            if actual != expected or (size and len(blob) != int(size)):
                raise ValueError(f"installed file differs from RECORD: {name}: {rel}")
            sha256 = hashlib.sha256(blob).hexdigest()
            if rel in hashes and hashes[rel] != sha256:
                raise ValueError(f"installed distributions disagree on shared file {rel}")
            hashes[rel] = sha256
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blob)
            dest.chmod(source.stat().st_mode & 0o777)
            output_records.append((rel, digest, str(len(blob))))
        if record_path is None:
            raise ValueError(f"{name} RECORD does not list itself")
        with (target / record_path).open("w", newline="") as stream:
            csv.writer(stream).writerows([*output_records, (record_path, "", "")])
    return {"distributions": distributions, "files": hashes}


def _download(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response, destination.open("xb") as output:
        if not response.url.startswith("https://"):
            raise ValueError("BusyBox download redirected away from HTTPS")
        # The pinned APK is small; don't fill a disk on an unexpected response.
        remaining = 32 * 1024 * 1024
        while chunk := response.read(min(1024 * 1024, remaining + 1)):
            remaining -= len(chunk)
            if remaining < 0:
                raise ValueError("BusyBox APK exceeds 32 MiB")
            output.write(chunk)


def build_anchor(options: AnchorBuildOptions) -> AnchorBuildResult:
    """Build, verify offline in a fresh container, and optionally publish.

    Failures raise before publication. Local images are retained for diagnosis.
    Publishing uses skopeo's requested format (v2s2 by default), then reads the
    actual remote manifest digest. No latest update, config edit, or Git action
    is performed. Docker build needs registry read access to the pinned base;
    its RUN steps and the self-containment test have no network access.
    """
    _validate(options)
    # Three sibling directories: downloaded archives cannot shadow Python imports
    # or plant Dockerfile/build assets. No interpreter runs inside downloads.
    with (
        TemporaryDirectory(prefix="flotilla-anchor-context-") as context_dir,
        TemporaryDirectory(prefix="flotilla-anchor-scripts-") as scripts_dir,
        TemporaryDirectory(prefix="flotilla-anchor-download-") as download_dir,
    ):
        context, scripts, downloads = Path(context_dir), Path(scripts_dir), Path(download_dir)
        manifest = _snapshot_installed(context / "site-packages")
        # Assets must come from the SAME installed distribution as the snapshot,
        # not a PYTHONPATH overlay or a development checkout.
        installed_package = context / "site-packages/flotilla"
        if (
            resources.files("flotilla").joinpath("anchor_build.py").read_bytes()
            != (installed_package / "anchor_build.py").read_bytes()
        ):
            raise ValueError("imported Anchor builder differs from installed Flotilla; remove the source override")
        installed_assets = installed_package / "anchor_assets"
        package_assets = resources.files("flotilla.anchor_assets")
        for name in ("Dockerfile", "verify.py", "extract_busybox.py", "GPL-2.0.txt"):
            blob = package_assets.joinpath(name).read_bytes()
            if blob != (installed_assets / name).read_bytes():
                raise ValueError("imported Anchor assets differ from installed Flotilla; remove the source override")
            destination = context if name != "extract_busybox.py" else scripts
            (destination / name).write_bytes(blob)
        (context / "snapshot.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
        archive = downloads / "busybox.apk"
        _download(options.busybox_url, archive)
        _run(
            [
                sys.executable,
                "-I",
                str(scripts / "extract_busybox.py"),
                str(archive),
                BUSYBOX_CONTROL,
                BUSYBOX_SHA256,
                str(context / "busybox"),
            ],
            cwd=scripts,
        )
        _run(
            [
                *options.docker,
                "build",
                "--platform",
                "linux/amd64",
                "--network",
                "none",
                "--pull=false",
                "--build-arg",
                f"PYTHON_IMAGE={options.python_image}",
                "--tag",
                options.image,
                str(context),
            ],
            cwd=scripts,
        )
        image_id = _run([*options.docker, "image", "inspect", "--format", "{{.Id}}", options.image], cwd=scripts)
        if not _DIGEST.fullmatch(image_id):
            raise ValueError(f"Docker did not return an image ID: {image_id!r}")
        # Use the immutable local ID, never a tag another build could replace.
        _run(
            [
                *options.docker,
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "/usr/bin/python3",
                image_id,
                "-I",
                "/opt/flotilla/verify.py",
            ],
            cwd=scripts,
        )
        remote_digest = published = None
        if options.push:
            # Export by ID so a concurrent retag cannot publish an unchecked image.
            archive_path = scripts / "anchor.tar"
            _run([*options.docker, "save", "--output", str(archive_path), image_id], cwd=scripts)
            auth = ["--authfile", str(options.authfile.resolve())] if options.authfile else []
            digest_file = scripts / "remote-digest"
            _run(
                [
                    *options.skopeo,
                    "copy",
                    "--format",
                    options.manifest_format,
                    "--digestfile",
                    str(digest_file),
                    *auth,
                    f"docker-archive:{archive_path}",
                    f"docker://{options.image}",
                ],
                cwd=scripts,
            )
            copied_digest = digest_file.read_text().strip()
            if not _DIGEST.fullmatch(copied_digest):
                raise ValueError(f"skopeo did not record a destination digest: {copied_digest!r}")
            published = f"{options.image.rsplit(':', 1)[0]}@{copied_digest}"
            remote_digest = _run(
                [*options.skopeo, "inspect", *auth, "--format", "{{.Digest}}", f"docker://{published}"], cwd=scripts
            )
            if not _DIGEST.fullmatch(remote_digest) or remote_digest != copied_digest:
                raise ValueError(f"registry did not return the published manifest digest: {remote_digest!r}")
        versions = manifest["distributions"]
        assert isinstance(versions, dict)
        return AnchorBuildResult(options.image, versions["flotilla"], image_id, remote_digest, published)
