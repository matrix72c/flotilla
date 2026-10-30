"""Shared episode loop; mode (compat/perf) and arm are orthogonal.

compat: open -> every service answers a shell -> close. perf: additionally a 100 KiB upload,
20 execs in an 80/15/5 short/mid/heavy mix. Failures are classified coarsely from
FlotillaError stage/category; evidence strings ride along for later reclassification.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from flotilla import Environment, FlotillaError

from loadtest.events import EventLog

UPLOAD_PATH = "/tmp/loadtest-upload.bin"
UPLOAD_BYTES = 100 * 1024
EXEC_MIX = (("short", 0.80, "true"), ("mid", 0.15, "sleep 2; echo mid"), ("heavy", 0.05, "sleep 5; echo heavy"))


def classify_error(exc: BaseException) -> dict:
    if isinstance(exc, FlotillaError):
        return {
            "class": f"flotilla.{exc.category.value}",
            "stage": exc.stage,
            "retryable": exc.retryable,
            "message": str(exc)[:400],
        }
    return {"class": type(exc).__name__, "stage": None, "retryable": False, "message": str(exc)[:400]}


async def perf_phase(env: Environment, events: EventLog, task: str) -> None:
    payload = os.urandom(UPLOAD_BYTES)
    began = time.monotonic()
    await env.agent.write_file(UPLOAD_PATH, payload, mode=0o600)
    result = await env.agent.shell(f"wc -c < {UPLOAD_PATH}", timeout=30)
    events.emit(
        "upload",
        task,
        bytes=UPLOAD_BYTES,
        upload_s=round(time.monotonic() - began, 3),
        verified=result.exit_code == 0 and result.stdout.decode(errors="replace").strip() == str(UPLOAD_BYTES),
    )
    for index in range(20):
        draw = (index * 7919) % 100 / 100  # deterministic 80/15/5-ish mix without RNG state
        label, command = EXEC_MIX[-1][0], EXEC_MIX[-1][2]
        for name, share, cmd in EXEC_MIX:
            if draw < share:
                label, command = name, cmd
                break
            draw -= share
        began = time.monotonic()
        result = await env.agent.shell(command, timeout=120)
        events.emit(
            "exec",
            task,
            seq=index,
            mix=label,
            exec_s=round(time.monotonic() - began, 3),
            exit_code=result.exit_code,
            timed_out=bool(result.timed_out),
        )


async def run_episode(arm, task: Path, events: EventLog, *, mode: str = "compat") -> bool:
    task_name = str(task)
    request_id = f"ep-{uuid.uuid4().hex[:12]}"
    events.emit("episode_start", task_name, mode=mode, request_id=request_id)
    began = time.monotonic()
    try:
        async with arm.open(task, request_id) as env:
            info = await env.info()
            services = sorted(s.name for s in info.services)
            for name in services:
                result = await env.service(name).shell("true", timeout=60)
                if result.exit_code != 0 or result.timed_out:
                    raise RuntimeError(f"service {name} does not answer (exit={result.exit_code})")
            events.emit("services_ready", task_name, services=services)
            if mode == "perf":
                await perf_phase(env, events, task_name)
    except BaseException as exc:  # noqa: BLE001 - every failure is a datapoint, never aborts the driver
        events.emit(
            "episode_end",
            task_name,
            ok=False,
            elapsed_s=round(time.monotonic() - began, 3),
            error=classify_error(exc),
        )
        return False
    events.emit("episode_end", task_name, ok=True, elapsed_s=round(time.monotonic() - began, 3))
    return True
