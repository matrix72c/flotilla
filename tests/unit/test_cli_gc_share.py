"""Command-line dispatch of `flotilla gc` and `flotilla share`.

Only argument parsing and dispatch are tested here: the real platform behavior is tested in `test_gc.py` (library) and
`test_share_release.py` (library). `probe` / `publish` have no precedent for CLI-level tests since they need a real
platform; these can be tested because both paths return before reading the config.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from flotilla.cli import main
from flotilla.testing.platform import FakePlatform, ManualClock, SharedTree
from flotilla.testing.reports import report

SHA = "0" * 64


def _deployment(tmp_path: Path, **extra: str) -> Path:
    """A minimal working deployment config, with the capability report written next to it."""
    (tmp_path / "capabilities.json").write_text(report().dump_json())
    lines = [
        "[opensandbox]",
        'endpoint = "http://os.test"',
        'credential_env = "FLOTILLA_CRED"',
        'capabilities = "capabilities.json"',
        "",
        "[opensandbox.network]",
        'sandbox_cidrs = ["10.0.0.0/16"]',
        'platform_cidrs = ["169.254.169.254/32"]',
        "",
        "[storage]",
        'volumes = "host"',
        'host_path = "/srv/flotilla"',
        "",
        "[share]",
        'release = "2026-10-07.1"',
        f'release_sha256 = "{SHA}"',
        "",
        "[reaper]",
        'anchor_image = "reg/flotilla@sha256:' + "a" * 64 + '"',
    ]
    path = tmp_path / "flotilla.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


# ───────────────────────────── Argument parsing ─────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["gc"], id="gc-missing-deployment"),
        pytest.param(["share"], id="share-missing-subcommand"),
        pytest.param(["share", "publish"], id="share-publish-missing-dir"),
        pytest.param(["share", "publish", "x"], id="share-publish-missing-deployment"),
        pytest.param(["share", "verify"], id="share-verify-missing-deployment"),
        pytest.param(["share", "gc"], id="share-gc-missing-deployment"),
    ],
)
def test_missing_arguments_exit_2(argv: list[str]) -> None:
    """argparse raises `SystemExit(2)` on a missing required argument instead of returning 2; assert the exception."""
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


def test_both_commands_are_registered() -> None:
    """Both commands are dispatched in `main` and no longer fall through to "not implemented"."""
    import inspect

    from flotilla import cli

    source = inspect.getsource(cli.main)
    assert 'args.command == "gc"' in source
    assert 'args.command == "share"' in source
    # The fallback line is still there (for future subcommands added without wiring), but these two must not reach it.
    assert "is not implemented" in source


# ───────────────────────────── share verify digest semantics ─────────────────────────────


def test_share_verify_release_without_sha256_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--release requires --sha256 too: the config pins a different release, so verifying with it is meaningless."""
    cfg = _deployment(tmp_path)
    code = main(["share", "verify", "--deployment", str(cfg), "--release", "other"])
    assert code == 2
    assert "--release requires --sha256" in capsys.readouterr().err


def test_share_verify_rejects_non_hex_sha256(tmp_path: Path) -> None:
    """The --sha256 shape is enforced by config validation; confirm it is rejected (ConfigError path -> 2)."""
    cfg = _deployment(tmp_path)
    assert main(["share", "verify", "--deployment", str(cfg), "--release", "other", "--sha256", "nope"]) == 2


# ───────────────────────────── gc dispatch ─────────────────────────────


def test_gc_launch_of_missing_config_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A missing config file is reported as a config error (exit code 2), not a traceback."""
    assert main(["gc", "--deployment", str(tmp_path / "nope.toml"), "--launch", "L9"]) == 2
    assert "cannot read deployment configuration" in capsys.readouterr().err


def test_share_publish_bad_component_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--component without `=` exits 2 before connecting (an unset credential must not mask this usage error)."""
    cfg = _deployment(tmp_path)
    assert main(["share", "publish", str(tmp_path), "--deployment", str(cfg), "--component", "nope"]) == 2
    captured = capsys.readouterr()
    assert "name=version" in captured.err
    assert "credential" not in captured.err


def test_parser_exposes_gc_and_share_flags() -> None:
    """The parser itself: `--launch`, `--keep`, `--dry-run` and `--component` are all present."""
    from flotilla.cli import _build_parser

    parser = _build_parser()
    gc = parser.parse_args(["gc", "--deployment", "d.toml", "--launch", "L1"])
    assert gc.launch == "L1" and gc.command == "gc"
    share_gc = parser.parse_args(["share", "gc", "--deployment", "d.toml", "--keep", "r1", "--dry-run"])
    assert share_gc.keep == ["r1"] and share_gc.dry_run and share_gc.share_command == "gc"
    pub = parser.parse_args(
        ["share", "publish", "d", "--deployment", "d.toml", "--component", "a=1", "--component", "b=2"]
    )
    assert pub.component == ["a=1", "b=2"] and pub.release is None
    ver = parser.parse_args(["share", "verify", "--deployment", "d.toml"])
    assert ver.release is None and ver.sha256 is None


@pytest.mark.asyncio
async def test_share_gc_preview_matches_real_deletion(
    tmp_path: Path,
    platform: FakePlatform,
    clock: ManualClock,
    tree: SharedTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from flotilla import cli

    cfg = _deployment(tmp_path)
    tree.dirs.update({"share/releases", "share/releases/2026-10-07.1", "share/releases/stale"})
    monkeypatch.setattr(cli, "SystemClock", lambda: clock)
    monkeypatch.setattr(cli, "_platform", lambda *_: platform)
    monkeypatch.setattr(platform, "aclose", AsyncMock(), raising=False)
    argv = ["share", "gc", "--deployment", str(cfg)]
    parser = cli._build_parser()
    assert await clock.run(cli._share(parser.parse_args([*argv, "--dry-run"]))) == 0
    assert "would remove ['stale']" in capsys.readouterr().out
    assert "share/releases/stale" in tree.dirs
    assert await clock.run(cli._share(parser.parse_args(argv))) == 0
    assert "removed ['stale']" in capsys.readouterr().out
    assert "share/releases/stale" not in tree.dirs
    assert "share/releases/2026-10-07.1" in tree.dirs
