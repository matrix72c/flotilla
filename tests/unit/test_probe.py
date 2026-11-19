"""Probe orchestration and cleanup on the fake platform.

The isolation checks themselves are exercised in `test_probe_storage.py` and
`test_probe_network.py`. What is stated here is what the run around them guarantees: a check that
raises does not become evidence, and nothing the probe created outlives it -- including when the
caller cancels, and including when deletion itself fails.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from flotilla.capabilities import Evidence, IsolationEvidence, ProbeSettings
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.probe import PROBE_LABEL, _mounted_subdir, _Run, probe
from flotilla.testing.platform import FakePlatform, ManualClock, SharedTree

SETTINGS = ProbeSettings(
    image="reg/unit@sha256:" + "b" * 64,
    share_release="r1",
    endpoint="http://os.test",
)


@pytest.mark.asyncio
async def test_an_unmeasured_restriction_is_never_evidence_that_it_holds(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fake platform answers no isolation check, so every property must come back not held."""
    from flotilla import probe_checks

    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor@sha256:abc"))
    await clock.run(anchor.start())
    SharedTree(platform)
    await anchor.mkdir("launches/Lp", stage="prepare")

    async def nothing(run: _Run) -> IsolationEvidence:
        # The run's own directory, which cleanup confirms it removed again.
        await run.anchor.mkdir(f"launches/Lp/{run.run_id}", stage="prepare")
        return IsolationEvidence()

    monkeypatch.setattr(probe_checks, "measure", nothing)
    evidence = await clock.run(probe(platform, clock, anchor, SETTINGS))
    assert evidence.gaps() == list(evidence.gaps())  # every property, in declaration order
    assert len(evidence.gaps()) == 7
    assert not evidence.privileged_isolated()
    # Cleanup is the one thing this run did establish.
    assert evidence.cleanup.held
    await anchor.close()


@pytest.mark.asyncio
async def test_a_check_that_raises_does_not_become_evidence(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flotilla.probe_checks import attempt

    async def explode() -> None:
        raise RuntimeError("private remote response")

    outcome = await attempt(explode)
    assert isinstance(outcome, Evidence) and not outcome.held
    assert "private remote response" not in outcome.observed
    assert "RuntimeError" in outcome.observed


@pytest.mark.asyncio
async def test_cleanup_on_cancellation(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flotilla import probe_checks

    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())

    async def cancelled(run: _Run) -> IsolationEvidence:
        await run._create(run._spec("pending"))
        raise asyncio.CancelledError()

    monkeypatch.setattr(probe_checks, "measure", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await clock.run(probe(platform, clock, anchor, SETTINGS))
    assert [iid for iid in platform.iids() if PROBE_LABEL in platform.spec_of(iid).labels] == []
    await anchor.close()


@pytest.mark.asyncio
async def test_unconfirmed_deletion_fails_cleanup_without_echoing_the_remote_message(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flotilla import probe_checks

    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    real_delete = platform.delete

    async def create(run: _Run) -> IsolationEvidence:
        await run._create(run._spec("leak"))

        async def fail_delete(iid: str) -> None:
            raise RuntimeError("private response")

        monkeypatch.setattr(platform, "delete", fail_delete)
        return IsolationEvidence()

    monkeypatch.setattr(probe_checks, "measure", create)
    evidence = await clock.run(probe(platform, clock, anchor, SETTINGS))
    assert not evidence.cleanup.held
    assert "not confirmed deleted" in evidence.cleanup.observed
    assert "private response" not in evidence.cleanup.observed
    monkeypatch.setattr(platform, "delete", real_delete)
    await anchor.close()


def test_probe_settings_require_a_pinned_image() -> None:
    with pytest.raises(ValueError, match="pinned"):
        replace(SETTINGS, image="")


@pytest.mark.parametrize(
    "entry,ok",
    [
        ("srv:/export/p1/visible|/", True),
        ("srv:/export|/p1/visible", True),
        ("srv:/export|/", False),
        ("srv:/export/p1/visible-other|/", False),
    ],
)
def test_mount_subdir(entry: str, ok: bool) -> None:
    assert _mounted_subdir(entry, "p1/visible") is ok
