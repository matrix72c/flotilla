from __future__ import annotations

from typing import Any

from flotilla.config import FlotillaConfig

SHA = "0" * 64


def config_data(**sections: Any) -> dict[str, Any]:
    """Minimal working config: cidr policy, host volumes, allowlist egress; `sections` replace or add sections."""
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
