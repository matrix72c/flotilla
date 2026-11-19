"""CPU command contracts and stage-aware deployment validation."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from flotilla.anchor_build import AnchorBuildOptions, AnchorBuildResult
from flotilla.capabilities import ISOLATION_PROPERTIES, UNMEASURED, IsolationEvidence
from flotilla.cli import _build_parser, main
from flotilla.config import ConfigError, FlotillaConfig
from flotilla.environment import load_env_file
from flotilla.native.dataset import DatasetError
from flotilla.testing.config import config
from flotilla.testing.reports import evidence, no, ok


def test_probe_cleanup_is_required() -> None:
    """A deployment whose resources cannot be confirmed deleted is refused, waivers or not."""
    with pytest.raises(ConfigError, match="cleanup"):
        config().check_isolation(evidence(cleanup=no()))


def test_a_spec_opens_no_dataset(tmp_path: Path) -> None:
    """Building a spec touches no dataset: one naming a missing directory still validates."""
    absent = tmp_path / "not-created"
    data = config().model_dump(mode="json")
    data["datasets"] = {"future": {"local_root": str(absent), "platform_root": "datasets/future"}}
    data["agent_runtime"] = {"layers": {"lagent": {"local_root": str(absent), "platform_root": "runtimes/l1"}}}
    cfg = FlotillaConfig.from_mapping(data)
    assert cfg.datasets["future"].local_root == absent
    assert cfg.agent_runtime is not None and cfg.agent_runtime.layers["lagent"].local_root == absent
    # Resolving it is what fails, and only then.
    with pytest.raises(DatasetError, match="not a directory"):
        cfg.resolve_datasets()


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
    measured_evidence = evidence()

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

    async def measured(*args: object) -> IsolationEvidence:
        return measured_evidence

    monkeypatch.setattr(module, "Anchor", FakeAnchor)
    monkeypatch.setattr(module, "probe", measured)
    monkeypatch.setattr(FlotillaConfig, "build_platform", lambda *a, **k: SimpleNamespace(aclose=close_platform))
    with pytest.raises(ConfigError) as failure:
        await module.probe_deployment(cfg)
    assert "cleanup" in str(failure.value)
    assert "private details" not in str(failure.value)


@pytest.mark.parametrize("waived", [False, True])
def test_probe_reports_each_property_and_never_claims_a_waived_one_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], waived: bool
) -> None:
    """Every measured property reaches the operator, and a waiver reads as accepted, not as held."""
    from flotilla import cli

    security = {"accept_insecure": ["exec_auth"]} if waived else {}
    cfg = config(probe={"image": "reg/probe@sha256:" + "a" * 64}, security=security)
    path = tmp_path / "deployment.toml"
    path.write_text("")
    measured = evidence(
        exec_auth=no() if waived else ok(),
        # The privileged phase only ran far enough to measure the network.
        privileged_storage=UNMEASURED,
        privileged_no_auto_mount=UNMEASURED,
        privileged_exec_auth=UNMEASURED,
        privileged_inbound_isolation=UNMEASURED,
        external_forms=frozenset({"none", "allowlist"}),
        implicit_egress=("10.96.0.10:53/udp",),
    )

    async def probed(*args: object, **kwargs: object) -> IsolationEvidence:
        return measured

    monkeypatch.setattr(FlotillaConfig, "load", classmethod(lambda cls, p: cfg))
    monkeypatch.setattr(cli, "probe_deployment", probed)
    assert main(["probe", "--deployment", str(path)]) == 0
    out = capsys.readouterr().out
    for name in (*ISOLATION_PROPERTIES, "cleanup", "privileged_network"):
        assert name in out
    assert "allowlist, none" in out and "10.96.0.10:53/udp" in out
    # A privileged check that never ran is absent rather than printed as a failure.
    assert "privileged_exec_auth" not in out
    if waived:
        assert "NOT SHOWN  exec_auth" in out and "accepts running without: exec_auth" in out
    else:
        assert "NOT SHOWN" not in out and "accepts running without" not in out


@pytest.mark.asyncio
async def test_probe_requires_a_pinned_image_before_reaching_the_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flotilla.probe import probe_deployment

    cfg = config().model_copy(update={"base_dir": tmp_path})
    monkeypatch.setattr(
        FlotillaConfig, "build_platform", lambda *a, **k: pytest.fail("created platform before validation")
    )
    with pytest.raises(ConfigError, match=r"probe\.image"):
        await probe_deployment(cfg)
