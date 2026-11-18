"""Measured outcomes and evidence projection (no declared/manual source)."""

import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from flotilla.capabilities import CapabilityReport, Item
from flotilla.testing.reports import no, report, report_data


def test_round_trip_and_atomic_write(tmp_path: Path) -> None:
    value = report()
    path = tmp_path / "capabilities.json"
    value.write_json(path)
    assert CapabilityReport.load_json(path.read_bytes()) == value
    assert list(tmp_path.iterdir()) == [path]


def test_full_report_required_and_security() -> None:
    value = report()
    assert value.missing_required() == []
    assert value.security_gaps() == []


@pytest.mark.parametrize("status", ["failed", "error", "skipped"])
def test_non_passed_outcomes_cannot_grant_capabilities(status: Literal["failed", "error", "skipped"]) -> None:
    value = report(
        **{
            "network.link": {"status": status},
            "storage.volume_file": {"status": status},
            "runtime.readonly_rootfs": {"status": status},
        }
    )
    caps = value.to_capabilities()
    assert not caps.link and not caps.link_udp and not caps.volume_file and not caps.readonly_rootfs
    assert "ok" not in Item(status=status).model_dump()
    assert "source" not in Item(status=status).model_dump()


def test_manual_fields_and_legacy_schema_rejected() -> None:
    with pytest.raises(ValidationError):
        Item.model_validate({"ok": True, "source": "declared"})
    for schema in (1, 99):
        data = report_data(schema=schema)
        with pytest.raises(ValueError, match="schema"):
            CapabilityReport.load_json(json.dumps(data))


def test_no_network_requires_acceptance_not_empty_exception_list() -> None:
    caps = report(**{"network.network_acceptance": no(), "network.implicit_egress": []}).to_capabilities()
    assert not caps.supports_no_network
    assert "none" not in caps.external_forms


def test_unknown_address_cannot_enable_links() -> None:
    assert not report(**{"network.internal_address": "none"}).to_capabilities().link


def test_privileged_runtime_is_the_nested_daemon_measurement() -> None:
    """One runtime per deployment: `privileged_runtime` says what that runtime offers, nothing per instance."""
    from flotilla.testing.config import config

    assert report().to_capabilities().privileged_runtime
    for status in ("skipped", "failed", "error"):
        value = report(**{"runtime.privileged_runtime": {"status": status}})
        assert not value.to_capabilities().privileged_runtime
        assert not config().platform_capabilities(value).privileged_runtime


def test_devices_rest_on_their_own_measurements() -> None:
    assert report().to_capabilities().devices == frozenset({"fuse"})
    assert not report(**{"runtime.fuse": {"status": "failed"}}).to_capabilities().devices
    # A deployment can attach a device without hosting a nested daemon; the two are measured apart.
    partial = report(**{"runtime.privileged_runtime": {"status": "failed"}})
    assert partial.to_capabilities().devices == frozenset({"fuse"})


def test_anchor_writable_is_measured_not_assumed() -> None:
    """Without passed evidence, writable trial volumes have to be opened to every identity."""
    assert report().to_capabilities().anchor_writable
    for status in ("skipped", "failed", "error"):
        assert not report(**{"storage.anchor_writable": {"status": status}}).to_capabilities().anchor_writable


def test_required_and_security_failure_order() -> None:
    value = report(
        **{
            "lifecycle.label_list": no(),
            "runtime.exec_auth": no(),
            "network.inbound_isolation": no(),
            "storage.no_auto_mount": no(),
        }
    )
    assert "lifecycle list by label" in value.missing_required()
    assert value.security_gaps() == ["inbound_isolation", "exec_auth", "no_auto_mount"]
