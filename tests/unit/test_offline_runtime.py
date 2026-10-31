"""Offline policy survives preparation, caller overrides, and service wiring at open."""

import json
from pathlib import Path

import pytest

from flotilla import Flotilla, OpenOptions
from flotilla.config import FlotillaConfig
from flotilla.platform.base import FlotillaError
from flotilla.testing.build import prepared_environment
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as deployment


def _dns_report(cfg: FlotillaConfig, platform: FakePlatform, *, extra: bool = False) -> None:
    data = cfg.load_report().model_dump(mode="json", by_alias=True)
    data["network"]["cluster_dns"] = ["10.96.0.10"]
    data["network"]["implicit_egress"] = ["10.96.0.10:53/udp,tcp"]
    if extra:
        data["network"]["implicit_egress"].append("169.254.169.254:80/tcp")
    cfg.capabilities_path().write_text(json.dumps(data))
    platform.caps = cfg.platform_capabilities()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["task", "legacy", "option"])
async def test_no_network_keeps_peers_and_none_keeps_no_peers(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    mode: str,
) -> None:
    cfg, platform, _ = deployment
    cfg = cfg.model_copy(update={"network": cfg.network.model_copy(update={"external": "any"})})
    _dns_report(cfg, platform)
    source = tmp_path / "offline"
    (source / "environment").mkdir(parents=True)
    policy = {"task": 'network_mode = "no-network"', "legacy": "allow_internet = false", "option": ""}[mode]
    (source / "task.toml").write_text(f"[environment]\n{policy}\n")
    (source / "environment/docker-compose.yaml").write_text(
        "services:\n  main:\n    image: r.io/main:1\n  db:\n    image: r.io/db:1\n"
        "  isolated:\n    image: r.io/init:1\n    network_mode: none\n"
    )
    async with (
        Flotilla(deployment=cfg) as fleet,
        fleet.environment(prepared_environment(source, cfg), OpenOptions(disable_internet=mode == "option")) as env,
    ):
        services = {s.name: s.instance_id for s in (await env.info()).services}
        assert all(platform.spec_of(iid).external.mode == "none" for iid in services.values())
        topology = platform.topology_of(services["main"])
        assert topology is not None and topology.networks == {"default": frozenset({"main", "db"})}
        assert platform.topology_of(services["isolated"]) is None
        assert platform.spec_of(services["isolated"]).group is None
        assert (await env.agent.exec(["true"])).exit_code == 0
    assert fleet.cleanup_report is not None and fleet.cleanup_report.clean


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["task", "compose", "option"])
async def test_unknown_implicit_egress_rejects_each_offline_entrypoint(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    mode: str,
) -> None:
    cfg, platform, _ = deployment
    _dns_report(cfg, platform, extra=True)
    source = tmp_path / "offline"
    (source / "environment").mkdir(parents=True)
    policy = 'network_mode = "no-network"' if mode == "task" else ""
    (source / "task.toml").write_text(f"[environment]\n{policy}\n")
    compose = "services:\n  main:\n    image: r.io/main:1\n"
    if mode == "compose":
        compose += "    network_mode: none\n"
    (source / "environment/docker-compose.yaml").write_text(compose)
    async with Flotilla(deployment=cfg) as fleet:
        with pytest.raises((ValueError, FlotillaError), match="cluster DNS"):
            await fleet.open(prepared_environment(source, cfg), OpenOptions(disable_internet=mode == "option"))
        assert all(platform.spec_of(i.iid).labels.get("flotilla/role") == "anchor" for i in platform.instances())
