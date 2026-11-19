"""Build the management Anchor image: one verified static BusyBox in a scratch rootfs.

This is a CPU-side API, not part of task execution. No checkout, package index,
Harbor extra, or Docker-in-Docker is used. The anchor runs no interpreter: every
operation the framework asks of it is a BusyBox applet. The caller supplies Docker
access and (only for publishing) skopeo/registry auth.
"""

from __future__ import annotations

import re
import subprocess
import sys
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from flotilla.busybox import BUSYBOX_CONTROL, BUSYBOX_SHA256, BUSYBOX_URL

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")

#: Applets reachable through PATH: the anchor's directory commands (mkdir, rm, chown,
#: cp, mv, chmod, ls, true), the resident entrypoint (/bin/sleep), and shell access.
#: Everything else is invoked as `/.flotilla/bin/busybox <applet>`, which the multicall
#: binary serves without a link.
_PATH_APPLETS = ("sh", "sleep", "mkdir", "rm", "ls", "chown", "chmod", "cp", "mv", "true")


@dataclass(frozen=True)
class AnchorBuildOptions:
    """Build options; commands are argv prefixes, never shell command strings.

    ``busybox_url`` can select a mirror of the same pinned APK; its content hashes
    cannot be overridden. Only linux/amd64 is supported. Authentication stays
    outside the build context and is passed only to skopeo.
    """

    image: str
    push: bool = False
    docker: tuple[str, ...] = ("docker",)
    skopeo: tuple[str, ...] = ("skopeo",)
    authfile: Path | None = None
    manifest_format: str = "v2s2"
    busybox_url: str = BUSYBOX_URL


@dataclass(frozen=True)
class AnchorBuildResult:
    image: str
    image_id: str
    remote_digest: str | None
    published_image: str | None

    @property
    def anchor_pin(self) -> str | None:
        """What to put in the spec's `reaper.anchor_image`, or None if nothing was published.

        A local image ID is not a pin: the platform pulls by remote digest, so only a published
        reference can appear in a deployment.
        """
        return self.published_image


def _run(args: Sequence[str], *, cwd: Path) -> str:
    result = subprocess.run(list(args), cwd=cwd, check=True, text=True, stdout=subprocess.PIPE)
    return result.stdout.strip()


def _validate(options: AnchorBuildOptions) -> None:
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


def _stage_rootfs(context: Path, scripts: Path, downloads: Path, options: AnchorBuildOptions) -> None:
    """Lay out the scratch-image rootfs: busybox, PATH links, passwd/group, license, self-check."""
    assets = Path(__file__).parent / "anchor_assets"
    rootfs = context / "rootfs"
    for directory in (
        rootfs / ".flotilla/bin",
        rootfs / ".flotilla/licenses",
        rootfs / "bin",
        rootfs / "etc",
        rootfs / "tmp",
    ):
        directory.mkdir(parents=True)
    (context / "Dockerfile").write_bytes((assets / "Dockerfile").read_bytes())
    # Identity files: execd resolves HOME through /etc/passwd, and ownership changes read
    # /etc/group, in every sandbox including the anchor.
    (rootfs / "etc/passwd").write_text("root:x:0:0:root:/:/bin/sh\n")
    (rootfs / "etc/group").write_text("root:x:0:\n")
    for name in ("passwd", "group"):
        (rootfs / "etc" / name).chmod(0o644)
    (rootfs / "tmp").chmod(0o1777)
    (rootfs / ".flotilla/licenses/busybox-GPL-2.0.txt").write_bytes((assets / "GPL-2.0.txt").read_bytes())
    (rootfs / ".flotilla/verify.sh").write_bytes((assets / "verify.sh").read_bytes())
    archive = downloads / "busybox.apk"
    _download(options.busybox_url, archive)
    # Extract straight into the rootfs; no interpreter runs inside the download directory.
    _run(
        [
            sys.executable,
            "-I",
            str(scripts / "extract_busybox.py"),
            str(archive),
            BUSYBOX_CONTROL,
            BUSYBOX_SHA256,
            str(rootfs / ".flotilla/bin/busybox"),
        ],
        cwd=scripts,
    )
    for applet in _PATH_APPLETS:
        (rootfs / "bin" / applet).symlink_to("../.flotilla/bin/busybox")


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
    is performed. The build pulls nothing: the image is scratch plus a verified
    binary, and the self-containment test has no network access.
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
        (scripts / "extract_busybox.py").write_bytes(
            (Path(__file__).parent / "anchor_assets" / "extract_busybox.py").read_bytes()
        )
        _stage_rootfs(context, scripts, downloads, options)
        _run(
            [
                *options.docker,
                "build",
                "--platform",
                "linux/amd64",
                "--network",
                "none",
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
                "/bin/sh",
                image_id,
                "/.flotilla/verify.sh",
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
        return AnchorBuildResult(options.image, image_id, remote_digest, published)
