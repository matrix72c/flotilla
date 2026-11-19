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
            "network": {"sandbox_cidrs": ["10.0.0.0/16"], "platform_cidrs": ["169.254.169.254/32"]},
        },
        "storage": {"host_path": "/mnt/shared"},
        # A published dataset is a read-only input root: that is where a task's files may live.
        "inputs": {"datasets": {"host_path": "/mnt/datasets"}},
        "share": {"release": "2026-10-07.1", "release_sha256": SHA},
        "network": {"external": ["gateway.example.com", "10.20.0.0/24"]},
        "reaper": {"anchor_image": "reg/flotilla@sha256:" + "a" * 64},
    }
    data.update(sections)
    return data


def config(**sections: Any) -> FlotillaConfig:
    return FlotillaConfig.from_mapping(config_data(**sections))


def importable(spec: FlotillaConfig, name: str = "", attribute: str = "DEPLOYMENT") -> str:
    """Publish `spec` as an importable module attribute and return its `module:attribute` target.

    A deployment is named by where to import it from, so anything taking that name -- the CLI,
    `Deployment.load`, a Harbor task source -- needs a spec some module actually holds. This puts
    one in a synthetic module instead of writing a file. Not for production: nothing unregisters
    the module, so it lives as long as the process.
    """
    import sys
    import types
    import uuid

    module_name = name or f"flotilla_test_deployment_{uuid.uuid4().hex[:12]}"
    module = sys.modules.get(module_name) or types.ModuleType(module_name)
    setattr(module, attribute, spec)
    sys.modules[module_name] = module
    return f"{module_name}:{attribute}"
