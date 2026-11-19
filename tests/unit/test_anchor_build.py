from __future__ import annotations

import base64
import gzip
import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from flotilla import anchor_build
from flotilla.anchor_assets.extract_busybox import extract
from flotilla.anchor_build import AnchorBuildOptions, build_anchor

LOCAL = "sha256:" + "2" * 64
REMOTE = "sha256:" + "3" * 64
Context = dict[str, tuple[str, int, bytes]]
Commands = tuple[list[list[str]], list[Context]]


def _tree_snapshot(root: Path) -> dict[str, tuple[str, int, bytes]]:
    """Describe a build context before its temporary directory disappears."""
    snapshot: dict[str, tuple[str, int, bytes]] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[rel] = ("link", 0o777, os.readlink(path).encode())
        elif path.is_dir():
            snapshot[rel] = ("dir", path.stat().st_mode & 0o7777, b"")
        else:
            snapshot[rel] = ("file", path.stat().st_mode & 0o777, path.read_bytes())
    return snapshot


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> tuple[list[list[str]], list[dict[str, tuple[str, int, bytes]]]]:
    """Fake every external command; remember the calls and the staged build contexts."""
    calls: list[list[str]] = []
    contexts: list[dict[str, tuple[str, int, bytes]]] = []

    def run(args: list[str], *, cwd: Path) -> str:
        args = list(args)
        calls.append(args)
        if "extract_busybox.py" in " ".join(args):
            assert args[1] == "-I"
            assert Path(args[2]).parent == cwd
            assert Path(args[3]).parent != cwd
            assert Path(args[3]).parent != Path(args[-1]).parent
            output = Path(args[-1])
            output.write_bytes(b"verified static executable")
            output.chmod(0o755)  # the real extractor sets the mode the image inherits
        if "build" in args:
            context = Path(args[-1])
            assert context != cwd
            assert not list(context.glob("*.apk"))
            assert not (context / "extract_busybox.py").exists()
            contexts.append(_tree_snapshot(context))
        if "image" in args and "inspect" in args:
            return LOCAL
        if "copy" in args:
            Path(args[args.index("--digestfile") + 1]).write_text(REMOTE + "\n")
        if "inspect" in args:
            assert args[-1].endswith("@" + REMOTE)
            return REMOTE
        return ""

    def download(url: str, destination: Path) -> None:
        assert url.startswith("https://")
        assert not list(destination.parent.iterdir())
        destination.write_bytes(b"archive")

    monkeypatch.setattr(anchor_build, "_run", run)
    monkeypatch.setattr(anchor_build, "_download", download)
    return calls, contexts


def test_default_build_is_local_and_verifies_without_network_or_mounts(commands: Commands) -> None:
    result = build_anchor(AnchorBuildOptions("example.com/team/anchor:v1"))
    assert result.image_id == LOCAL
    assert result.published_image is None
    assert result.anchor_pin is None
    assert not any(call[0] == "skopeo" or "push" in call or "save" in call for call in commands[0])
    build = next(call for call in commands[0] if "build" in call)
    assert build[build.index("--network") + 1] == "none"
    assert not any("build-arg" in arg or "FROM" in arg for arg in build)
    run = next(call for call in commands[0] if "run" in call)
    assert run[run.index("--network") + 1] == "none"
    assert "--mount" not in run and "-v" not in run and "--env" not in run
    assert LOCAL in run and "example.com/team/anchor:v1" not in run
    assert run[run.index("--entrypoint") + 1] == "/bin/sh"
    assert run[-1] == "/.flotilla/verify.sh"


def test_push_uses_verified_id_v2s2_and_actual_remote_digest(commands: Commands, tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    result = build_anchor(
        AnchorBuildOptions(
            "registry.example:5000/team/anchor:v1",
            push=True,
            docker=("sudo", "docker"),
            skopeo=("custom-skopeo",),
            authfile=auth,
        )
    )
    save = next(call for call in commands[0] if "save" in call)
    assert save[-1] == LOCAL
    copy = next(call for call in commands[0] if "copy" in call)
    assert copy[:4] == ["custom-skopeo", "copy", "--format", "v2s2"]
    assert copy[-2].startswith("docker-archive:")
    assert str(auth) in copy
    assert result.published_image == f"registry.example:5000/team/anchor@{REMOTE}"
    assert result.anchor_pin == result.published_image
    assert not any("latest" in arg for call in commands[0] for arg in call)


def test_scratch_rootfs_layout_is_only_busybox_links_and_identity_files(commands: Commands) -> None:
    build_anchor(AnchorBuildOptions("example.com/team/anchor:v1"))
    tree = commands[1][0]
    assert tree["rootfs/.flotilla/bin/busybox"] == ("file", 0o755, b"verified static executable")
    for applet in ("sh", "sleep", "mkdir", "rm", "ls", "chown", "chmod", "cp", "mv", "true"):
        assert tree[f"rootfs/bin/{applet}"] == ("link", 0o777, b"../.flotilla/bin/busybox")
    assert tree["rootfs/etc/passwd"] == ("file", 0o644, b"root:x:0:0:root:/:/bin/sh\n")
    assert tree["rootfs/etc/group"] == ("file", 0o644, b"root:x:0:\n")
    assert tree["rootfs/tmp"] == ("dir", 0o1777, b"")
    assert tree["rootfs/.flotilla/verify.sh"][0] == "file"
    assert tree["rootfs/.flotilla/licenses/busybox-GPL-2.0.txt"][0] == "file"
    assert b"FROM scratch" in tree["Dockerfile"][2]
    assert sorted(name for name in tree if name.startswith("rootfs/bin/")) == sorted(
        f"rootfs/bin/{applet}" for applet in ("sh", "sleep", "mkdir", "rm", "ls", "chown", "chmod", "cp", "mv", "true")
    )
    top = sorted(name.removeprefix("rootfs/") for name in tree if name.startswith("rootfs/") and "/" not in name[7:])
    assert top == [".flotilla", "bin", "etc", "tmp"]
    assert sorted(name for name in tree if not name.startswith("rootfs")) == ["Dockerfile"]


def test_failed_self_containment_never_publishes(commands: Commands, monkeypatch: pytest.MonkeyPatch) -> None:
    original = anchor_build._run

    def fail(args: list[str], *, cwd: Path) -> str:
        if "run" in args:
            raise subprocess.CalledProcessError(1, args)
        return original(args, cwd=cwd)

    monkeypatch.setattr(anchor_build, "_run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        build_anchor(AnchorBuildOptions("registry/anchor:v1", push=True))
    assert not any(call[0] == "skopeo" for call in commands[0])


def test_invalid_remote_digest_is_not_reported(commands: Commands, monkeypatch: pytest.MonkeyPatch) -> None:
    original = anchor_build._run

    def run(args: list[str], *, cwd: Path) -> str:
        if args[0] == "skopeo" and "inspect" in args:
            return "not a digest"
        return original(args, cwd=cwd)

    monkeypatch.setattr(anchor_build, "_run", run)
    with pytest.raises(ValueError, match="registry did not return"):
        build_anchor(AnchorBuildOptions("registry/anchor:v1", push=True))


@pytest.mark.parametrize(
    "changes",
    [
        {"image": "repo:latest"},
        {"image": "repo"},
        {"image": "repo@" + REMOTE},
        {"busybox_url": "http://example/apk"},
        {"docker": "sudo docker"},
    ],
)
def test_invalid_options_fail_before_commands(changes: dict[str, Any], commands: Commands) -> None:
    options = {"image": "registry/anchor:v1", **changes}
    with pytest.raises(ValueError):
        build_anchor(AnchorBuildOptions(**options))
    assert not commands[0]


def _tar(files: dict[str, bytes], *, link: bool = False) -> bytes:
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w") as archive:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            if link:
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
            else:
                member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return result.getvalue()


def _apk(tmp_path: Path, *, link: bool = False) -> tuple[Path, str, str]:
    binary = b"static-busybox-fixture"
    data_gz = gzip.compress(_tar({"bin/busybox.static": binary}, link=link))
    info = ("datahash = " + hashlib.sha256(data_gz).hexdigest() + "\n").encode()
    control = gzip.compress(_tar({".PKGINFO": info}))
    archive = tmp_path / "package.apk"
    archive.write_bytes(control + data_gz)
    return archive, "Q1" + base64.b64encode(hashlib.sha1(control).digest()).decode(), hashlib.sha256(binary).hexdigest()


def test_verified_busybox_extraction(tmp_path: Path) -> None:
    archive, control, binary = _apk(tmp_path)
    output = tmp_path / "busybox"
    extract(archive, control, binary, output)
    assert output.read_bytes() == b"static-busybox-fixture"
    assert output.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("stage", ["control", "data", "binary", "truncated", "symlink"])
def test_busybox_tampering_never_writes_output(tmp_path: Path, stage: str) -> None:
    archive, control, binary = _apk(tmp_path, link=stage == "symlink")
    if stage == "control":
        control = "Q1bad"
    elif stage == "binary":
        binary = "0" * 64
    elif stage == "truncated":
        archive.write_bytes(archive.read_bytes()[:-1])
    elif stage == "data":
        blob = archive.read_bytes()
        decoder = __import__("zlib").decompressobj(16 + __import__("zlib").MAX_WBITS)
        decoder.decompress(blob)
        archive.write_bytes(blob[: len(blob) - len(decoder.unused_data)] + gzip.compress(_tar({"evil": b"x"})))
    output = tmp_path / "busybox"
    with pytest.raises(ValueError):
        extract(archive, control, binary, output)
    assert not output.exists()


def test_packaged_assets_build_a_busybox_only_anchor() -> None:
    assets = Path(anchor_build.__file__).parent / "anchor_assets"
    dockerfile = (assets / "Dockerfile").read_text()
    verify = (assets / "verify.sh").read_text()
    assert "FROM scratch" in dockerfile
    assert "pip" not in dockerfile and "apt-get" not in dockerfile and "COPY site-packages" not in dockerfile
    # The self-check runs every operation the anchor performs, including the verifier-output clear.
    for fragment in ("--list", "find", "-mindepth 1 -delete", "tar", "id -u", "/.flotilla/bin/busybox"):
        assert fragment in verify
    assert "python" not in verify
