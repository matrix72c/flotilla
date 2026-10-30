"""Independent platform observer: quotas, active inventory, terminated history -> JSONL.

Run as its own process next to a loadtest driver. Every tick it records a snapshot
(`kind=snapshot`): project/member quotas, the active sandbox inventory split by state and
flotilla ownership (metadata carries `flotilla/launch`), and resource sums. Terminated
records (`kind=terminated`) are emitted once per instance id with their releaseReason.
Never talks to the driver: reconciliation happens offline in report.py against both logs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

from loadtest.creds import env_path, read_env_file_values

FLOTILLA_LAUNCH = "flotilla/launch"
SNAPSHOT = "snapshot"
TERMINATED = "terminated"

_UNITS = {"Ki": 1 / (1 << 20), "Mi": 1 / (1 << 10), "Gi": 1.0, "Ti": 1 << 10}


def _memory_gib(value: str) -> float:
    """Parse `2Gi` / `512Mi` / `1GiB` / raw bytes into GiB."""
    text = str(value or "0").strip().rstrip("Bb")
    for suffix, factor in _UNITS.items():
        if text.endswith(suffix):
            try:
                return float(text[: -len(suffix)]) * factor
            except ValueError:
                return 0.0
    try:
        return float(text) / (1 << 30)
    except ValueError:
        return 0.0


def log(message: str) -> None:
    print(message, flush=True)


class Observer:
    def __init__(self, client: httpx.Client, endpoint: str, project: str, out: Path):
        self._client = client
        self._endpoint = endpoint.rstrip("/")
        self._project = project
        self._out = out
        self._seen_terminated: set[str] = set()

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        response = self._client.get(f"{self._endpoint}{path}", params=params)
        response.raise_for_status()
        return response.json()

    def quotas(self) -> dict:
        return self._get("/v1/resource-quotas", {"project": self._project})

    def _pages(self, path: str, params: dict) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            batch = self._get(path, {**params, "page": page, "pageSize": 100})
            items.extend(batch.get("items", []))
            pagination = batch.get("pagination", {}) or {}
            if not pagination.get("hasNextPage"):
                return items
            page += 1

    def active(self) -> list[dict]:
        return self._pages("/v1/sandboxes", {"project": self._project})

    def terminated_new(self) -> list[dict]:
        records = self._pages("/v1/sandboxes/terminated", {"project": self._project})
        fresh = [r for r in records if r.get("id") not in self._seen_terminated]
        self._seen_terminated.update(r.get("id") for r in fresh)
        return fresh

    @staticmethod
    def summarize(items: list[dict]) -> dict:
        by_state: dict[str, int] = {}
        flotilla = 0
        cpu = memory = 0
        for item in items:
            by_state[item.get("state", "?")] = by_state.get(item.get("state", "?"), 0) + 1
            metadata = item.get("metadata") or {}
            if FLOTILLA_LAUNCH in metadata:
                flotilla += 1
            requests = item.get("resourceRequests") or {}
            cpu += float(requests.get("cpu", 0) or 0)
            memory += _memory_gib(requests.get("memory"))
        return {
            "total": len(items),
            "by_state": by_state,
            "flotilla_owned": flotilla,
            "other": len(items) - flotilla,
            "cpu_sum": round(cpu, 1),
            "memory_gi_sum": round(memory, 1),
        }

    def tick(self) -> dict:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        snapshot = {
            "kind": SNAPSHOT,
            "ts": ts,
            "quotas": self.quotas(),
            "active": self.summarize(self.active()),
        }
        with self._out.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(snapshot, separators=(",", ":"), default=str) + "\n")
            for record in self.terminated_new():
                slim = {
                    "kind": TERMINATED,
                    "ts": ts,
                    "id": record.get("id"),
                    "terminatedAt": record.get("terminatedAt"),
                    "releaseReason": record.get("releaseReason") or (record.get("status") or {}).get("reason"),
                    "metadata": {k: v for k, v in (record.get("metadata") or {}).items() if k.startswith("flotilla/")},
                    "createdAt": record.get("createdAt"),
                }
                stream.write(json.dumps(slim, separators=(",", ":"), default=str) + "\n")
        return snapshot

    def run(self, interval: float, duration: float | None, stop_file: Path | None) -> None:
        deadline = time.monotonic() + duration if duration else None
        while True:
            began = time.monotonic()
            try:
                snapshot = self.tick()
                log(f"tick {snapshot['ts']} active={snapshot['active']}")
            except Exception as exc:  # noqa: BLE001 - keep observing through API hiccups
                log(f"tick failed: {type(exc).__name__}: {exc}")
            if deadline and time.monotonic() > deadline:
                return
            if stop_file is not None and stop_file.exists():
                log("stop file present; observer exiting")
                return
            time.sleep(max(0.0, interval - (time.monotonic() - began)))


def parse_args(argv: list[str]) -> argparse.Namespace:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret", type=Path, default=root / ".env")
    parser.add_argument("--endpoint", default=os.environ.get("OPENSANDBOX_ENDPOINT"),
                        help="platform API base URL (or OPENSANDBOX_ENDPOINT)")
    parser.add_argument("--project", default=os.environ.get("OPENSANDBOX_PROJECT"),
                        help="workspace name (or OPENSANDBOX_PROJECT)")
    parser.add_argument("--out", type=Path, required=True, help="observer JSONL output")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--duration", type=float, default=None, help="seconds; omit to run until stop file")
    parser.add_argument("--stop-file", type=Path, default=None)
    args = parser.parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    values = read_env_file_values(args.secret, ("OPEN_SANDBOX_ACCESS_KEY", "OPEN_SANDBOX_SECRET_KEY"))
    credential = values["OPEN_SANDBOX_ACCESS_KEY"] + ":" + values["OPEN_SANDBOX_SECRET_KEY"]
    client = httpx.Client(headers={"OPEN-SANDBOX-API-KEY": credential}, timeout=60)
    Observer(client, args.endpoint, args.project, args.out).run(args.interval, args.duration, args.stop_file)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
