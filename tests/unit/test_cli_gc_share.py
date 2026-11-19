"""Command-line dispatch of `flotilla gc`.

Only argument parsing and dispatch are tested here; the platform behavior is in `test_gc.py`.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from flotilla.cli import main
from flotilla.config import FlotillaConfig
from flotilla.testing.config import importable
from flotilla.testing.platform import FakePlatform, ManualClock, SharedTree

SHA = "0" * 64


def _deployment(**extra: Any) -> str:
    """A minimal working spec, named the way `flotilla gc` takes one."""
    return importable(
        FlotillaConfig.from_mapping(
            {
                "opensandbox": {
                    "endpoint": "http://os.test",
                    "credential_env": "FLOTILLA_CRED",
                    "network": {"sandbox_cidrs": ["10.0.0.0/16"], "platform_cidrs": ["169.254.169.254/32"]},
                },
                "storage": {"volumes": "host", "host_path": "/srv/flotilla"},
                "share": {"release": "2026-10-07.1", "release_sha256": SHA},
                "reaper": {"anchor_image": "reg/flotilla@sha256:" + "a" * 64},
                **extra,
            }
        )
    )


# ───────────────────────────── Argument parsing ─────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["gc"], id="gc-missing-deployment"),
        pytest.param(["inspect"], id="inspect-missing-deployment"),
        pytest.param(["probe"], id="probe-missing-deployment"),
    ],
)
def test_missing_arguments_exit_2(argv: list[str]) -> None:
    """argparse raises `SystemExit(2)` on a missing required argument instead of returning 2; assert the exception."""
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


def test_only_three_commands_exist() -> None:
    """Everything a deployment needs from outside its own process, and nothing a caller can do in it."""
    from flotilla.cli import _build_parser

    [subparsers] = [a for a in _build_parser()._actions if isinstance(a, argparse._SubParsersAction)]
    assert sorted(subparsers.choices) == ["gc", "inspect", "probe"]


# ───────────────────────────── gc dispatch ─────────────────────────────


def test_gc_of_an_unimportable_deployment_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    """A name that imports nothing is a config error (exit code 2), not a traceback."""
    assert main(["gc", "--deployment", "flotilla.no_such_deployment:SPEC", "--launch", "L9"]) == 2
    assert "cannot import" in capsys.readouterr().err


def test_parser_exposes_gc_flags() -> None:
    from flotilla.cli import _build_parser

    parser = _build_parser()
    gc = parser.parse_args(["gc", "--deployment", "pkg:SPEC", "--launch", "L1"])
    assert gc.launch == "L1" and gc.command == "gc"
    assert parser.parse_args(["gc", "--deployment", "pkg:SPEC"]).staging_age == 24 * 3600


@pytest.mark.asyncio
async def test_gc_without_anchor_deletes_sandboxes_and_leaves_directories(
    tmp_path: Path,
    platform: FakePlatform,
    clock: ManualClock,
    tree: SharedTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from flotilla import cli
    from flotilla.core import labels
    from flotilla.platform.base import ErrorCategory, ExternalPolicy, FlotillaError, InstanceSpec, Resources

    tree.dirs.update({"launches", "launches/L0"})
    spec = InstanceSpec(
        image="svc@sha256:abc",
        entrypoint=("sleep",),
        labels={labels.LAUNCH: "L0", labels.TRIAL: "t1", labels.UNIT: "web", labels.ROLE: labels.ROLE_UNIT},
        timeout_seconds=3600,
        resources=Resources(cpu="1", memory="2Gi"),
        volumes=(),
        external=ExternalPolicy(mode="none"),
    )
    orphan = (await platform.create(spec)).iid
    # The anchor image cannot be pulled: its create is rejected.
    platform.inject_fault(
        "create", FlotillaError("image pull failed", stage="create", category=ErrorCategory.IMAGE, retryable=False)
    )
    monkeypatch.setattr(cli, "SystemClock", lambda: clock)
    monkeypatch.setattr(FlotillaConfig, "build_platform", lambda *_, **__: platform)
    monkeypatch.setattr(platform, "aclose", AsyncMock(), raising=False)
    args = cli._build_parser().parse_args(["gc", "--deployment", _deployment(), "--launch", "L0"])
    assert await clock.run(cli._gc(args)) == 1  # directories could not be collected
    captured = capsys.readouterr()
    assert "anchor unavailable" in captured.err and "directory left" in captured.out
    assert orphan not in platform.iids()
    assert "launches/L0" in tree.dirs
