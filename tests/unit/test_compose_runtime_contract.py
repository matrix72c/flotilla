"""Offline harness checks; only the optional config test invokes Compose, never Docker."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from flotilla.compose.adapter import ComposeConfigAdapter
from flotilla.config import FlotillaConfig
from tests.contract import compose_runtime as contract

IMAGE = "python@sha256:" + "a" * 64


def test_fixture_is_self_contained_and_covers_shared_and_nested_binds() -> None:
    files = contract.fixture_files(IMAGE)
    document = yaml.safe_load(files["environment/docker-compose.yaml"])
    assert document == contract.compose_document(IMAGE)
    main = document["services"]["main"]
    assert main["image"] == IMAGE
    assert main["entrypoint"] == ["python3", "-I", "/probe.py"]
    assert main["command"][1] == ""
    assert [(v["source"], v["target"], v["read_only"]) for v in main["volumes"]] == [
        ("./overlay", "/data/nested", True),
        ("./data", "/data", False),
        ("./data", "/readonly", True),
        ("./data/sub", "/descendant", False),
        ("./probe.py", "/probe.py", True),
    ]
    for mount in main["volumes"]:
        assert mount["bind"]["create_host_path"] is False
        prefix = "environment/" + mount["source"].removeprefix("./")
        assert any(name == prefix or name.startswith(prefix + "/") for name in files)
    assert files["environment/data/nested/value"] != files["environment/overlay/value"]
    compile(contract.PROBE, "probe.py", "exec")
    compile(contract.READ_REPORT, "read_report.py", "exec")


def test_environment_does_not_inherit_interpolation_or_platform_credentials() -> None:
    env = contract.docker_environment(
        {
            "PATH": "/untrusted",
            "HOME": "/home/test",
            "DOCKER_HOST": "unix:///test.sock",
            "CONTRACT_UNSET": "would-change-fallback",
            "CONTRACT_EXPLICIT": "ambient-wrong",
            "PLATFORM_API_KEY": "do-not-inherit",
            "COMPOSE_FILE": "/unrelated.yaml",
        }
    )
    assert env == {
        "PATH": os.defpath,
        "HOME": "/home/test",
        "DOCKER_HOST": "unix:///test.sock",
        "CONTRACT_EXPLICIT": "explicit value",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_removal", [False, True])
async def test_cleanup_is_label_scoped_and_continues_after_failure(
    monkeypatch: pytest.MonkeyPatch, fail_removal: bool
) -> None:
    calls: list[list[str]] = []
    remaining = {kind: [kind + "-owned"] for kind in ("container", "network", "volume")}

    async def fake_command(argv: Sequence[str], env: Mapping[str, str], *, timeout: float = 180) -> bytes:
        args = list(argv)
        calls.append(args)
        kind = args[1]
        if args[2] == "ls":
            assert args[3:] == [
                "-q",
                "--filter",
                "label=com.docker.compose.project=compose-runtime-unique",
                *(["--all"] if kind == "container" else []),
            ]
            return "\n".join(remaining[kind]).encode()
        assert args == [
            "docker-test",
            kind,
            "rm",
            *(["--force"] if kind != "network" else []),
            kind + "-owned",
        ]
        if fail_removal and kind == "container":
            raise RuntimeError("deletion failed")
        remaining[kind].clear()
        return b""

    monkeypatch.setattr(contract, "command", fake_command)
    assert await contract.cleanup_docker("docker-test", "compose-runtime-unique", {}) is not fail_removal
    assert any(args[1:3] == ["volume", "rm"] for args in calls)
    assert remaining["container"] == (["container-owned"] if fail_removal else [])
    assert not remaining["network"] and not remaining["volume"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "equal-but-wrong", "partial-up", "cleanup-failed"])
async def test_run_reports_comparison_and_cleans_partial_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    pytest.importorskip("harbor")
    from flotilla.integrations.harbor_attempt import HarborAttempt
    from flotilla.runtime import client

    cfg = Mock()
    cfg.prepare.compose_version = "2.39.4"
    platform = AsyncMock()
    platform.list.return_value = []
    cfg.build_platform.return_value = platform
    monkeypatch.setattr(FlotillaConfig, "load", Mock(return_value=cfg))
    anchor = AsyncMock()
    anchor.iid = None
    anchor.exists.return_value = False
    monkeypatch.setattr(contract, "Anchor", Mock(return_value=anchor))
    fleet = Mock()
    fleet.close = AsyncMock(return_value=Mock(launch_id="owned-fleet", clean=True))
    monkeypatch.setattr(client, "Flotilla", Mock(return_value=fleet))
    attempt = AsyncMock()
    attempt.workspace.key = "launches/owned-fleet/workspaces/owned-attempt"
    observed = contract.expected_observation()
    if mode == "equal-but-wrong":
        observed["argv"] = ["incorrect-but-identical"]
    main = AsyncMock()
    main.agent.exec.return_value = Mock(exit_code=0, timed_out=False, stdout=json.dumps(observed).encode())
    attempt.open.return_value = main
    create = AsyncMock(return_value=attempt)
    monkeypatch.setattr(HarborAttempt, "create", create)
    docker_cleanup = AsyncMock(return_value=mode != "cleanup-failed")
    monkeypatch.setattr(contract, "cleanup_docker", docker_cleanup)

    async def fake_command(argv: Sequence[str], env: Mapping[str, str], *, timeout: float = 180) -> bytes:
        if "version" in argv:
            return b"v2.39.4\n"
        if "up" in argv and mode == "partial-up":
            raise RuntimeError("sensitive diagnostic must not appear in report")
        return json.dumps(observed).encode()

    monkeypatch.setattr(contract, "command", fake_command)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    output = tmp_path / "report.json"
    passed = await contract.run(
        tmp_path / "deployment.toml",
        output,
        compose=Path("/official-compose"),
        docker="docker",
        image=IMAGE,
        local_root=scratch,
    )
    assert passed is (mode == "success")
    report = json.loads(output.read_text())
    assert report["passed"] is passed
    assert "sensitive diagnostic" not in output.read_text()
    docker_cleanup.assert_awaited_once()
    assert docker_cleanup.await_args is not None
    assert docker_cleanup.await_args.args[1] == report["docker_project"]
    fleet.close.assert_awaited_once()
    anchor.close.assert_awaited_once()
    platform.aclose.assert_awaited_once()
    assert bool(list(scratch.iterdir())) is (mode == "cleanup-failed")
    if mode == "partial-up":
        create.assert_not_awaited()
        anchor.start.assert_not_awaited()
        assert report["error_type"] == "RuntimeError"
        assert report["stage"] == "docker-start"
    else:
        attempt.close.assert_awaited_once()
        assert report["checks"]["runtimes_equal"] is True
        assert report["checks"]["docker_expected"] is (mode != "equal-but-wrong")
        assert report["checks"]["flotilla_expected"] is (mode != "equal-but-wrong")
        # Original fixture bytes, not Docker's mutated local source, are uploaded.
        uploads = {call.args[0].split("/environment/")[-1]: call.args[1] for call in anchor.upload.await_args_list}
        assert uploads["data/seed"] == b"seed-content"
        assert "data/marker" not in uploads


@pytest.mark.skipif(not os.environ.get("FLOTILLA_TEST_COMPOSE"), reason="explicit official Compose executable required")
def test_fixture_normalizes_to_expected_argv_and_environment(tmp_path: Path) -> None:
    for name, data in contract.fixture_files(IMAGE).items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    root = tmp_path / "environment"
    project = ComposeConfigAdapter(command=(os.environ["FLOTILLA_TEST_COMPOSE"],)).load(
        root,
        files=[root / "docker-compose.yaml"],
        environment=contract.INTERPOLATION,
        main_image=IMAGE,
        main_build=False,
    )
    assert not project.rejected, project.findings
    main = project.services["main"]
    assert main.environment == contract.expected_observation()["environment"]
    assert main.command == tuple(contract.expected_observation()["argv"])
    assert main.entrypoint == ("python3", "-I", "/probe.py")
    assert not project.runtime_params
