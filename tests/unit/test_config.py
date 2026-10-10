"""`FlotillaConfig`（Architecture §16.3）：读入校验、与能力报告比对、到 core / 后端设置的翻译。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from flotilla.config import ConfigError, FlotillaConfig
from flotilla.platform.base import ExternalPolicy
from tests.unit.reports import no, report

SHA = "0" * 64


def config_data(**sections: Any) -> dict[str, Any]:
    """最小可用的配置：cidr 策略、host 卷、窄列表出站；`sections` 整段替换或新增。"""
    data: dict[str, Any] = {
        "opensandbox": {
            "endpoint": "http://os.test/",
            "credential_env": "FLOTILLA_CRED",
            "capabilities": "deployments/test.json",
            "network": {"sandbox_cidrs": ["10.0.0.0/16"], "platform_cidrs": ["169.254.169.254/32"]},
        },
        "storage": {"host_path": "/mnt/shared"},
        "share": {"release": "2026-10-07.1", "release_sha256": SHA},
        "network": {"external": ["gateway.example.com", "10.20.0.0/24"]},
        "reaper": {"anchor_image": "reg/flotilla@sha256:" + "a" * 64},
    }
    data.update(sections)
    return data


def config(**sections: Any) -> FlotillaConfig:
    return FlotillaConfig.from_mapping(config_data(**sections))


# ───────────────────────────── 读入 ─────────────────────────────


def test_loads_toml_and_resolves_report_path(tmp_path: Path) -> None:
    (tmp_path / "flotilla.toml").write_text(
        f"""
[opensandbox]
endpoint = "http://os.test"
credential_env = "FLOTILLA_CRED"
capabilities = "deployments/test.json"

[opensandbox.network]
sandbox_cidrs = ["10.0.0.0/16"]

[opensandbox.create_fields]
ports = [{{ containerPort = 44772, name = "execd", protocol = "tcp" }}]

[storage]
volumes = "host"
host_path = "/srv/flotilla"

[share]
release = "2026-10-07.1"
release_sha256 = "{SHA}"
"""
    )
    cfg = FlotillaConfig.load(tmp_path / "flotilla.toml")
    assert cfg.capabilities_path() == tmp_path / "deployments/test.json"
    assert cfg.opensandbox.create_fields["ports"][0]["containerPort"] == 44772
    assert cfg.storage_settings().host_path == "/srv/flotilla"
    assert cfg.network.policy() == ExternalPolicy(mode="none")  # 未给 external：只能 none


def test_pvc_storage_translates_to_settings() -> None:
    cfg = config(storage={"volumes": "pvc", "claim_name": "project-data", "root_subpath": "opt/flotilla"})
    settings = cfg.storage_settings()
    assert (settings.volumes, settings.claim_name, settings.root_subpath) == ("pvc", "project-data", "opt/flotilla")


def test_endpoint_trailing_slash_normalized() -> None:
    assert config().opensandbox.endpoint == "http://os.test"


@pytest.mark.parametrize(
    "sections",
    [
        {"unknown": {}},  # 未知段落
        {"opensandbox": {**config_data()["opensandbox"], "credential": "ak:sk"}},  # 凭证不能写进配置
        {"opensandbox": {**config_data()["opensandbox"], "credential_env": "ak:sk"}},
        {"opensandbox": {**config_data()["opensandbox"], "endpoint": "os.test"}},
        {"storage": {"volumes": "unknown", "host_path": "/mnt"}},  # 未支持的卷形式
        {"storage": {"volumes": "pvc", "host_path": "/mnt"}},  # pvc 形式缺 claim_name
        {"share": {"release": "../x", "release_sha256": SHA}},
        {"share": {"release": "r1", "release_sha256": "abc"}},
        {"network": {"external": ["0.0.0.0/0"]}},  # 任意外网须写 "any"
        {"network": {"external": ["any"]}},  # 列表中的 "any" 不是任意外网
        {"network": {"external": ["1.2.3"]}},  # 残缺 IP 不是域名
        {"network": {"external": ["*.com"]}},  # 通配整个顶级域
        {"network": {"external": ["example.-com-"]}},
        {"network": {"external": ["https://gateway"]}},
        {"network": {"external": "all"}},
        {"reaper": {"renew_interval": 60, "grace": 90}},  # 续期间隔不够"明显小于" G
        {"reaper": {"renew_interval": 2, "grace": 90}},  # 短于锚点续期失败的重试间隔
        {"reaper": {"grace": 90, "gc_window": 60}},
        {"limits": {"exec_rate": 0}},
    ],
)
def test_rejects_invalid_config(sections: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        config(**sections)


def test_any_requires_platform_cidrs() -> None:
    sandbox_only = {**config_data()["opensandbox"], "network": {"sandbox_cidrs": ["10.0.0.0/16"]}}
    with pytest.raises(ValidationError, match="platform_cidrs"):
        config(opensandbox=sandbox_only, network={"external": "any"})


def test_cidr_link_requires_sandbox_cidrs() -> None:
    with pytest.raises(ValidationError, match="sandbox_cidrs"):
        config(opensandbox={**config_data()["opensandbox"], "network": {}}, network={"external": []})


def test_credential_from_environment_only() -> None:
    cfg = config()
    assert cfg.credential({"FLOTILLA_CRED": "ak:sk"}) == "ak:sk"
    with pytest.raises(ConfigError, match="FLOTILLA_CRED") as exc:
        cfg.credential({})
    assert "ak:sk" not in str(exc.value)


# ───────────────────────────── 与能力报告比对 ─────────────────────────────


def test_matching_report_passes() -> None:
    config().check_against(report())


def test_collects_all_problems() -> None:
    bad = report(
        **{
            "lifecycle.label_list": no(),
            "runtime.exec_auth": no(),
            "network.external_forms": ["none", "any"],
        }
    )
    with pytest.raises(ConfigError) as exc:
        config().check_against(bad)
    problems = exc.value.problems
    assert any("C1 按标签列出" in p for p in problems)
    assert any("exec_auth" in p and "accept_insecure" in p for p in problems)
    assert any("allowlist" in p for p in problems)  # 窄列表需要部署支持 allowlist


def test_endpoint_mismatch_rejected() -> None:
    with pytest.raises(ConfigError, match="endpoint"):
        config().check_against(report(endpoint="http://other.test"))


def test_security_gaps_require_explicit_acceptance_and_no_stale_entries() -> None:
    gap = report(**{"storage.no_auto_mount": no()})
    config(security={"accept_insecure": ["no_auto_mount"]}).check_against(gap)
    with pytest.raises(ConfigError, match="no_auto_mount"):
        config().check_against(gap)
    with pytest.raises(ConfigError, match="已满足"):
        config(security={"accept_insecure": ["exec_auth"]}).check_against(report())


def test_gc_window_must_exceed_grace_plus_visibility() -> None:
    slow = report(**{"lifecycle.list_visibility_s": 120.0})  # 90 + 120 ≥ 180
    with pytest.raises(ConfigError, match="gc_window"):
        config().check_against(slow)
    config(reaper={**config_data()["reaper"], "gc_window": 300}).check_against(slow)


@pytest.mark.parametrize(
    ("external", "value", "ok"),
    [
        (["gateway.example.com"], "https://gateway.example.com/v1", True),
        (["*.example.com"], "https://gw.example.com/v1", True),
        (["10.20.0.0/24"], "http://10.20.0.7:8000/v1", True),
        (["gateway.example.com"], "https://other.example.com/v1", False),
        (["10.20.0.0/24"], "http://10.30.0.7/v1", False),
        (["gateway.example.com"], "qwen3-32b", True),  # 不像地址：不涉及网络
        (["10.20.0.0/24"], "10.30.0.7:8000/v1", False),  # 不带 scheme 的地址同样检查
        (["gateway.example.com"], "gateway.example.com:8443", True),
    ],
)
def test_runtime_params_must_be_reachable(external: list[str], value: str, ok: bool) -> None:
    cfg = config(network={"external": external}, runtime_params={"UPSTREAM": value})
    if ok:
        cfg.check_against(report())
    else:
        with pytest.raises(ConfigError, match="UPSTREAM"):
            cfg.check_against(report())


def test_any_rejects_runtime_param_on_platform_infrastructure() -> None:
    cfg = config(network={"external": "any"}, runtime_params={"META": "http://169.254.169.254/latest"})
    with pytest.raises(ConfigError, match="平台基础设施"):
        cfg.check_against(report())


# ───────────────────────────── 翻译 ─────────────────────────────


def test_external_policy_forms() -> None:
    assert config(network={"external": "any"}).network.policy() == ExternalPolicy(mode="any")
    assert config(network={"external": []}).network.policy() == ExternalPolicy(mode="none")
    narrow = config().network.policy()
    assert narrow == ExternalPolicy(mode="allowlist", hosts=("gateway.example.com",), cidrs=("10.20.0.0/24",))


def test_translates_to_core_and_backend_settings() -> None:
    cfg = config(
        timeouts={"create": 300, "close": 5},
        reaper={**config_data()["reaper"], "trial_ttl": 7200},
        opensandbox={**config_data()["opensandbox"], "execd": {"protocol": "legacy"}},
    )
    trial = cfg.trial_settings(extra_labels={"run": "r1"})
    assert trial.share_release == "2026-10-07.1" and trial.create_timeout_s == 300 and trial.ttl_s == 7200
    assert trial.external.mode == "allowlist" and trial.extra_labels == {"run": "r1"}
    assert cfg.reaper_settings().close_timeout_s == 5
    assert cfg.monitor_settings().ttl_s == 7200
    assert cfg.anchor_settings().grace_s == 90 and cfg.anchor_settings().image.startswith("reg/flotilla@")
    assert cfg.execd_settings().protocol == "legacy"
    assert cfg.network_settings().sandbox_cidrs == ("10.0.0.0/16",)
    assert cfg.default_resources("agent").memory == "4Gi"


def test_anchor_image_required_for_anchor_settings() -> None:
    with pytest.raises(ConfigError, match="anchor_image"):
        config(reaper={}).anchor_settings()


@pytest.mark.parametrize(
    "section",
    [
        {"opensandbox": {**config_data()["opensandbox"], "credential": "AK:SECRET"}},
        {"opensandbox": {**config_data()["opensandbox"], "credential_env": "AK:SECRET"}},
        {"credential": "AK:SECRET"},
    ],
)
def test_validation_errors_never_echo_values(section: dict[str, Any]) -> None:
    # 凭证最常见的误放位置：未知字段或 credential_env 本身。错误消息只有字段路径与原因（原则 8）。
    with pytest.raises(ValidationError) as exc:
        config(**section)
    assert "SECRET" not in str(exc.value)


def test_any_rejects_runtime_param_inside_sandbox_cidrs() -> None:
    cfg = config(network={"external": "any"}, runtime_params={"PEER": "http://10.0.3.4:8000"})
    with pytest.raises(ConfigError, match="sandbox 网段"):
        cfg.check_against(report())
