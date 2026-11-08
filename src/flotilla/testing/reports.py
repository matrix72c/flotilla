"""Synthetic schema-2 evidence for offline tests only; never a runtime bootstrap."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from flotilla.capabilities import SCHEMA_VERSION, CapabilityReport


def ok() -> dict[str, Any]:
    return {"status": "passed", "evidence": "synthetic unit-test evidence"}


def no() -> dict[str, Any]:
    return {"status": "failed", "evidence": "synthetic unit-test failure"}


def report_data(**overrides: Any) -> dict[str, Any]:
    from flotilla.testing.config import config

    data: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "deployment": "test",
        "endpoint": "http://os.test",
        "probed_at": datetime(2026, 10, 7, tzinfo=UTC).isoformat(),
        "probe_version": "0.0.0",
        "evidence_profile": config().evidence_profile(),
        "lifecycle": {
            "create_get_delete": ok(),
            "label_list": ok(),
            "renew": ok(),
            "anchor_ttl": ok(),
            "trial_ttl": ok(),
            "max_label_value_len": 63,
            "list_visibility_s": 30.0,
            "max_ttl_seconds": None,
        },
        "exec": {
            "prerequisites": ok(),
            "exec_identity": ok(),
            "files": ok(),
            "placeholder_entry": ok(),
            "background": ok(),
        },
        "network": {
            "link": ok(),
            "link_udp": ok(),
            "link_max_members": 10,
            "inbound_isolation": ok(),
            "external_none": ok(),
            "external_any": ok(),
            "external_allowlist": ok(),
            "network_acceptance": ok(),
            "dns": ok(),
            "internal_address": "exec",
            "implicit_egress": ["10.96.0.10:53/udp"],
            "cluster_dns": ["10.96.0.10"],
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
            "exec_auth": ok(),
            "memory_limit": ok(),
            "cpu_limit": ok(),
            "cpu_limit_ratio": 1.0,
            "privileged_runtime": {**ok(), "scope": "privileged"},
            "privileged_storage": {**ok(), "scope": "privileged"},
            "privileged_exec_auth": {**ok(), "scope": "privileged"},
            "privileged_network": {**ok(), "scope": "privileged"},
            "privileged_inbound_isolation": {**ok(), "scope": "privileged"},
            "privileged_no_auto_mount": {**ok(), "scope": "privileged"},
            "fuse": {**ok(), "scope": "privileged"},
            "image_digest": ok(),
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
