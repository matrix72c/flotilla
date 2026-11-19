"""Offline policy survives planning, caller overrides, and service wiring at open."""

from pathlib import Path

import pytest

from flotilla import Flotilla, OpenOptions
from flotilla.config import FlotillaConfig
from flotilla.platform.base import FlotillaError
from flotilla.testing.planning import APP, DIGEST2, planned_environment
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as deployment


def _with_cluster_dns(cfg: FlotillaConfig, platform: FakePlatform, *, extra: bool = False) -> FlotillaConfig:
    """A deployment whose resolver reaches out, plus optionally one address nobody declared."""
    egress = ["10.96.0.10:53/udp,tcp"] + (["169.254.169.254:80/tcp"] if extra else [])
    cfg = cfg.model_copy(
        update={"platform": cfg.platform.model_copy(update={"cluster_dns": ["10.96.0.10"], "implicit_egress": egress})}
    )
    platform.caps = cfg.platform_capabilities()
    return cfg


@pytest.mark.asyncio
# The `legacy` case declares Harbor's deprecated allow_internet on purpose: it must keep working.
@pytest.mark.filterwarnings("ignore:The 'allow_internet' field is deprecated")
@pytest.mark.parametrize("mode", ["task", "legacy", "option"])
async def test_no_network_keeps_peers_and_none_keeps_no_peers(
    tmp_path: Path,
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    mode: str,
) -> None:
    cfg, platform, _ = deployment
    cfg = cfg.model_copy(update={"network": cfg.network.model_copy(update={"external": "any"})})
    cfg = _with_cluster_dns(cfg, platform)
    source = tmp_path / "offline"
    (source / "environment").mkdir(parents=True)
    policy = {"task": 'network_mode = "no-network"', "legacy": "allow_internet = false", "option": ""}[mode]
    (source / "task.toml").write_text(f"[environment]\n{policy}\n")
    (source / "environment/docker-compose.yaml").write_text(
        f"services:\n  main:\n    image: {APP}\n  db:\n    image: r.io/ns/db@{DIGEST2}\n"
        f"  isolated:\n    image: r.io/ns/init@{DIGEST2}\n    network_mode: none\n"
    )
    async with (
        Flotilla(deployment=cfg) as fleet,
        fleet.environment(planned_environment(source, cfg), OpenOptions(disable_internet=mode == "option")) as env,
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
    cfg = _with_cluster_dns(cfg, platform, extra=True)
    source = tmp_path / "offline"
    (source / "environment").mkdir(parents=True)
    policy = 'network_mode = "no-network"' if mode == "task" else ""
    (source / "task.toml").write_text(f"[environment]\n{policy}\n")
    compose = f"services:\n  main:\n    image: {APP}\n"
    if mode == "compose":
        compose += "    network_mode: none\n"
    (source / "environment/docker-compose.yaml").write_text(compose)
    async with Flotilla(deployment=cfg) as fleet:
        with pytest.raises((ValueError, FlotillaError), match="cluster DNS"):
            await fleet.open(planned_environment(source, cfg), OpenOptions(disable_internet=mode == "option"))
        assert all(platform.spec_of(i.iid).labels.get("flotilla/role") == "anchor" for i in platform.instances())
