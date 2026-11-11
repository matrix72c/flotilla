"""Builder connectivity belongs only to explicitly configured runtime management."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from flotilla.compose.normalize import normalize
from flotilla.config import FlotillaConfig
from flotilla.core.anchor import Anchor
from flotilla.manifest import Unit
from flotilla.platform.base import Capabilities, ExternalPolicy, Resources, SharedVolume
from flotilla.platform.opensandbox import OpenSandboxPlatform
from flotilla.testing.config import config, config_data
from flotilla.testing.platform import FakePlatform, ManualClock


def configured() -> FlotillaConfig:
    return config(
        opensandbox={
            **config_data()["opensandbox"],
            "management_extensions": {"runtimeClassName": "kata"},
            "privileged_extensions": {"runtimeClassName": "task-privileged"},
        },
        prepare={
            "management_profile": "management",
            "management_resources": {"cpu": "2", "memory": "4Gi"},
            "management_network": {"external": ["builder.example.test"]},
            "management_env": {
                "DOCKER_HOST": "tcp://builder.example.test:2376",
                "DOCKER_TLS_VERIFY": "1",
                "DOCKER_CERT_PATH": "/run/build-credentials",
            },
            "management_mounts": [
                {"key": "operator/build-credentials", "mount_path": "/run/build-credentials", "read_only": True}
            ],
        },
    )


def test_bootstrap_and_gc_do_not_inherit_builder_access() -> None:
    cfg = configured()
    management = cfg.anchor_settings(workspace=True)
    assert management.external == ExternalPolicy(mode="allowlist", hosts=("builder.example.test",), cidrs=())
    assert management.management_env["DOCKER_TLS_VERIFY"] == "1"
    assert management.profile == "management"
    assert management.resources == Resources(cpu="2", memory="4Gi")
    bootstrap = cfg.anchor_settings()
    assert bootstrap == config().anchor_settings()
    assert bootstrap.profile == "default"
    assert bootstrap.resources == Resources(cpu="0.5", memory="512Mi")
    assert bootstrap.external.mode == "none"
    assert not bootstrap.management_mounts and not bootstrap.management_env


@pytest.mark.parametrize(
    "mount",
    [
        SharedVolume(key="operator/keys", mount_path="/run/keys", read_only=False),
        SharedVolume(key="../keys", mount_path="/run/keys", read_only=True),
        SharedVolume(key="operator/keys", mount_path="/flotilla-root/sub", read_only=True),
        SharedVolume(key="operator/keys", mount_path="/.flotilla", read_only=True),
    ],
)
def test_management_mounts_are_explicit_readonly_and_nonoverlapping(mount: SharedVolume) -> None:
    with pytest.raises(ValueError):
        replace(config().anchor_settings(), management_mounts=(mount,))


@pytest.mark.asyncio
async def test_runtime_anchor_receives_explicit_builder_mount_and_network(
    caps: Capabilities, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = configured()
    clock = ManualClock()
    platform = FakePlatform(clock=clock, caps=caps)
    anchor = Anchor(platform, clock, "management-test", cfg.anchor_settings(workspace=True))
    await clock.run(anchor.start())
    try:
        assert anchor.iid is not None
        spec = platform.spec_of(anchor.iid)
        assert spec.external.hosts == ("builder.example.test",)
        assert spec.volumes[0].key == ""  # trusted management storage root
        assert spec.volumes[1].key == "operator/build-credentials"
        assert spec.volumes[1].read_only
        assert spec.profile == "management" and not spec.privileged
        assert spec.resources == Resources(cpu="2", memory="4Gi")
        # Exercise config -> backend factory -> request assembly without contacting a platform.
        monkeypatch.setenv("FLOTILLA_CRED", "test")
        backend = cfg.build_platform(clock=clock, caps=cfg.management_capabilities())
        try:
            assert isinstance(backend, OpenSandboxPlatform)
            assert not backend.caps.privileged_runtime  # Kata selection is not task privileged permission.
            body = backend._create_body(spec, "test-token")
            assert body["extensions"]["runtimeClassName"] == "kata"
            assert body["resourceLimits"] == body["resourceRequests"] == {"cpu": "2", "memory": "4Gi"}
            assert body["entrypoint"] == list(cfg.anchor_settings().entrypoint)
        finally:
            await backend.aclose()
    finally:
        await anchor.close()


def test_default_workspace_runtime_and_resources_are_unchanged() -> None:
    cfg = config()
    assert cfg.anchor_settings(workspace=True) == cfg.anchor_settings()
    # Resource sizing is independent of selecting an alternative runtime.
    larger = config(prepare={"management_resources": {"cpu": "4", "memory": "8Gi"}})
    assert larger.anchor_settings(workspace=True).resources == Resources(cpu="4", memory="8Gi")
    assert larger.anchor_settings(workspace=True).profile == "default"
    assert larger.anchor_settings() == cfg.anchor_settings()


@pytest.mark.parametrize("field", ["profile", "management_profile", "management_extensions"])
def test_task_data_cannot_select_management_profile(field: str) -> None:
    raw = {
        "image": "reg/app:tag",
        "resources": {},
        "context": {"env": {}, "uid": 0, "gid": 0, "cwd": "/"},
        "command": None,
        field: "management",
    }
    with pytest.raises(ValidationError, match=field):
        Unit.model_validate(raw)
    project = normalize({"services": {"main": {"image": "reg/app:tag", field: "management"}}}, {})
    assert any(f.kind == "reject" and f.path == f"services.main.{field}" for f in project.findings)
