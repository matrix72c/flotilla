"""Shared unit test fixtures: a hand-written capability report, a matching `FakePlatform`, and the shared tree model."""

from __future__ import annotations

import pytest

from flotilla.platform.base import Capabilities
from flotilla.testing.platform import FakePlatform, ManualClock, SharedTree


@pytest.fixture
def caps() -> Capabilities:
    """Test capability declaration: link available, address via exec, list visibility within 30s."""
    return Capabilities(
        max_label_value_len=63,
        list_visibility_s=30.0,
        max_ttl_seconds=21600,
        link=True,
        link_udp=False,
        link_max_members=10,
        inbound_isolation=True,
        external_forms=frozenset({"none", "any"}),
        implicit_egress=(),
        internal_address="exec",
        shared_volume=True,
        volume_file=True,
        no_auto_mount=True,
        exec_auth=True,
        privileged_runtime=False,
        devices=frozenset(),
    )


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def platform(caps: Capabilities, clock: ManualClock) -> FakePlatform:
    return FakePlatform(caps, clock=clock)


@pytest.fixture
def tree(platform: FakePlatform) -> SharedTree:
    return SharedTree(platform)
