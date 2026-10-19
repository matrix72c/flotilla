"""Capability report: schema, required items, security gaps, `Capabilities` projection."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from flotilla.capabilities import SCHEMA_VERSION, CapabilityReport
from tests.support.reports import no, ok, report, report_data


def test_round_trips_through_json() -> None:
    r = report()
    assert CapabilityReport.load_json(r.dump_json()) == r


def test_full_report_has_no_missing_required_or_gaps() -> None:
    r = report()
    assert r.missing_required() == [] and r.security_gaps() == []


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"lifecycle.label_list": no()}, "lifecycle list by label"),
        ({"exec.background": no()}, "background processes"),
        ({"storage.read_only": no()}, "read-only mounts"),
        ({"network.implicit_egress_declared": no()}, "implicit egress declaration"),
        ({"network.external_forms": ["none"]}, "egress any or allowlist"),
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
    assert caps.link is False and caps.link_udp is False  # without link, UDP is moot
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
        {"lifecycle.max_label_value_len": 32},  # label values must allow at least 63 characters
        {"lifecycle.list_visibility_s": 0},
        {"network.external_forms": ["all"]},
        {"network.internal_address": "none"},  # an address must be obtainable when link is true
        {"network.external_forms": ["any"]},  # external_none is true but none is missing
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
