"""能力报告（Platform_Requirements §5）：schema、必需项判定、安全缺口、到 `Capabilities` 的投影。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from flotilla.capabilities import SCHEMA_VERSION, CapabilityReport
from tests.unit.reports import no, ok, report, report_data


def test_round_trips_through_json() -> None:
    r = report()
    assert CapabilityReport.load_json(r.dump_json()) == r


def test_full_report_has_no_missing_required_or_gaps() -> None:
    r = report()
    assert r.missing_required() == [] and r.security_gaps() == []


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"lifecycle.label_list": no()}, "C1 按标签列出"),
        ({"exec.background": no()}, "C3 后台进程"),
        ({"storage.read_only": no()}, "C8 只读挂载"),
        ({"network.implicit_egress_declared": no()}, "C13 隐式放行声明"),
        ({"network.external_forms": ["none"]}, "C6 any 或 allowlist 至少其一"),
    ],
)
def test_missing_required(override: dict[str, Any], expected: str) -> None:
    assert expected in report(**override).missing_required()


def test_functional_and_degraded_items_are_not_required() -> None:
    r = report(**{"network.link": no(), "storage.shared_volume": no(), "runtime.diagnostics": no()})
    assert r.missing_required() == []


def test_security_gaps_listed_in_fixed_order() -> None:
    r = report(**{"runtime.exec_auth": no(), "network.inbound_isolation": no(), "storage.no_auto_mount": no()})
    assert r.security_gaps() == ["inbound_isolation", "exec_auth", "no_auto_mount"]


def test_to_capabilities_projects_fields() -> None:
    caps = report(**{"network.link": no(), "storage.volume_file": no()}).to_capabilities()
    assert caps.link is False and caps.link_udp is False  # 没有 link 时 UDP 也无从谈起
    assert caps.volume_file is False and caps.shared_volume is True
    assert caps.external_forms == frozenset({"none", "any", "allowlist"})
    assert caps.list_visibility_s == 30.0 and caps.max_ttl_seconds is None
    assert caps.devices == frozenset({"fuse"})


def test_devices_require_privileged_runtime() -> None:
    caps = report(**{"runtime.privileged_runtime": no()}).to_capabilities()
    assert caps.privileged_runtime is False and caps.devices == frozenset()


@pytest.mark.parametrize(
    "override",
    [
        {"lifecycle.max_label_value_len": 32},  # C1 第 5 条要求 ≥ 63
        {"lifecycle.list_visibility_s": 0},
        {"network.external_forms": ["all"]},
        {"network.internal_address": "none"},  # link 为真时必须取得到地址
        {"network.external_forms": ["any"]},  # external_none 为真却不含 none
    ],
)
def test_rejects_inconsistent_report(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        report(**override)


def test_rejects_unknown_fields_and_versions() -> None:
    data = report_data()
    data["runtime"]["gpu"] = ok()
    with pytest.raises(ValidationError):
        CapabilityReport.model_validate(data)
    with pytest.raises(ValueError, match="schema"):
        CapabilityReport.load_json(report().dump_json().replace(f'"schema": {SCHEMA_VERSION}', '"schema": 99'))
