"""CPU command contracts and stage-aware deployment validation."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from flotilla.anchor_build import AnchorBuildOptions, AnchorBuildResult
from flotilla.capabilities import CapabilityReport
from flotilla.cli import _build_parser, main
from flotilla.config import ConfigError, FlotillaConfig
from flotilla.environment import load_env_file
from flotilla.testing.config import config
from flotilla.testing.reports import no, report


def test_probe_cleanup_is_required() -> None:
    cfg = config()
    evidence = report(cleanup=no())
    assert "confirmed cleanup" in evidence.missing_required()
    with pytest.raises(ConfigError, match="confirmed cleanup"):
        cfg.check_against(evidence)


def test_deployment_loading_does_not_open_releases(tmp_path: Path) -> None:
    data = config().model_dump(exclude={"base_dir"})
    data["task_releases"] = {"future": {"local_root": "not-created", "platform_root": "releases/future"}}
    data["agent_runtime"] = {"layers": {"lagent": {"local_root": "not-created", "platform_root": "runtimes/l1"}}}
    cfg = FlotillaConfig.from_mapping(data, base_dir=tmp_path)
    assert cfg.task_releases["future"].local_root == Path("not-created")
    assert cfg.agent_runtime is not None and cfg.agent_runtime.layers["lagent"].local_root == Path("not-created")
    assert cfg.to_mapping()["task_releases"]["future"]["local_root"] == str(tmp_path / "not-created")
    bindings = cfg.release_bindings()
    with pytest.raises(OSError):
        bindings.resolve({"release": "future", "task": "t"})


def test_env_is_literal_and_errors_never_echo_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_NATIVE_SECRET", raising=False)
    path = tmp_path / "prepare.env"
    path.write_text("export TEST_NATIVE_SECRET='literal $HOME $(id)'\n")
    load_env_file(path)
    import os

    assert os.environ["TEST_NATIVE_SECRET"] == "literal $HOME $(id)"
    path.write_text("TEST_NATIVE_SECRET='hidden secret\n")
    with pytest.raises(ValueError, match="line 1") as error:
        load_env_file(path)
    assert "hidden secret" not in str(error.value)


def test_legacy_prepare_is_not_silently_warmup() -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["prepare", "task-key", "--deployment", "d.toml"])


def test_anchor_defaults_to_build_only(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import flotilla.anchor_build as builder

    seen: list[AnchorBuildOptions] = []

    def build(options: AnchorBuildOptions) -> AnchorBuildResult:
        seen.append(options)
        return AnchorBuildResult(
            image="reg/anchor:v1", version="0", image_id="sha256:test", remote_digest=None, published_image=None
        )

    monkeypatch.setattr(builder, "build_anchor", build)
    assert main(["anchor", "build", "--image", "reg/anchor:v1", "--python-image", "reg/python@sha256:" + "a" * 64]) == 0
    assert not seen[0].push
    assert "not published" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["remove", "close", "platform"])
async def test_probe_anchor_cleanup_failure_is_persisted_not_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    import importlib

    module = importlib.import_module("flotilla.probe")
    cfg = config(probe={"image": "reg/probe@sha256:" + "a" * 64}).model_copy(update={"base_dir": tmp_path})
    evidence = report(evidence_profile=cfg.evidence_profile())

    class FakeAnchor:
        launch_id = "probe-test"

        def __init__(self, *args: object) -> None:
            pass

        async def start(self) -> None:
            pass

        async def mkdir(self, *args: object, **kwargs: object) -> None:
            pass

        async def remove(self, *args: object, **kwargs: object) -> None:
            if broken == "remove":
                raise RuntimeError("private details must not enter evidence")

        async def close(self) -> None:
            if broken == "close":
                raise RuntimeError("private details must not enter evidence")

    async def close_platform() -> None:
        if broken == "platform":
            raise RuntimeError("private details must not enter evidence")

    async def measured(*args: object) -> CapabilityReport:
        return evidence

    monkeypatch.setattr(module, "Anchor", FakeAnchor)
    monkeypatch.setattr(module, "probe", measured)
    monkeypatch.setattr(FlotillaConfig, "build_platform", lambda *a, **k: SimpleNamespace(aclose=close_platform))
    with pytest.raises(ConfigError, match="confirmed cleanup"):
        await module.probe_deployment(cfg)
    text = cfg.capabilities_path().read_text()
    assert "unconfirmed" in text
    assert "private details" not in text


@pytest.mark.asyncio
async def test_probe_output_mismatch_fails_before_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from flotilla.probe import probe_deployment

    cfg = config().model_copy(update={"base_dir": tmp_path})
    monkeypatch.setattr(
        FlotillaConfig, "build_platform", lambda *a, **k: pytest.fail("created platform before validation")
    )
    with pytest.raises(ConfigError, match="must match"):
        await probe_deployment(cfg, out=tmp_path / "wrong.json")
