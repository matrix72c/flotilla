"""单元测试共用的 fixture：一份手填的能力报告、对着它的 `FakePlatform` 与共享目录模型。"""

from __future__ import annotations

import pytest

from flotilla.platform.base import Capabilities
from flotilla.platform.fake import FakePlatform, ManualClock, SharedTree


@pytest.fixture
def caps() -> Capabilities:
    """测试用能力声明：link 可用、exec 取地址、list 可见时限 30s。"""
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
