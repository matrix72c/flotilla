"""`flotilla.platform.base` 的协议与值类型：fake 满足协议、枚举取值是对外契约。"""

from __future__ import annotations

from flotilla.platform.base import Clock, ErrorCategory, InstanceStatus, Platform
from flotilla.platform.fake import FakePlatform, ManualClock


def test_fake_platform_satisfies_protocol(platform: FakePlatform) -> None:
    assert isinstance(platform, Platform)


def test_manual_clock_satisfies_protocol(clock: ManualClock) -> None:
    assert isinstance(clock, Clock)


def test_error_categories_are_stable() -> None:
    # 类别值会进日志与 xtuner 的失败统计，改名即破坏对外契约。
    assert {c.value for c in ErrorCategory} == {
        "rate_limited",
        "capacity",
        "transient",
        "image",
        "invalid",
        "not_found",
        "lost",
        "service",
    }


def test_instance_status_values() -> None:
    assert {s.value for s in InstanceStatus} == {"pending", "running", "terminal", "unknown"}
