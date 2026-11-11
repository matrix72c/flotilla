import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from flotilla.build import docker
from flotilla.build.docker import _docker
from flotilla.build.images import ImageError


def test_build_failure_preserves_cause_and_redacts_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd, 1, "", "building with private-token\n" + "progress\n" * 1000 + "CAUSE: registry unavailable"
        )

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ImageError) as error:
        _docker(["buildx", "build", "--build-arg", "TOKEN=private-token"], docker_cmd=["docker"], timeout_s=30)
    message = str(error.value)
    assert "CAUSE: registry unavailable" in message
    assert "private-token" not in message
    assert len(message) < 2000


def test_build_timeout_does_not_leak_the_original_command_through_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import traceback

    def run(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(subprocess, "run", run)
    args = ["buildx", "build", "--build-arg", "TOKEN=private-token"]
    with pytest.raises(ImageError) as error:
        _docker(args, docker_cmd=["docker"], timeout_s=30)
    assert "private-token" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("copy_fails", [False, True])
def test_export_cleanup_failure_is_visible_without_hiding_copy_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, copy_fails: bool
) -> None:
    def command(args: Sequence[str], **kwargs: Any) -> str:
        if args[0] == "create":
            return "owned-container"
        raise ImageError("cleanup failed")

    def copy(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        import io
        import tarfile

        if not copy_fails:
            with tarfile.open(fileobj=kwargs["stdout"], mode="w") as archive:
                info = tarfile.TarInfo("data")
                info.size = 4
                archive.addfile(info, io.BytesIO(b"seed"))
        return subprocess.CompletedProcess(cmd, int(copy_fails), "", "")

    monkeypatch.setattr(subprocess, "run", copy)
    monkeypatch.setattr(docker, "_docker", command)
    with pytest.raises(ImageError, match="image export failed" if copy_fails else "cleanup failed") as error:
        docker.DockerImageExport().extract("test-image", "/data", tmp_path)
    if copy_fails:
        assert "flotilla-export-" in " ".join(error.value.__notes__)
        assert "removal failed" in " ".join(error.value.__notes__)


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_lost_create_response_uses_registered_name_for_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cleanup_fails: bool
) -> None:
    journal = tmp_path / "containers"
    created = []
    removed = []

    def command(args: Sequence[str], **kwargs: Any) -> str:
        if args[0] == "create":
            name = args[2]
            assert (journal / name).is_file()  # Ownership precedes the remote side effect.
            created.append(name)
            raise ImageError("create response lost")
        assert args[:3] == ["rm", "-f", "-v"]
        removed.append(args[3])
        if cleanup_fails:
            raise ImageError("Docker unavailable")
        return ""

    monkeypatch.setattr(docker, "_docker", command)
    with pytest.raises(ImageError, match="create response lost"):
        docker.DockerImageExport(ownership_dir=journal).accounts("fixture")
    assert created == removed and len(created) == 1
    assert list(journal.iterdir()) == ([journal / created[0]] if cleanup_fails else [])


def test_export_uses_the_configured_timeout_and_reports_docker_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from flotilla.config import FlotillaConfig
    from flotilla.prepare.docker import docker_context
    from flotilla.testing.config import config_data
    from flotilla.testing.reports import report

    caps = tmp_path / "caps.json"
    caps.write_text(report().dump_json())
    data = config_data(prepare={"export_timeout": 1234})
    data["opensandbox"]["capabilities"] = str(caps)
    cfg = FlotillaConfig.from_mapping(data)
    exporter = docker_context(cfg, tmp_path / "journal").image_export
    assert isinstance(exporter, docker.DockerImageExport) and exporter.timeout_s == 1234

    seen: list[float] = []

    def copy(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs["timeout"])
        kwargs["stderr"].write(b"Error response from daemon: \n  disk quota exceeded\n")
        return subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(subprocess, "run", copy)
    monkeypatch.setattr(docker, "_docker", lambda args, **kwargs: "owned-container")
    with pytest.raises(ImageError, match="status 1: Error response from daemon: disk quota exceeded"):
        exporter.extract("test-image", "/data", tmp_path / "out")
    assert seen == [1234]


def test_builder_publication_pins_schema2_manifests_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from flotilla.build.builder import BuildRequest
    from flotilla.build.docker import DockerBuilder

    (tmp_path / "Dockerfile").write_text("FROM r.io/base:1\n")
    argv: list[str] = []

    def fake_docker(args: Sequence[str], **kwargs: Any) -> str:
        argv.extend(args)
        metadata = Path(args[args.index("--metadata-file") + 1])
        metadata.write_text('{"containerimage.digest": "sha256:' + "ab" * 32 + '"}')
        return "pushed"

    monkeypatch.setattr(docker, "_docker", fake_docker)
    request = BuildRequest(service="main", tag="r.io/ns/out:user0-1", context=tmp_path, dockerfile="Dockerfile")
    assert DockerBuilder().build(request) == "sha256:" + "ab" * 32
    # An explicit image exporter replaces `--push` shorthand: schema2 manifests, no
    # attestation index, matching registries that reject OCI manifests by quota checks.
    assert "--push" not in argv
    assert argv[argv.index("--output") + 1] == "type=image,push=true,oci-mediatypes=false"
    assert argv[argv.index("--provenance") + 1] == "false"
    assert argv[argv.index("--sbom") + 1] == "false"

    argv.clear()
    oci = DockerBuilder(oci_mediatypes=True, attestations=True)
    oci.build(request)
    assert argv[argv.index("--output") + 1] == "type=image,push=true,oci-mediatypes=true"
    assert argv[argv.index("--provenance") + 1] == "true"
    with pytest.raises(ImageError, match="oci_mediatypes"):
        DockerBuilder(attestations=True)


def test_docker_context_threads_publication_settings(tmp_path: Path) -> None:
    from flotilla.config import FlotillaConfig
    from flotilla.prepare.docker import docker_context
    from flotilla.testing.config import config_data
    from flotilla.testing.reports import report

    caps = tmp_path / "caps.json"
    caps.write_text(report().dump_json())
    data = config_data()
    data["opensandbox"]["capabilities"] = str(caps)
    builder = docker_context(FlotillaConfig.from_mapping(data), tmp_path / "journal").builder
    assert isinstance(builder, docker.DockerBuilder)
    assert builder.oci_mediatypes is False and builder.attestations is False  # schema2 default
