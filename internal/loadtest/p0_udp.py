"""P0 calibration: UDP datagram between two linked sandboxes, via flotilla.

The link_udp capability may be "not revalidated" after platform updates. This probe opens
a UDP echo task (see internal/loadtest/tasks/p0-udp), sends one datagram from main to
`udptarget:5353`, and expects the payload echoed back. Success flips the capability with
evidence; failure restores it and records the error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loadtest.kata_gate  # noqa: F401  # execd readiness gate for kata instances

from loadtest.creds import bind_credential, env_path
from loadtest.events import EventLog
from loadtest.flotilla_arm import FlotillaArm

SEND = (
    "python3 -c \"import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM);"
    " s.settimeout(5); s.sendto(b'ping', ('udptarget', 5353)); print(s.recv(64).decode())\""
)


def flip(cap_path: Path, ok: bool, evidence: str) -> Path | None:
    data = json.loads(cap_path.read_text())
    item = data["network"]["link_udp"]
    backup = None
    if ok and not item.get("ok"):
        backup = cap_path.with_name(cap_path.name + ".bak-udp")
        if not backup.exists():
            backup.write_text(cap_path.read_text())
    item["ok"] = ok
    item["source"] = "probed"
    item["evidence"] = evidence
    cap_path.write_text(json.dumps(data, indent=2) + "\n")
    return backup


async def main(args: argparse.Namespace) -> int:
    out_dir = args.out.parent
    events = EventLog(out_dir / "udp-events.jsonl", "p0-udp", "flotilla")
    arm = FlotillaArm(args.deployment, args.secret, events)
    cap_path = Path(arm.config.capabilities_path())
    backup = flip(cap_path, True, f"P0 UDP validation in progress {time.strftime('%H:%M:%S')}")
    passed = False
    out: dict = {}
    try:
        async with arm.open(args.task, "p0-udp") as env:
            result = await env.agent.shell(SEND, timeout=60)
            output = (result.stdout + result.stderr).decode(errors="replace").strip()
            print(f"udp probe exit={result.exit_code} out={output!r}", flush=True)
            out["reply"] = output
            passed = result.exit_code == 0 and output == "udp-ok:ping"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        print(out["error"], flush=True)
    finally:
        await arm.close()
        events.close()
        if passed:
            flip(cap_path, True, f"P0 validated {time.strftime('%Y-%m-%d')}: UDP datagram "
                                 f"main->udptarget:5353 round-trips across the sandbox link.")
        elif backup is not None and backup.exists():
            cap_path.write_text(backup.read_text())
            print("capability restored", flush=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"passed": passed, **out}, indent=1))
    print(f"RESULT passed={passed}", flush=True)
    return 0 if passed else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, default=env_path("FLOTILLA_DEPLOYMENT"))
    parser.add_argument("--secret", type=Path, default=env_path("OPENSANDBOX_SECRET_FILE"))
    parser.add_argument("--task", type=Path, required=True, help="UDP probe task dir (tasks/p0-udp)")
    parser.add_argument("--out", type=Path, default=Path("build") / "p0-udp-result.json")
    args = parser.parse_args(argv)
    if args.deployment is None:
        parser.error("--deployment or FLOTILLA_DEPLOYMENT is required")
    if args.secret is None:
        parser.error("--secret or OPENSANDBOX_SECRET_FILE is required")
    return args


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args(sys.argv[1:]))))
