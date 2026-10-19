"""Construct the OpenSandbox backend with explicit deployment settings."""

from __future__ import annotations

from flotilla.platform.opensandbox.execd import ExecdSettings
from flotilla.platform.opensandbox.network import NetworkSettings
from flotilla.platform.opensandbox.platform import OpenSandboxPlatform, build
from flotilla.platform.opensandbox.storage import StorageSettings

__all__ = ["ExecdSettings", "NetworkSettings", "OpenSandboxPlatform", "StorageSettings", "build"]
