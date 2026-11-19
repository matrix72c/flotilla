"""Sandbox backend settings and the shape of a backend.

A backend is registered by adding it to `BACKENDS` in `flotilla.config` -- that table is the whole
discovery mechanism. The built-in OpenSandbox backend is configured by `[opensandbox]`; another
backend is selected with `backend = "<name>"` plus a `[backends.<name>]` table, validated by its
`settings_model`.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator

from flotilla.platform.base import Capabilities, Clock, Platform

if TYPE_CHECKING:
    from flotilla.config import FlotillaConfig

BUILTIN = "opensandbox"
_NAME = re.compile(r"[a-z][a-z0-9_-]*")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class BackendSettings(BaseModel):
    """Fields every backend table has. Backends subclass this for their own keys; unknown keys are rejected."""

    # hide_input_in_errors: validation errors do not echo input, so a misplaced credential (e.g. unknown field
    # credential = "ak:sk") never reaches terminals or logs. Errors still carry the field path and reason.
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, allow_inf_nan=False)

    endpoint: str  # identifies the deployment
    credential_env: str

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


@runtime_checkable
class Backend[S: BackendSettings](Protocol):
    """One sandbox platform: its settings table, what it can enforce, and how to build its `Platform` client."""

    settings_model: type[BackendSettings]

    def check(self, settings: S, cfg: FlotillaConfig) -> list[str]: ...

    def capabilities(self, settings: S, cfg: FlotillaConfig, caps: Capabilities) -> Capabilities: ...

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
