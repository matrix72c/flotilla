#!/usr/bin/env python3
"""OpenSandbox peer-policy and hosts connectivity check; Python 3.9+, standard library only.

Supply --endpoint and --image; credentials come from OPEN_SANDBOX_API_KEY or a hidden prompt.
Creates two 0.5 CPU / 512 MiB instances, TTL 600s; deletes them in finally.
Exit 0: all checks pass; 1: connectivity differs from expectation; 2: setup/cleanup error.
"""

import argparse
import getpass
import ipaddress
import json
import os
import secrets
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="Server root URL, without /v1")
    parser.add_argument("--project", help="Optional deployment-specific project extension")
    parser.add_argument("--link", choices=("cidr", "sandbox"), default="cidr")
    parser.add_argument(
        "--same-cluster", action="store_true", help="For deployments supporting sandboxGroup: send one shared UUID"
    )
    parser.add_argument("--image", required=True, help="Must contain sh, env, sleep, ip, ss, getent, netcat-openbsd")
    parser.add_argument("--command-format", choices=("command", "argv"), default="command")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    key = os.environ.get("OPEN_SANDBOX_API_KEY") or getpass.getpass("OpenSandbox API key (hidden): ")
    if not key:
        parser.error("Set OPEN_SANDBOX_API_KEY (never pass credentials as a command-line argument).")
    run = "cidr-hosts-" + secrets.token_hex(6)
    group = str(uuid.uuid4()) if args.same_cluster else None
    output = args.output or Path(run + ".json")
    if output.exists():
        parser.error("Output already exists; choose another --output.")
    tokens, instances, ips = {}, {}, {}
    report = {
        "run": run,
        "endpoint": args.endpoint,
        "image": args.image,
        "project": args.project,
        "link": args.link,
        "sandbox_group": group,
        "instances": instances,
        "ips": ips,
        "http": [],
        "commands": [],
        "checks": [],
        "cleanup": {},
    }
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def save():
        text = json.dumps(report, indent=2)
        for value in (key, *tokens.values()):
            text = text.replace(value, "[REDACTED]")
        output.write_text(text + "\n")

    def request(method, path, body=None, token=None, allowed=(200, 201, 202, 204)):
        headers = {"OPEN-SANDBOX-API-KEY": key, "Content-Type": "application/json"}
        if token:
            headers["X-EXECD-ACCESS-TOKEN"] = token
        req = urllib.request.Request(
            args.endpoint.rstrip("/") + "/v1" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            response = opener.open(req, timeout=30)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            status, raw = response.status, response.read().decode()
            record = {
                "method": method,
                "path": path,
                "status": status,
                "request_id": response.headers.get("x-request-id"),
                "time": time.time(),
            }
        if path.endswith("/networkpolicy"):
            record.update(request=body, response=json.loads(raw) if raw else None)
        report["http"].append(record)  # Do not log create bodies/responses or credential headers.
        save()
        if status not in allowed:
            raise RuntimeError(f"{method} {path}: HTTP {status}")
        return status, raw

    def api(method, path, body=None):
        _, raw = request(method, path, body)
        return json.loads(raw) if raw else None

    def execute(name, command, background=False):
        argv = ["/usr/bin/env", "-i", "PATH=/usr/sbin:/usr/bin:/sbin:/bin", "/bin/sh", "-c", command]
        body = {"cwd": "/", "uid": 0, "gid": 0, "background": background}
        body[args.command_format] = argv if args.command_format == "argv" else shlex.join(argv)
        if not background:
            body["timeout"] = 10000  # execd uses milliseconds.
        _, raw = request("POST", f"/sandboxes/{instances[name]}/proxy/44772/command", body, tokens[name])
        events = [
            json.loads(line.removeprefix("data:").strip())
            for line in raw.splitlines()
            if line.strip() and not line.startswith(":")
        ]
        result = {"name": name, "command": command, "background": background, "events": events}
        report["commands"].append(result)
        save()
        stdout = "\n".join(str(e.get("text", "")) for e in events if e.get("type") == "stdout")
        ok = not any(e.get("type") == "error" for e in events) and any(
            e.get("type") == ("init" if background else "execution_complete") for e in events
        )
        return ok, stdout

    def require(name, command, background=False):
        ok, stdout = execute(name, command, background)
        if not ok:
            raise RuntimeError(f"{name}: command failed; see report commands")
        return stdout

    def http_command(host):
        payload = f"GET / HTTP/1.0\r\nHost: {host}\r\n\r\n"
        return "printf %s " + shlex.quote(payload) + " | nc -v -w 3 " + shlex.quote(host) + " 48080"

    def owned_ids():
        query = urllib.parse.urlencode({"metadata": urllib.parse.urlencode({"repro": run}), "pageSize": 200})
        return {
            s["id"]
            for s in api("GET", "/sandboxes?" + query).get("items", [])
            if s.get("metadata", {}).get("repro") == run
        }

    save()
    print(f"Report: {output.resolve()}", flush=True)
    try:
        for name in ("a", "b"):
            tokens[name] = secrets.token_urlsafe(24)
            created = api(
                "POST",
                "/sandboxes",
                {
                    "image": {"uri": args.image},
                    "entrypoint": ["/bin/sleep", "600"],
                    "timeout": 600,
                    "resourceRequests": {"cpu": "0.5", "memory": "512Mi"},
                    "resourceLimits": {"cpu": "0.5", "memory": "512Mi"},
                    "env": {"EXECD_ACCESS_TOKEN": tokens[name]},
                    "metadata": {"repro": run, "unit": name},
                    "extensions": {
                        **({"project": args.project} if args.project else {}),
                        **({"sandboxGroup": group} if group else {}),
                    },
                    "ports": [{"containerPort": 44772, "protocol": "tcp", "name": "execd"}],
                    "networkPolicy": {"defaultAction": "deny", "egress": []},
                },
            )
            instances[name] = created["id"]
            save()
            print(f"Created {name}: {instances[name]}", flush=True)
        for name, iid in instances.items():
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                info = api("GET", f"/sandboxes/{iid}")
                state = str((info.get("status") or {}).get("state") or info.get("state", "")).lower()
                if state == "running":
                    report.setdefault("placement", {})[name] = {
                        "backend": info.get("backendName"),
                        "cluster": (info.get("extensions") or {}).get("clusterID"),
                    }
                    save()
                    break
                if state in ("failed", "terminated", "stopping"):
                    raise RuntimeError(f"{name}: {state}")
                time.sleep(2)
            else:
                raise RuntimeError(f"{name}: startup timeout")
            route = require(name, "ip -4 -o route show default").split()
            dev = route[route.index("dev") + 1]
            address = require(name, "ip -4 -o addr show dev " + shlex.quote(dev)).split()
            ips[name] = str(ipaddress.ip_interface(address[address.index("inet") + 1]).ip)
            require(name, "command -v nc && command -v getent && command -v ss")
            marker = f"{run}-{name}"
            response = f"HTTP/1.0 200 OK\r\nContent-Length: {len(marker)}\r\nConnection: close\r\n\r\n{marker}"
            require(name, "while true; do printf %s " + shlex.quote(response) + " | nc -l -q 0 48080; done", True)
            time.sleep(1)
            if marker not in require(name, http_command("127.0.0.1")):
                raise RuntimeError(f"{name}: localhost response missing marker")
            require(name, "ss -lnt")
        if args.same_cluster:
            clusters = {p["cluster"] for p in report["placement"].values()}
            if len(clusters) != 1 or None in clusters:
                raise RuntimeError("same-cluster scheduling could not be confirmed from instance details")
        for name, peer in (("a", "b"), ("b", "a")):
            entry = f"\n{ips[peer]} peer-{peer}.flotilla.test\n"
            require(name, "printf %s " + shlex.quote(entry) + " >> /etc/hosts")
            resolved = require(name, f"getent ahostsv4 peer-{peer}.flotilla.test")
            if {line.split()[0] for line in resolved.splitlines() if line.strip()} != {ips[peer]}:
                raise RuntimeError(f"{name}: hosts resolution mismatch")
        allow_phase = f"{args.link}_allow"
        for phase in ("initial_deny", allow_phase, "revoked"):
            for name, peer in (("a", "b"), ("b", "a")):
                path = f"/sandboxes/{instances[name]}/networkpolicy"
                if phase != "initial_deny":
                    target = f"sandbox:{instances[peer]}" if args.link == "sandbox" else ips[peer] + "/32"
                    rules = [{"action": "allow", "target": target}] if phase == allow_phase else []
                    api("PUT", path, {"defaultAction": "deny", "egress": rules})
                api("GET", path)
            time.sleep(5)
            for name, peer in (("a", "b"), ("b", "a")):
                for host in (ips[peer], f"peer-{peer}.flotilla.test"):
                    for attempt in range(1, 4 if phase == allow_phase else 2):
                        ok, stdout = execute(name, http_command(host))
                        reachable = ok and "HTTP/1.0 200 OK" in stdout and f"{run}-{peer}" in stdout
                        report["checks"].append(
                            {"phase": phase, "source": name, "target": host, "attempt": attempt, "reachable": reachable}
                        )
                        save()
                        print(f"{phase}: {name} -> {host}: {'OK' if reachable else 'FAIL'}", flush=True)
                        if reachable or phase != allow_phase:
                            break
                        time.sleep(2)
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        ids = set(instances.values())
        try:
            ids |= owned_ids()  # Also recover a create whose response was lost; never delete other runs.
        except Exception as exc:
            report["reconcile_error"] = type(exc).__name__
        for iid in sorted(ids):
            try:
                request("DELETE", f"/sandboxes/{iid}", allowed=(200, 202, 204, 404))
                for _ in range(30):
                    status, _ = request("GET", f"/sandboxes/{iid}", allowed=(200, 404))
                    if status == 404:
                        break
                    time.sleep(1)
                report["cleanup"][iid] = "not_found" if status == 404 else "still_visible"
            except Exception as exc:
                report["cleanup"][iid] = type(exc).__name__
        try:
            report["remaining"] = sorted(owned_ids())
        except Exception as exc:
            report["reconcile_error"] = type(exc).__name__
        save()
    if (
        report.get("error")
        or report.get("reconcile_error")
        or report.get("remaining")
        or any(v != "not_found" for v in report["cleanup"].values())
    ):
        print("Setup/cleanup error; inspect report.")
        return 2
    allowed = [c for c in report["checks"] if c["phase"] == allow_phase]
    passed = len({(c["source"], c["target"]) for c in allowed if c["reachable"]}) == 4 and all(
        not c["reachable"] for c in report["checks"] if c["phase"] != allow_phase
    )
    print(f"Connectivity checks {'PASS' if passed else 'FAIL'}; temporary instances deleted.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
