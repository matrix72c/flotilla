"""Capability report builders: a fully met report, overridden per field. Used by `test_capabilities`/`test_config`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from flotilla.capabilities import SCHEMA_VERSION, CapabilityReport


def ok(source: str = "probed") -> dict[str, Any]:
    return {"ok": True, "source": source}


def no(source: str = "probed") -> dict[str, Any]:
    return {"ok": False, "source": source}


def report_data(**overrides: Any) -> dict[str, Any]:
    """A report with all required, security and multi-service capabilities met; override by "section.field" or field."""
    data: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "deployment": "test",
        "endpoint": "http://os.test",
        "probed_at": datetime(2026, 10, 7, tzinfo=UTC).isoformat(),
        "probe_version": "0.0.0",
        "lifecycle": {
            "create_get_delete": ok(),
            "label_list": ok(),
            "renew": ok(),
            "max_label_value_len": 63,
            "list_visibility_s": 30.0,
            "max_ttl_seconds": None,
        },
        "exec": {"exec_identity": ok(), "files": ok(), "placeholder_entry": ok(), "background": ok()},
        "network": {
            "link": ok(),
            "link_udp": ok(),
            "link_max_members": 10,
            "inbound_isolation": ok("declared"),
            "external_forms": ["none", "any", "allowlist"],
            "external_none": ok(),
            "internal_address": "exec",
            "implicit_egress": ["10.96.0.10:53/udp"],
            "implicit_egress_declared": ok("declared"),
        },
        "storage": {
            "read_only": ok(),
            "shared_volume": ok(),
            "subdir_isolation": ok(),
            "missing_subpath": no(),
            "no_auto_mount": ok(),
            "dir_management": ok(),
            "volume_file": ok(),
        },
        "runtime": {
            "exec_auth": ok("declared"),
            "memory_limit": ok(),
            "cpu_limit": ok(),
            "cpu_limit_ratio": 1.0,
            "diagnostics": ok(),
            "privileged_runtime": ok(),
            "devices": ["fuse"],
            "image_digest": ok(),
            "multi_container": no("declared"),
        },
    }
    for path, value in overrides.items():
        section, _, field = path.partition(".")
        if field:
            data[section][field] = value
        else:
            data[section] = value
    return data


def report(**overrides: Any) -> CapabilityReport:
    return CapabilityReport.model_validate(report_data(**overrides))
