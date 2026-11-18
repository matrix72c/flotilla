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


@pytest.mark.parametrize("read_fails", [False, True])
def test_account_cleanup_failure_is_visible_without_hiding_the_read_error(
    monkeypatch: pytest.MonkeyPatch, read_fails: bool
) -> None:
    def command(args: Sequence[str], **kwargs: Any) -> str:
        if args[0] == "create":
            return "owned-container"
        if args[0] == "cp":
            if read_fails:
                raise ImageError("image read failed")
            Path(args[2]).write_text("root:x:0:0:::\n")
            return ""
        raise ImageError("cleanup failed")

    monkeypatch.setattr(docker, "_docker", command)
    with pytest.raises(ImageError, match="image read failed" if read_fails else "cleanup failed") as error:
        docker.DockerImageAccounts().accounts("test-image")
    if read_fails:
        assert "flotilla-accounts-" in " ".join(error.value.__notes__)
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
        docker.DockerImageAccounts(ownership_dir=journal).accounts("fixture")
    assert created == removed and len(created) == 1
    assert list(journal.iterdir()) == ([journal / created[0]] if cleanup_fails else [])


def test_account_reads_use_the_configured_timeout_and_report_docker_stderr(
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
    accounts = docker_context(FlotillaConfig.from_mapping(data), tmp_path / "journal").image_accounts
    assert accounts is not None

    seen: list[float] = []

    def run(cmd: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs["timeout"])
        return subprocess.CompletedProcess(cmd, 1, "", "Error response from daemon: disk quota exceeded")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ImageError, match="disk quota exceeded"):
        accounts("test-image")
    assert seen == [1234, 1234]  # create, then the cleanup removal
