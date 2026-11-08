"""Storage checks retain independent outcomes and inspect procfs through exec."""

from __future__ import annotations

import pytest

from flotilla.capabilities import Item, ProbeSettings
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.platform.base import ErrorCategory, ExecResult, FlotillaError, InstanceHandle, ProcessSpec
from flotilla.probe import _Run
from flotilla.probe_checks import _mountinfo, attempt, storage_extra
from flotilla.testing.platform import FakePlatform, ManualClock, SharedTree

SETTINGS = ProbeSettings(image="probe", share_release="r1", endpoint="http://os.test", evidence_profile="test")


@pytest.mark.asyncio
async def test_attempt_retains_structured_error_without_remote_message() -> None:
    async def broken() -> Item:
        raise FlotillaError("HTTP 503 secret output", stage="run", category=ErrorCategory.TRANSIENT, retryable=True)

    result = await attempt(broken)
    assert result.status == "error"
    assert result.measurements["stage"] == "run"
    assert result.measurements["category"] == "transient"
    assert result.measurements["retryable"] is True
    assert result.measurements["http_status"] == 503
    assert result.measurements["frames"][-1]["function"] == "broken"
    assert result.measurements["frames"][-1]["file"] == "test_probe_storage.py"
    assert "secret output" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [None, "shared_volume", "volume_file", "no_auto_mount", "mountinfo"])
async def test_storage_checks_preserve_independent_results(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch, broken: str | None
) -> None:
    from flotilla import probe_checks

    SharedTree(platform)
    anchor = Anchor(platform, clock, "storage-test", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    run = _Run(platform, clock, anchor, SETTINGS, "p1")
    original_exec = platform.exec_handler
    base = "launches/storage-test/p1/shared-ordinary/visible"
    seen: list[str] = []

    def fail() -> None:
        raise FlotillaError("secret output", stage="run", category=ErrorCategory.TRANSIENT, retryable=True)

    async def read_file(_unit: InstanceHandle, path: str) -> bytes:
        assert path != "/proc/self/mountinfo", "procfs must not use the download API"
        if broken == "shared_volume" and path == "/probe/from-a":
            fail()
        if broken == "volume_file" and path == "/probe-file":
            fail()
        return b"p1b" if path.endswith("from-b") else b"p1"

    def execute(iid: str, proc: ProcessSpec) -> ExecResult:
        if iid == anchor.iid:
            return original_exec(iid, proc)
        if proc.argv[-1] == "cat /proc/self/mountinfo":
            seen.append("mountinfo")
            if broken == "mountinfo":
                return ExecResult(1, b"", b"unavailable")
            return ExecResult(0, f"1 0 0:1 / /probe ro - nfs srv:/{base} ro\n".encode(), b"")
        return ExecResult(0, b"", b"")

    async def no_mount(*args: object, **kwargs: object) -> Item:
        seen.append("no_auto_mount")
        if broken == "no_auto_mount":
            fail()
        return Item(status="passed", evidence="independent test observation")

    platform.set_exec_handler(execute)
    monkeypatch.setattr(platform, "read_file", read_file)
    monkeypatch.setattr(probe_checks, "no_auto_mount", no_mount)
    try:
        results = await clock.run(storage_extra(run))
        for key in ("shared_volume", "volume_file", "no_auto_mount"):
            assert results[key].status == ("error" if broken == key else "passed")
        assert results["read_only"].ok
        assert results["subdir_isolation"].status == ("error" if broken == "mountinfo" else "passed")
        assert results["missing_subpath"].status == "failed"  # fake accepts absent paths; never fabricate rejection
        assert seen == ["no_auto_mount", "mountinfo"]
        assert all("secret output" not in result.model_dump_json() for result in results.values())
    finally:
        assert await clock.run(run.cleanup()) == []
        await anchor.close()
    assert not platform.iids()


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code,timed_out", [(1, False), (0, True)])
async def test_mountinfo_rejects_failed_command(
    platform: FakePlatform, clock: ManualClock, exit_code: int, timed_out: bool
) -> None:
    anchor = Anchor(platform, clock, "test", AnchorSettings(image="anchor"))
    run = _Run(platform, clock, anchor, SETTINGS, "p1")
    platform.set_exec_handler(lambda *_: ExecResult(exit_code, b"partial", b"", timed_out=timed_out))
    unit = await clock.run(run._create(run._spec("mounted")))
    with pytest.raises(ValueError, match="mount table inspection"):
        await _mountinfo(run, unit)
    await clock.run(run._delete(unit.iid))
