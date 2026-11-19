"""Synthetic isolation evidence for offline tests only; never a substitute for a probe run."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from flotilla.capabilities import Evidence, IsolationEvidence


def ok() -> Evidence:
    return Evidence(True, "synthetic unit-test evidence")


def no() -> Evidence:
    return Evidence(False, "synthetic unit-test failure")


def evidence(**overrides: Any) -> IsolationEvidence:
    """Everything held, including the privileged runtime. Override one field to test a gap."""
    held = {
        "external_none": ok(),
        "network_acceptance": ok(),
        "inbound_isolation": ok(),
        "read_only": ok(),
        "subdir_isolation": ok(),
        "no_auto_mount": ok(),
        "exec_auth": ok(),
        "cleanup": ok(),
        "privileged_storage": ok(),
        "privileged_no_auto_mount": ok(),
        "privileged_exec_auth": ok(),
        "privileged_network": ok(),
        "privileged_inbound_isolation": ok(),
        "external_forms": frozenset({"none", "any", "allowlist"}),
        "implicit_egress": ("10.96.0.10:53/udp",),
        "cluster_dns": ("10.96.0.10",),
    }
    return replace(IsolationEvidence(**held), **overrides)  # type: ignore[arg-type]
