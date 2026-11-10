from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import io
import json
import subprocess
import tarfile
from importlib import metadata, resources
from pathlib import Path
from typing import Any

import pytest

from flotilla import anchor_build
from flotilla.anchor_assets.extract_busybox import extract
from flotilla.anchor_build import AnchorBuildOptions, build_anchor

BASE = "python@sha256:" + "1" * 64
LOCAL = "sha256:" + "2" * 64
REMOTE = "sha256:" + "3" * 64


def _distribution(root: Path, name: str = "flotilla", requires: tuple[str, ...] = ()) -> metadata.Distribution:
    info = root / f"{name}-1.2.3.dist-info"
    info.mkdir(parents=True)
    package = root / name
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("# installed bytes\n")
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.2.3\n"
        + "".join(f"Requires-Dist: {item}\n" for item in requires)
    )
    (info / "WHEEL").write_text("Wheel-Version: 1.0\nTag: py3-none-any\n")
    (info / "direct_url.json").write_text(
        json.dumps(
            {
                "url": "https://example.com/source.git",
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": "a" * 40,
                },
            }
        )
    )
    if name == "flotilla":
        (package / "anchor_build.py").write_text("# package identity marker\n")
    records = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and (path.is_relative_to(info) or path.is_relative_to(package)):
            blob = path.read_bytes()
            digest = base64.urlsafe_b64encode(hashlib.sha256(blob).digest()).rstrip(b"=").decode()
            records.append((path.relative_to(root).as_posix(), "sha256=" + digest, str(len(blob))))
    records.append((f"{info.name}/RECORD", "", ""))
    with (info / "RECORD").open("w", newline="") as stream:
        csv.writer(stream).writerows(records)
    return metadata.Distribution.at(info)


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def snapshot(target: Path) -> dict[str, object]:
        target.mkdir()
        assets = target / "flotilla/anchor_assets"
        assets.mkdir(parents=True)
        (assets.parent / "anchor_build.py").write_bytes(
            resources.files("flotilla").joinpath("anchor_build.py").read_bytes()
        )
        for name in ("Dockerfile", "verify.py", "extract_busybox.py", "GPL-2.0.txt"):
            (assets / name).write_bytes(resources.files("flotilla.anchor_assets").joinpath(name).read_bytes())
        return {"distributions": {"flotilla": "1.2.3"}, "files": {}}

    def run(args: list[str], *, cwd: Path) -> str:
        args = list(args)
        calls.append(args)
        if "extract_busybox.py" in " ".join(args):
            assert args[1] == "-I"
            assert Path(args[2]).parent == cwd
            assert Path(args[3]).parent != cwd
            assert Path(args[3]).parent != Path(args[-1]).parent
            Path(args[-1]).write_bytes(b"verified static executable")
        if "build" in args:
            context = Path(args[-1])
            assert context != cwd
            assert not list(context.glob("*.apk"))
            assert not (context / "extract_busybox.py").exists()
            assert (context / "busybox").is_file()
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

    monkeypatch.setattr(anchor_build, "_snapshot_installed", snapshot)
    monkeypatch.setattr(anchor_build, "_run", run)
    monkeypatch.setattr(anchor_build, "_download", download)
    return calls


def test_default_build_is_local_and_verifies_without_network_or_mounts(commands: list[list[str]]) -> None:
    result = build_anchor(AnchorBuildOptions("example.com/team/anchor:v1", BASE))
    assert result.image_id == LOCAL
    assert result.version == "1.2.3"
    assert result.published_image is None
    assert result.deployment_toml is None
    assert not any(call[0] == "skopeo" or "push" in call or "save" in call for call in commands)
    build = next(call for call in commands if "build" in call)
    assert build[build.index("--network") + 1] == "none"
    run = next(call for call in commands if "run" in call)
    assert run[run.index("--network") + 1] == "none"
    assert "--mount" not in run and "-v" not in run and "--env" not in run
    assert LOCAL in run and "example.com/team/anchor:v1" not in run
    assert run[-2:] == ["-I", "/opt/flotilla/verify.py"]


def test_push_uses_verified_id_v2s2_and_actual_remote_digest(commands: list[list[str]], tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    auth.write_text("{}")
    result = build_anchor(
        AnchorBuildOptions(
            "registry.example:5000/team/anchor:v1",
            BASE,
            push=True,
            docker=("sudo", "docker"),
            skopeo=("custom-skopeo",),
            authfile=auth,
        )
    )
    save = next(call for call in commands if "save" in call)
    assert save[-1] == LOCAL
    copy = next(call for call in commands if "copy" in call)
    assert copy[:4] == ["custom-skopeo", "copy", "--format", "v2s2"]
    assert copy[-2].startswith("docker-archive:")
    assert str(auth) in copy
    assert result.published_image == f"registry.example:5000/team/anchor@{REMOTE}"
    assert result.deployment_toml == f'[reaper]\nanchor_image = "{result.published_image}"\n'
    assert not any("latest" in arg for call in commands for arg in call)


@pytest.mark.parametrize("changed", ["anchor_build.py", "anchor_assets/Dockerfile"])
def test_source_override_cannot_supply_different_build_assets(
    commands: list[list[str]], monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    original = anchor_build._snapshot_installed

    def snapshot(target: Path) -> dict[str, object]:
        result = original(target)
        (target / "flotilla" / changed).write_text("different installed version")
        return result

    monkeypatch.setattr(anchor_build, "_snapshot_installed", snapshot)
    with pytest.raises(ValueError, match="installed Flotilla"):
        build_anchor(AnchorBuildOptions("registry/anchor:v1", BASE))
    assert not commands


def test_failed_self_containment_never_publishes(commands: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    original = anchor_build._run

    def fail(args: list[str], *, cwd: Path) -> str:
        if "run" in args:
            raise subprocess.CalledProcessError(1, args)
        return original(args, cwd=cwd)

    monkeypatch.setattr(anchor_build, "_run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        build_anchor(AnchorBuildOptions("registry/anchor:v1", BASE, push=True))
    assert not any(call[0] == "skopeo" for call in commands)


def test_invalid_remote_digest_is_not_reported(commands: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    original = anchor_build._run

    def run(args: list[str], *, cwd: Path) -> str:
        if args[0] == "skopeo" and "inspect" in args:
            return "not a digest"
        return original(args, cwd=cwd)

    monkeypatch.setattr(anchor_build, "_run", run)
    with pytest.raises(ValueError, match="registry did not return"):
        build_anchor(AnchorBuildOptions("registry/anchor:v1", BASE, push=True))


@pytest.mark.parametrize(
    "changes",
    [
        {"python_image": "python:3.12"},
        {"image": "repo:latest"},
        {"image": "repo"},
        {"image": "repo@" + REMOTE},
        {"busybox_url": "http://example/apk"},
        {"docker": "sudo docker"},
    ],
)
def test_invalid_options_fail_before_commands(changes: dict[str, Any], commands: list[list[str]]) -> None:
    options = {"image": "registry/anchor:v1", "python_image": BASE, **changes}
    with pytest.raises(ValueError):
        build_anchor(AnchorBuildOptions(**options))
    assert not commands


def test_installed_snapshot_preserves_exact_files_metadata_and_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = _distribution(tmp_path / "installed")
    monkeypatch.setattr(anchor_build, "_installed_distributions", lambda: [dist])
    target = tmp_path / "snapshot"
    manifest = anchor_build._snapshot_installed(target)
    assert manifest["distributions"] == {"flotilla": "1.2.3"}
    assert (target / "flotilla/__init__.py").read_bytes() == b"# installed bytes\n"
    assert (target / "flotilla-1.2.3.dist-info/direct_url.json").read_text() == dist.read_text("direct_url.json")
    assert metadata.Distribution.at(target / "flotilla-1.2.3.dist-info").version == "1.2.3"


def test_modified_installed_file_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = _distribution(tmp_path / "installed")
    Path(str(dist.locate_file("flotilla/__init__.py"))).write_text("unexpected modification")
    monkeypatch.setattr(anchor_build, "_installed_distributions", lambda: [dist])
    with pytest.raises(ValueError, match="differs from RECORD"):
        anchor_build._snapshot_installed(tmp_path / "snapshot")


@pytest.mark.parametrize("rel", ["../../secret", "/etc/passwd", "flotilla//bad", "flotilla/../bad"])
def test_snapshot_rejects_unsafe_record_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rel: str) -> None:
    dist = _distribution(tmp_path / "installed")
    record = tmp_path / "installed/flotilla-1.2.3.dist-info/RECORD"
    with record.open("a") as stream:
        csv.writer(stream).writerow((rel, "sha256=invalid", "1"))
    monkeypatch.setattr(anchor_build, "_installed_distributions", lambda: [dist])
    with pytest.raises(ValueError, match=r"outside site-packages|noncanonical"):
        anchor_build._snapshot_installed(tmp_path / "snapshot")


def test_editable_distribution_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = _distribution(tmp_path / "installed")
    Path(str(dist.locate_file("flotilla-1.2.3.dist-info/direct_url.json"))).write_text(
        json.dumps({"url": "file:///checkout", "dir_info": {"editable": True}})
    )
    monkeypatch.setattr(metadata, "distribution", lambda name: dist)
    with pytest.raises(ValueError, match="editable"):
        anchor_build._installed_distributions()


def test_dependency_snapshot_uses_installed_base_closure_not_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flotilla = _distribution(tmp_path / "installed", requires=("httpx>=1", 'harbor; extra == "harbor"'))
    httpx = _distribution(tmp_path / "installed", "httpx")
    distributions = {"flotilla": flotilla, "httpx": httpx}
    monkeypatch.setattr(metadata, "distribution", lambda name: distributions[name])
    found = anchor_build._installed_distributions()
    assert [dist.metadata["Name"] for dist in found] == ["flotilla", "httpx"]


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


def test_packaged_assets_have_runtime_verification_not_legacy_compiler() -> None:
    assets = resources.files("flotilla.anchor_assets")
    dockerfile = assets.joinpath("Dockerfile").read_text()
    verification = assets.joinpath("verify.py").read_text()
    assert "pip install" not in dockerfile and "apt-get" not in dockerfile
    assert "--without-pip" in dockerfile
    # Python 3.12 doesn't discover pyvenv.cfg through an outside symlink.
    # Both execd paths must run a wrapper that execs the actual venv path.
    assert 'exec /opt/flotilla/bin/python "$@"' in dockerfile
    assert "python-entrypoint /usr/bin/python3" in dockerfile
    assert "python-entrypoint /usr/local/bin/python3" in dockerfile
    assert "ln -sf /opt/flotilla/bin/python" not in dockerfile
    assert "flotilla.native.platform_verify" in verification
    assert "--self-test" in verification
    assert "integrations.workspace" not in verification
