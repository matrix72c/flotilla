"""Neutral Harbor contract fixture; local files/processes with fake resource lifecycle.

Only our fixed test scripts run here. Path translation is not a sandbox and must
never be used to execute dataset or user-supplied tasks.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path

from flotilla.core import labels
from flotilla.platform.base import ExecResult, InstanceHandle, ProcessSpec
from tests.support.platform import FakePlatform


def attach_local_files(platform: FakePlatform, root: Path) -> None:
    original_exec = platform.exec_handler
    original_read = platform.read_file
    original_write = platform.write_file

    def local(iid: str, path: str) -> Path:
        base = root / iid
        target = base / path.lstrip("/")
        if not target.resolve().is_relative_to(base.resolve()):
            raise ValueError("test path escapes instance")
        return target

    async def read(handle: InstanceHandle, path: str) -> bytes:
        target = local(handle.iid, path)
        if target.is_file():
            return await asyncio.to_thread(target.read_bytes)
        return await original_read(handle, path)

    async def write(handle: InstanceHandle, path: str, data: bytes, *, mode: int, uid: int, gid: int) -> None:
        await original_write(handle, path, data, mode=mode, uid=uid, gid=gid)
        target = local(handle.iid, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)

    def execute(iid: str, proc: ProcessSpec) -> ExecResult:
        if platform.spec_of(iid).labels.get(labels.ROLE) == labels.ROLE_ANCHOR:
            return original_exec(iid, proc)
        base = root / iid
        base.mkdir(parents=True, exist_ok=True)
        # Commands are emitted by Flotilla/Harbor, never supplied by datasets.
        argv = list(proc.argv)
        if argv[0] == "/.flotilla/bin/busybox":
            argv = argv[1:]
            if argv[0] == "id":
                return ExecResult(0, b"0\n", b"")
            if argv[:2] != ["sh", "-c"]:
                argv = [str(local(iid, arg)) if arg.startswith("/") else arg for arg in argv]
        if argv[:2] in (["/bin/bash", "-c"], ["/bin/sh", "-c"], ["sh", "-c"]):
            command = argv[2]
            command = command.replace("/.flotilla/bin/busybox ", "")
            command = command.replace("chown 0:0", f"chown {os.getuid()}:{os.getgid()}")
            command = re.sub(
                r"/(?:logs|tests|evidence|tmp|run|etc)(?=/|\b)", lambda match: str(local(iid, match.group())), command
            )
            argv[2] = command
        cwd = local(iid, proc.cwd or "/")
        cwd.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, **proc.env}
        if "FIXTURE_REWARD_PATH" in env:
            env["FIXTURE_REWARD_PATH"] = str(local(iid, env["FIXTURE_REWARD_PATH"]))
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=5)
        return ExecResult(result.returncode, result.stdout, result.stderr)

    platform.read_file = read  # type: ignore[method-assign]
    platform.write_file = write  # type: ignore[method-assign]
    platform.set_exec_handler(execute)
