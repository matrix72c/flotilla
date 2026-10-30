"""Credential binding for loadtest entrypoints (no external project dependencies).

`bind_credential` ports the training-side provider: the platform credential is composed
from an access key pair in a private env file and exported under the deployment's
`credential_env` name. Values never appear in logs or command lines.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from flotilla.config import FlotillaConfig


def read_env_file_values(path: str | Path, names: tuple[str, ...]) -> dict[str, str]:
    """Read required literal assignments from a private ``.env`` file."""
    resolved = Path(path).expanduser()
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or resolved.is_symlink():
        raise RuntimeError(f"secret environment path is not a regular, non-symlink file: {resolved}")
    if info.st_mode & 0o077:
        raise RuntimeError(f"secret environment file must not grant permissions to group or other: {resolved}")
    values: dict[str, str] = {}
    wanted = set(names)
    for line in resolved.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip().strip("'\"")
        if key in wanted:
            values[key] = value
    missing = sorted(wanted - set(values))
    if missing:
        raise RuntimeError(f"secret environment file lacks required entries: {missing}")
    return values


def bind_credential(config: FlotillaConfig, secret_file: str | Path) -> None:
    """Export the platform credential under the deployment's credential env name."""
    values = read_env_file_values(
        secret_file, ("OPEN_SANDBOX_ACCESS_KEY", "OPEN_SANDBOX_SECRET_KEY")
    )
    credential = values["OPEN_SANDBOX_ACCESS_KEY"] + ":" + values["OPEN_SANDBOX_SECRET_KEY"]
    name = config.opensandbox.credential_env
    existing = os.environ.get(name)
    if existing is not None and existing != credential:
        raise ValueError("worker credential environment conflicts with secret file")
    os.environ[name] = credential


def env_path(name: str) -> Path | None:
    """Optional path argument sourced from the environment."""
    value = os.environ.get(name)
    return Path(value) if value else None
