"""The storage isolation checks: read-only, subdirectory confinement, and nothing mounted uninvited."""

from __future__ import annotations

import pytest

from flotilla.capabilities import Evidence, ProbeSettings
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.platform.base import ErrorCategory, ExecResult, FlotillaError, ProcessSpec
from flotilla.probe import _Run
from flotilla.probe_checks import _mountinfo, attempt, storage
from flotilla.testing.platform import FakePlatform, ManualClock, SharedTree

SETTINGS = ProbeSettings(image="probe@sha256:" + "a" * 64, share_release="r1", endpoint="http://os.test")


@pytest.mark.asyncio
async def test_a_failed_check_keeps_its_diagnosis_without_the_remote_message() -> None:
    async def broken() -> Evidence:
        raise FlotillaError("HTTP 503 secret output", stage="run", category=ErrorCategory.TRANSIENT, retryable=True)

    result = await attempt(broken)
    assert not result.held
    assert "stage" in result.observed and "run" in result.observed
    assert "transient" in result.observed
    assert "503" in result.observed
    assert "test_probe_storage.py" in result.observed
    assert "secret output" not in result.observed


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [None, "write-allowed", "sibling-visible", "mount-root", "no_auto_mount"])
async def test_each_storage_restriction_stands_or_falls_on_its_own(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch, broken: str | None
) -> None:
    """One restriction failing must not take the others' evidence with it."""
    from flotilla import probe_checks

    SharedTree(platform)
    anchor = Anchor(platform, clock, "storage-test", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    run = _Run(platform, clock, anchor, SETTINGS, "p1")
    original_exec = platform.exec_handler
    base = "launches/storage-test/p1/shared-ordinary/visible"
    seen: list[str] = []

    def execute(iid: str, proc: ProcessSpec) -> ExecResult:
        if iid == anchor.iid:
            return original_exec(iid, proc)
        script = proc.argv[-1]
        if script == "cat /proc/self/mountinfo":
            seen.append("mountinfo")
            # A deployment that mounts the shared root instead of the requested subdirectory.
            source = "srv:/" if broken == "mount-root" else f"srv:/{base}"
            return ExecResult(0, f"1 0 0:1 / /probe ro - nfs {source} ro\n".encode(), b"")
        if "touch /probe/forbidden" in script:
            seen.append("read-only")
            # The script succeeds only when the write is rejected and no sibling is visible.
            return ExecResult(1 if broken in ("write-allowed", "sibling-visible") else 0, b"", b"")
        return ExecResult(0, b"", b"")

    async def no_mount(*args: object, **kwargs: object) -> Evidence:
        seen.append("no_auto_mount")
        if broken == "no_auto_mount":
            raise FlotillaError("secret output", stage="run", category=ErrorCategory.TRANSIENT, retryable=True)
        return Evidence(True, "independent test observation")

    platform.set_exec_handler(execute)
    monkeypatch.setattr(probe_checks, "no_auto_mount", no_mount)
    try:
        results = await clock.run(storage(run))
        assert results["no_auto_mount"].held is (broken != "no_auto_mount")
        assert results["read_only"].held is (broken not in ("write-allowed", "sibling-visible"))
        assert results["subdir_isolation"].held is (broken != "mount-root")
        assert "secret output" not in str(results)
        assert seen == ["no_auto_mount", "read-only", "mountinfo"]
    finally:
        assert await clock.run(run.cleanup()) == []
        await anchor.close()
    assert not platform.iids()


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code,timed_out", [(1, False), (0, True)])
async def test_mountinfo_rejects_failed_command(
    platform: FakePlatform, clock: ManualClock, exit_code: int, timed_out: bool
) -> None:
    """A partial mount table must not be read as "nothing extra was mounted"."""
    anchor = Anchor(platform, clock, "test", AnchorSettings(image="anchor"))
    run = _Run(platform, clock, anchor, SETTINGS, "p1")
    platform.set_exec_handler(lambda *_: ExecResult(exit_code, b"partial", b"", timed_out=timed_out))
    unit = await clock.run(run._create(run._spec("mounted")))
    with pytest.raises(ValueError, match="mount table inspection"):
        await _mountinfo(run, unit)
    await clock.run(run._delete(unit.iid))
