"""Bring-up compatibility driver (checkpointed, resumable, STOP-file aware).

Sweeps a task list with bounded concurrency through one arm. The event log is the
checkpoint: tasks with a terminal `episode_end` are skipped on resume; a crashed run's
half-open episodes are simply re-run (platform TTLs and the reaper reclaim their sandboxes).
Run the observer alongside; reconcile offline with report.py.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from loadtest.creds import env_path
from loadtest.events import EventLog, read_events
from loadtest.episode import run_episode
from loadtest.flotilla_arm import FlotillaArm

import loadtest.kata_gate  # noqa: F401  # execd readiness gate for kata instances


def load_tasks(
    registry: Path,
    datasets: list[str] | None,
    limit: int | None,
    shuffle: int | None,
    extra: list[Path] | None = None,
) -> list[Path]:
    import json

    entries = json.loads(registry.read_text())
    tasks: list[Path] = []
    for dataset in entries:
        if datasets and dataset["name"] not in datasets:
            continue
        tasks.extend(Path(t["path"]) for t in dataset["tasks"])
    for extra_file in extra or []:
        tasks.extend(Path(t["path"]) for t in json.loads(Path(extra_file).read_text()))
    if shuffle is not None:
        random.Random(shuffle).shuffle(tasks)
    if limit is not None:
        tasks = tasks[:limit]
    return tasks


def terminal_tasks(events_path: Path) -> set[str]:
    if not events_path.exists():
        return set()
    return {
        r["task"]
        for r in read_events(events_path)
        if r.get("kind") == "episode_end" and r.get("task")
    }


async def sweep(args: argparse.Namespace) -> int:
    tasks = load_tasks(args.registry, args.datasets, args.limit, args.shuffle, args.extra_tasks)
    done = terminal_tasks(args.events)
    pending = [t for t in tasks if str(t) not in done]
    run = f"compat-{time.strftime('%Y%m%d-%H%M%S')}"
    events = EventLog(args.events, run, args.arm)
    events.emit("sweep_start", total=len(tasks), already_done=len(done) - (len(tasks) - len(pending)),
                pending=len(pending), concurrency=args.concurrency, mode=args.mode)
    print(f"{len(tasks)} tasks, {len(pending)} pending (skip {len(tasks) - len(pending)} done)", flush=True)

    arm = FlotillaArm(args.deployment, args.secret, events)
    semaphore = asyncio.Semaphore(args.concurrency)
    stop = args.stop_file
    results = {"ok": 0, "fail": 0}

    async def worker(task: Path) -> None:
        if stop.exists():
            return
        async with semaphore:
            if stop.exists():
                return
            ok = await run_episode(arm, task, events, mode=args.mode)
            results["ok" if ok else "fail"] += 1
            total = results["ok"] + results["fail"]
            if total % 10 == 0 or total == len(pending):
                print(f"progress {total}/{len(pending)} ok={results['ok']} fail={results['fail']}", flush=True)

    try:
        await asyncio.gather(*(worker(t) for t in pending))
    finally:
        await arm.close()
        events.emit("sweep_end", **results)
        events.close()
    print(f"done ok={results['ok']} fail={results['fail']}", flush=True)
    return 0 if results["fail"] == 0 else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True, help="harbor registry.json")
    parser.add_argument("--deployment", type=Path, default=env_path("FLOTILLA_DEPLOYMENT"))
    parser.add_argument("--secret", type=Path, default=env_path("OPENSANDBOX_SECRET_FILE"))
    parser.add_argument("--events", type=Path, required=True, help="event JSONL (append; also the checkpoint)")
    parser.add_argument("--datasets", nargs="*", default=None, help="filter dataset names")
    parser.add_argument("--extra-tasks", nargs="*", default=None,
                        help="extra task-list JSON files ([{name, path}]), e.g. build/tb2-tasks.json")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--shuffle", type=int, default=None, help="seed; also enables shuffling")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--mode", choices=("compat", "perf"), default="compat")
    parser.add_argument("--arm", default="flotilla")
    parser.add_argument("--stop-file", type=Path, default=Path("/tmp/loadtest-stop"))
    args = parser.parse_args(argv)
    if args.deployment is None:
        parser.error("--deployment or FLOTILLA_DEPLOYMENT is required")
    if args.secret is None:
        parser.error("--secret or OPENSANDBOX_SECRET_FILE is required")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    return asyncio.run(sweep(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
