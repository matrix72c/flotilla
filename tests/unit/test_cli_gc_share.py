"""`flotilla gc` 与 `flotilla share` 的命令行分派（Architecture §5.6、§3.3、§16.3）。

这里只验参数解析与分派这一层：真正接平台的行为在 `test_gc.py`（库）与 `test_share_release.py`（库）里测。
`probe` / `publish` 没有 CLI 级测试的先例 —— 它们要连真平台；这里能测是因为这两条路径在读配置之前就返回。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from flotilla.cli import main
from flotilla.platform.fake import FakePlatform, ManualClock, SharedTree
from tests.unit.reports import report

SHA = "0" * 64


def _deployment(tmp_path: Path, **extra: str) -> Path:
    """一份最小可用的部署配置，能力报告写在它旁边。"""
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


# ───────────────────────────── 参数解析 ─────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["gc"], id="gc-缺-deployment"),
        pytest.param(["share"], id="share-缺子命令"),
        pytest.param(["share", "publish"], id="share-publish-缺-dir"),
        pytest.param(["share", "publish", "x"], id="share-publish-缺-deployment"),
        pytest.param(["share", "verify"], id="share-verify-缺-deployment"),
        pytest.param(["share", "gc"], id="share-gc-缺-deployment"),
    ],
)
def test_missing_arguments_exit_2(argv: list[str]) -> None:
    """argparse 在缺必需参数时抛 `SystemExit(2)`，不是返回 2 —— 这里断言异常。"""
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


def test_both_commands_are_registered() -> None:
    """两个命令都已在 `main` 里分派，不再落到"尚未实现"。"""
    import inspect

    from flotilla import cli

    source = inspect.getsource(cli.main)
    assert 'args.command == "gc"' in source
    assert 'args.command == "share"' in source
    # 兜底那一行还在（给未来新增却忘了接线的子命令用），但这两个不该走到它。
    assert "尚未实现" in source


# ───────────────────────────── share verify 的摘要语义 ─────────────────────────────


def test_share_verify_release_without_sha256_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """指定 --release 时必须同时给 --sha256：配置钉的是别的发布号，拿它来验没有意义。"""
    cfg = _deployment(tmp_path)
    code = main(["share", "verify", "--deployment", str(cfg), "--release", "other"])
    assert code == 2
    assert "必须同时指定" in capsys.readouterr().err


def test_share_verify_rejects_non_hex_sha256(tmp_path: Path) -> None:
    """--sha256 的形状由配置校验管，这里确认它会被拒（走 ConfigError 路径 → 2）。"""
    cfg = _deployment(tmp_path)
    assert main(["share", "verify", "--deployment", str(cfg), "--release", "other", "--sha256", "nope"]) == 2


# ───────────────────────────── gc 的分派 ─────────────────────────────


def test_gc_launch_of_missing_config_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """配置文件不存在时报配置错误（退出码 2），不是 traceback。"""
    assert main(["gc", "--deployment", str(tmp_path / "nope.toml"), "--launch", "L9"]) == 2
    assert "读不出部署配置" in capsys.readouterr().err


def test_share_publish_bad_component_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--component 缺 `=` 时报 2，且先于连平台（凭证没设置也不该掩盖这个用法错误）。"""
    cfg = _deployment(tmp_path)
    assert main(["share", "publish", str(tmp_path), "--deployment", str(cfg), "--component", "nope"]) == 2
    captured = capsys.readouterr()
    assert "name=version" in captured.err
    assert "凭证" not in captured.err


def test_parser_exposes_gc_and_share_flags() -> None:
    """参数表本身：`--launch`、`--keep`、`--dry-run`、`--component` 都在。"""
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
    assert "将删除 ['stale']" in capsys.readouterr().out
    assert "share/releases/stale" in tree.dirs
    assert await clock.run(cli._share(parser.parse_args(argv))) == 0
    assert "已删除 ['stale']" in capsys.readouterr().out
    assert "share/releases/stale" not in tree.dirs
    assert "share/releases/2026-10-07.1" in tree.dirs
