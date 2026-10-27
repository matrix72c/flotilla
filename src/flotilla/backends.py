"""Sandbox backend plugins: the settings every backend table carries and how a backend builds its `Platform`.

The built-in OpenSandbox backend is configured by `[opensandbox]`. A third-party backend registers a `Backend`
subclass (or instance) in the `flotilla.backends` entry-point group and is selected with `backend = "<name>"`
plus a `[backends.<name>]` table, validated by its `settings_model`.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from importlib import import_module
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator

from flotilla.platform.base import Capabilities, Clock, Platform

if TYPE_CHECKING:
    from flotilla.config import FlotillaConfig

ENTRY_POINT_GROUP = "flotilla.backends"
BUILTIN = "opensandbox"
_BUILTINS = {BUILTIN: "flotilla.config:OpenSandboxBackend"}
_NAME = re.compile(r"[a-z][a-z0-9_-]*")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_COMMON = frozenset({"endpoint", "credential_env", "capabilities"})


class BackendSettings(BaseModel):
    """Fields every backend table has. Backends subclass this for their own keys; unknown keys are rejected."""

    # hide_input_in_errors: validation errors do not echo input, so a misplaced credential (e.g. unknown field
    # credential = "ak:sk") never reaches terminals or logs. Errors still carry the field path and reason.
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, allow_inf_nan=False)

    endpoint: str  # identifies the deployment; the capability report must record the same value
    credential_env: str
    capabilities: str  # capability report path (from `flotilla probe`); relative to the config file's directory

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"endpoint must be an http(s) URL: {value!r}")
        return value.rstrip("/")

    @field_validator("credential_env")
    @classmethod
    def _credential_env(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            # Do not echo the value: the most common mistake here is pasting the credential itself.
            raise ValueError(
                "credential_env must be an environment variable name ([A-Za-z_][A-Za-z0-9_]*), not the credential"
            )
        return value


class Backend[S: BackendSettings](ABC):
    """One sandbox platform. Only `build` is required; the other hooks have conservative defaults."""

    settings_model: ClassVar[type[BackendSettings]] = BackendSettings

    def check(self, settings: S, cfg: FlotillaConfig) -> list[str]:
        """Cross-field problems between the backend table and the rest of the config, checked at load."""
        return []

    def capabilities(self, settings: S, cfg: FlotillaConfig, caps: Capabilities) -> Capabilities:
        """Adjust capabilities from the report for what this configuration enforces or cannot provide."""
        return caps

    def profile(self, settings: S, cfg: FlotillaConfig) -> str:
        """Fingerprint of the settings that optional filesystem evidence depends on.

        `flotilla probe` records it, and read-only rootfs / native tmpfs evidence is ignored when it changes. The
        default covers every backend-specific key, so any change to them requires a new probe.
        """
        data = settings.model_dump(mode="json", exclude=set(_COMMON))
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @abstractmethod
    def build(
        self,
        settings: S,
        cfg: FlotillaConfig,
        *,
        clock: Clock,
        credential: str,
        caps: Capabilities,
        command_gate: Callable[[], Awaitable[None]] | None = None,
    ) -> Platform:
        """Construct a platform client. The caller owns it and calls `aclose()`.

        `command_gate`, when given, must be awaited before each user command request (the client's exec rate limit).
        """


@functools.cache
def load_backend(name: str) -> Backend[Any]:
    """Resolve a backend by name: built-in first, then the `flotilla.backends` entry points."""
    if not _NAME.fullmatch(name):
        raise ValueError(f"backend must match [a-z][a-z0-9_-]*: {name!r}")
    if name in _BUILTINS:
        module, _, attr = _BUILTINS[name].partition(":")
        target = getattr(import_module(module), attr)
    else:
        found = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == name]
        if not found:
            installed = sorted({BUILTIN, *(ep.name for ep in entry_points(group=ENTRY_POINT_GROUP))})
            raise ValueError(f"unknown backend {name!r}; installed: {', '.join(installed)}")
        if len(found) > 1:
            raise ValueError(f"backend {name!r} is registered by more than one installed package")
        target = found[0].load()
    backend = target() if isinstance(target, type) else target
    if not isinstance(backend, Backend):
        raise ValueError(f"backend {name!r} does not provide a flotilla.backends.Backend")
    return backend
