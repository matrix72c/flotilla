"""Backend selection: the in-tree table, `[backends.<name>]` validation, and construction through the config."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from flotilla.backends import BackendSettings
from flotilla.config import BACKENDS, ConfigError, FlotillaConfig, OpenSandboxBackend
from flotilla.platform.base import Capabilities, Clock, Platform
from flotilla.platform.clock import SystemClock
from flotilla.testing.config import config_data
from flotilla.testing.platform import FakePlatform


class FakeSettings(BackendSettings):
    region: str
    tier: str = "standard"


class FakeBackend:
    """Satisfies `flotilla.backends.Backend`; registered into the in-tree table like a real backend."""

    settings_model = FakeSettings

    def __init__(self) -> None:
        self.built: list[dict[str, Any]] = []

    def check(self, settings: FakeSettings, cfg: FlotillaConfig) -> list[str]:
        return ["fake backend has no outbound access"] if cfg.network.external == "any" else []

    def capabilities(self, settings: FakeSettings, cfg: FlotillaConfig, caps: Capabilities) -> Capabilities:
        return caps

    def build(
        self,
        settings: FakeSettings,
        cfg: FlotillaConfig,
        *,
        clock: Clock,
        credential: str,
        caps: Capabilities,
        command_gate: Callable[[], Awaitable[None]] | None = None,
    ) -> Platform:
        self.built.append({"region": settings.region, "credential": credential, "gate": command_gate})
        return FakePlatform(caps)


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeBackend]:
    backend = FakeBackend()
    monkeypatch.setitem(BACKENDS, "fake", backend)
    yield backend


def fake_data(**table: Any) -> dict[str, Any]:
    data = config_data()
    del data["opensandbox"]
    data["backend"] = "fake"
    data["backends"] = {"fake": {"endpoint": "http://os.test", "credential_env": "FAKE_TOKEN", **table}}
    return data


def test_the_management_client_is_bounded_regardless_of_what_the_platform_supports() -> None:
    """A management anchor gets no capability a task would get, so it cannot become a task runner."""
    cfg = FlotillaConfig.from_mapping(config_data())
    management = cfg.management_capabilities()
    assert not management.privileged_runtime and not management.shared_volume and not management.link
    assert cfg.platform_capabilities().privileged_runtime


def test_builtin_backend_is_the_default() -> None:
    cfg = FlotillaConfig.from_mapping(config_data())
    assert cfg.backend == "opensandbox"
    assert isinstance(BACKENDS["opensandbox"], OpenSandboxBackend)
    assert cfg.backend_settings is cfg.opensandbox


def test_third_party_backend_table_is_validated_by_its_model(registered: FakeBackend) -> None:
    cfg = FlotillaConfig.from_mapping(fake_data(region="cn-sh"))
    settings = cfg.backend_settings
    assert isinstance(settings, FakeSettings)
    assert (settings.region, settings.tier, settings.endpoint) == ("cn-sh", "standard", "http://os.test")
    with pytest.raises(ConfigError, match="no \\[opensandbox\\] settings"):
        cfg.opensandbox_settings()


def test_backend_table_errors_name_the_field_without_values(registered: FakeBackend) -> None:
    with pytest.raises(ValidationError) as missing:
        FlotillaConfig.from_mapping(fake_data())
    assert "backends.fake.region" in str(missing.value)
    with pytest.raises(ValidationError) as unknown:
        FlotillaConfig.from_mapping(fake_data(region="cn-sh", credential="ak:SECRET"))
    assert "backends.fake.credential" in str(unknown.value)
    assert "SECRET" not in str(unknown.value)


def test_backend_check_problems_reject_the_config(registered: FakeBackend) -> None:
    data = fake_data(region="cn-sh")
    data["network"] = {"external": "any"}
    with pytest.raises(ValidationError, match="fake backend has no outbound access"):
        FlotillaConfig.from_mapping(data)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"backend": "missing"}, "unknown backend 'missing'; in-tree: fake, opensandbox"),
        ({"backend": "Bad Name"}, "backend must match"),
        ({"backends": {}}, "requires a \\[backends.fake\\] table"),
        ({"opensandbox": config_data()["opensandbox"]}, "\\[opensandbox\\] is set but backend = 'fake'"),
    ],
)
def test_backend_selection_mistakes_are_rejected(registered: FakeBackend, change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        FlotillaConfig.from_mapping({**fake_data(region="cn-sh"), **change})


def test_unselected_backend_tables_are_rejected(registered: FakeBackend) -> None:
    data = fake_data(region="cn-sh")
    data["backends"]["other"] = {}
    with pytest.raises(ValidationError, match="unselected backends: other"):
        FlotillaConfig.from_mapping(data)
    builtin = config_data()
    builtin["backends"] = {"fake": {}}
    with pytest.raises(ValidationError, match="use \\[opensandbox\\]"):
        FlotillaConfig.from_mapping(builtin)


def test_runtime_builds_the_selected_backend(
    registered: FakeBackend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flotilla.runtime.manager import Manager

    cfg = FlotillaConfig.from_mapping(fake_data(region="cn-sh"))
    monkeypatch.setenv("FAKE_TOKEN", "test-only")
    manager = Manager(cfg, clock=SystemClock())
    assert isinstance(manager.platform, FakePlatform)
    [built] = registered.built
    assert (built["region"], built["credential"]) == ("cn-sh", "test-only")
    assert built["gate"] is not None  # the runtime's exec rate limit reaches the backend


def test_child_mapping_keeps_the_backend_and_its_settings(registered: FakeBackend) -> None:
    """A spec a process received as data validates to the same backend table it was dumped from."""
    cfg = FlotillaConfig.from_mapping(fake_data(region="cn-sh"))
    child = FlotillaConfig.from_mapping(cfg.model_dump(mode="json"))
    assert child.backend == "fake"
    assert child.backend_settings == cfg.backend_settings
