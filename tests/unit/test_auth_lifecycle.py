"""Authentication rejection must not become an unbounded lifecycle retry."""

from __future__ import annotations

import pytest

from flotilla import Flotilla
from flotilla.config import FlotillaConfig
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.core.deletion import Deleter
from flotilla.core.waiting import wait_running
from flotilla.platform.base import ErrorCategory, FlotillaError
from flotilla.testing.platform import FakePlatform, ManualClock, SharedTree
from flotilla.testing.runtime import deployment as deployment


def denied() -> FlotillaError:
    return FlotillaError("HTTP 401", stage="create", category=ErrorCategory.INVALID, retryable=False)


@pytest.mark.asyncio
async def test_rejected_cleanup_is_immediate_and_unconfirmed(platform: FakePlatform, clock: ManualClock) -> None:
    deleter = Deleter(platform, clock)
    platform.inject_fault("delete", denied())
    assert not await deleter.delete("owned")
    platform.inject_fault("list", denied())
    assert not await deleter.scope().sweep({"launch": "owned"}, until=30)
    assert clock.monotonic() == 0


@pytest.mark.asyncio
async def test_rejected_readiness_does_not_wait_for_startup_timeout(platform: FakePlatform, clock: ManualClock) -> None:
    platform.inject_fault("get", denied())
    with pytest.raises(FlotillaError) as error:
        await wait_running(platform, clock, "owned", stage="prepare")
    assert error.value.category is ErrorCategory.INVALID
    assert error.value.stage == "prepare"
    assert clock.monotonic() == 0


@pytest.mark.asyncio
async def test_rejected_anchor_listing_releases_created_anchor(platform: FakePlatform, clock: ManualClock) -> None:
    platform.inject_fault("list", denied())
    anchor = Anchor(platform, clock, "owned", AnchorSettings(image="anchor@sha256:abc"))
    with pytest.raises(FlotillaError, match="HTTP 401"):
        await clock.run(anchor.start())
    assert not platform.instances()
    assert clock.monotonic() < platform.caps.list_visibility_s


@pytest.mark.asyncio
async def test_anchor_timeout_preserves_uncertain_create(platform: FakePlatform, clock: ManualClock) -> None:
    platform.set_latency("create", 10, phase="after")
    anchor = Anchor(platform, clock, "owned", AnchorSettings(image="anchor@sha256:abc", start_timeout_s=1))
    with pytest.raises(FlotillaError):
        await clock.run(anchor.start())
    assert anchor.creation_uncertain
    assert anchor.iid is None
    assert len(platform.instances()) == 1


@pytest.mark.asyncio
async def test_public_rejected_create_needs_no_reconciliation(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, platform, _ = deployment
    platform.inject_fault("create", denied())

    async def unexpected_list(query: object) -> None:
        pytest.fail("A definitively rejected create must not be reconciled")

    monkeypatch.setattr(platform, "list", unexpected_list)
    fleet = Flotilla(deployment=cfg)
    with pytest.raises(FlotillaError, match="HTTP 401"):
        await fleet.start()
    assert (await fleet.close()).clean
    assert not platform.instances()


@pytest.mark.asyncio
async def test_public_lost_create_response_still_requires_reconciliation(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
) -> None:
    cfg, platform, _ = deployment
    cfg = cfg.model_copy(update={"timeouts": cfg.timeouts.model_copy(update={"close": 5})})
    platform.inject_create_then_fail(
        FlotillaError("response lost", stage="create", category=ErrorCategory.TRANSIENT, retryable=True)
    )
    fleet = Flotilla(deployment=cfg)
    with pytest.raises(FlotillaError, match="response lost"):
        await fleet.start()
    assert (await fleet.close()).clean
    assert not platform.instances()
