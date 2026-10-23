"""Opt-in real deployment contract for readonly rootfs, tmpfs, transfers and cleanup."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import yaml

from flotilla import EnvironmentDefinition, Flotilla, OpenOptions
from flotilla.config import FlotillaConfig
from flotilla.core import labels
from flotilla.core.anchor import Anchor
from flotilla.core.deletion import Deleter
from flotilla.platform.base import FlotillaError
from flotilla.platform.clock import SystemClock
from flotilla.platform.opensandbox import build

CASES = {
    "readonly": (True, False, 0),
    "tmpfs": (False, True, 0),
    "combined": (True, True, 0),
    "nonroot": (True, True, 1000),
}


async def run(deployment: Path, image: str, output: Path, *, cases: tuple[str, ...] = tuple(CASES)) -> bool:
    if not cases or any(case not in CASES for case in cases):
        raise ValueError("contract requires at least one known case")
    cfg = FlotillaConfig.load(deployment)
    caps = cfg.platform_capabilities()
    needs_readonly = any(CASES[case][0] for case in cases)
    needs_tmpfs = any(CASES[case][1] for case in cases)
    if (needs_readonly and not caps.readonly_rootfs) or (needs_tmpfs and not caps.native_tmpfs):
        raise ValueError("contract requires matching successful probes for the requested filesystem capabilities")
    result: dict[str, Any] = {
        "protocol": cfg.opensandbox.filesystem_protocol,
        "cases": cases,
        "checks": [],
        "clean": False,
    }
    output.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        output.write_text(json.dumps(result, indent=2) + "\n")

    def check(name: str, passed: bool, *, fatal: bool = True) -> None:
        result["checks"].append({"name": name, "passed": passed})
        save()
        print(f"{name}: {passed}", flush=True)
        if not passed and fatal:
            raise AssertionError(name)

    observer = build(
        endpoint=cfg.opensandbox.endpoint,
        credential=cfg.credential(),
        clock=SystemClock(),
        caps=caps,
        settings=cfg.network_settings(),
        storage=cfg.storage_settings(),
        execd=cfg.execd_settings(),
        filesystem=cfg.filesystem_settings(),
        extensions=cfg.opensandbox.extensions,
        create_fields=cfg.opensandbox.create_fields,
        group_extension=cfg.opensandbox.group_extension,
    )
    fleet = Flotilla(deployment=cfg)
    try:
        with TemporaryDirectory(prefix="flotilla-filesystem-contract-") as folder:
            root = Path(folder)
            result["local_temporary_directory"] = folder
            source = root / "source"
            source.mkdir()
            (source / "payload").write_bytes(bytes(range(256)) * 32)
            for readonly, tmpfs, uid in (CASES[case] for case in cases):
                name = f"ro-{int(readonly)}-tmpfs-{int(tmpfs)}"
                if uid:
                    name += f"-uid-{uid}"
                directory = root / name
                directory.mkdir()
                service: dict[str, Any] = {
                    "image": image,
                    "user": f"{uid}:{1001 if uid else 0}",
                    "read_only": readonly,
                    "entrypoint": [
                        "/bin/sh",
                        "-c",
                        "echo filesystem-contract; echo filesystem-contract-stderr >&2; exec sleep 2147483647",
                    ],
                    "command": [],
                    "mem_limit": "512m",
                    "volumes": ["results:/results"],
                }
                if tmpfs:
                    service["tmpfs"] = ["/tmp:size=1m,mode=0750,uid=1000,gid=1001"]
                compose = {"services": {"main": service}, "volumes": {"results": {}}}
                (directory / "docker-compose.yaml").write_text(yaml.safe_dump(compose))
                definition = EnvironmentDefinition(directory, directory, name)
                async with fleet.environment(definition, OpenOptions(startup_timeout=600)) as env:
                    info = await env.info()
                    state = await observer.get(info.services[0].instance_id)
                    result.setdefault("environments", []).append(
                        {
                            "case": name,
                            **asdict(info),
                            "effective_filesystem": asdict(state.filesystem) if state.filesystem is not None else None,
                        }
                    )
                    save()
                    agent = env.agent
                    if uid:
                        # Empty named volumes start root-owned. Prepare the fixture's
                        # writable directory explicitly; runtime scratch needs no fixup.
                        prepared = await agent.shell(f"chown {uid}:1001 /results", uid=0, gid=0)
                        check(
                            f"{name}/non-root-volume-permissions",
                            prepared.exit_code == 0 and (await agent.shell("test -w /results")).exit_code == 0,
                        )
                    if readonly:
                        write = await agent.shell("touch /.flotilla-contract-write", uid=0, gid=0, env={"LC_ALL": "C"})
                        check(f"{name}/root-EROFS", write.exit_code != 0 and b"Read-only file system" in write.stderr)
                        try:
                            await agent.write_file("/.flotilla-upload", b"must fail")
                        except FlotillaError:
                            check(f"{name}/upload-rejected", True)
                        else:
                            check(f"{name}/upload-rejected", False)
                        if not tmpfs:
                            write = await agent.shell("touch /tmp/flotilla-contract-write", env={"LC_ALL": "C"})
                            check(
                                f"{name}/tmp-not-opened",
                                write.exit_code != 0 and b"Read-only file system" in write.stderr,
                            )
                    await agent.write_file("/results/file", b"writable-volume")
                    check(f"{name}/volume", await agent.read_file("/results/file") == b"writable-volume")
                    await agent.upload_file(source / "payload", "/results/binary")
                    binary = root / f"binary-{name}"
                    await agent.download_file("/results/binary", binary)
                    check(f"{name}/binary-file-transfer", binary.read_bytes() == (source / "payload").read_bytes())
                    await agent.upload_dir(source, "/results/upload")
                    downloaded = root / f"download-{name}"
                    await agent.download_dir("/results/upload", downloaded)
                    check(
                        f"{name}/directory-transfer",
                        (downloaded / "payload").read_bytes() == (source / "payload").read_bytes(),
                    )
                    log = "/.flotilla-runtime/main.log" if readonly else "/run/flotilla/main.log"
                    async with asyncio.timeout(15):
                        while b"filesystem-contract-stderr" not in await agent.read_file(log):
                            await asyncio.sleep(0.1)
                    check(f"{name}/service-log", True)
                    if readonly:
                        scratch = await agent.shell("ls -A /.flotilla-runtime")
                        check(f"{name}/transfer-archives-removed", scratch.stdout.splitlines() == [b"main.log"])
                    if tmpfs:
                        stat = await agent.shell("stat -f -c %T /tmp; stat -c '%u:%g %a' /tmp")
                        check(
                            f"{name}/mount",
                            stat.exit_code == 0 and stat.stdout.splitlines() == [b"tmpfs", b"1000:1001 750"],
                        )
                        check(
                            f"{name}/non-root-write",
                            (await agent.shell("echo marker > /tmp/flotilla-marker", uid=1000, gid=1001)).exit_code
                            == 0,
                        )
                        check(f"{name}/exec-retains-data", await agent.read_file("/tmp/flotilla-marker") == b"marker\n")
                        # Fetch error bytes through the file API: some execd versions
                        # omit stderr when dd exits immediately after a failed write.
                        await agent.write_file("/results/capacity-error", b"", mode=0o666)
                        await agent.write_file("/results/capacity-stat", b"", mode=0o666)
                        fill = await agent.shell(
                            "dd if=/dev/zero of=/tmp/fill bs=65536 count=32 2>/results/capacity-error; rc=$?; "
                            "stat -f -c '%S %b %a' /tmp >/results/capacity-stat; "
                            "stat -c %s /tmp/fill >>/results/capacity-stat; exit $rc",
                            uid=1000,
                            gid=1001,
                            env={"LC_ALL": "C"},
                        )
                        error = await agent.read_file("/results/capacity-error")
                        counts = [int(value) for value in (await agent.read_file("/results/capacity-stat")).split()]
                        result.setdefault("capacity_results", []).append(
                            {
                                "case": name,
                                "exit_code": fill.exit_code,
                                "timed_out": fill.timed_out,
                                "stdout": fill.stdout.decode(errors="replace")[:1000],
                                "stderr": fill.stderr.decode(errors="replace")[:1000],
                                "stderr_file": error.decode(errors="replace")[:1000],
                                "capacity_stat": counts,
                            }
                        )
                        check(f"{name}/ENOSPC", fill.exit_code != 0 and b"No space left on device" in error)
                        check(
                            f"{name}/capacity-enforced",
                            len(counts) == 4
                            and counts[0] * counts[1] == 1024 * 1024
                            and counts[2] == 0
                            and 0 < counts[3] <= 1024 * 1024,
                        )
                        echo = await agent.shell("printf 'after-full-stdout\\n'; printf 'after-full-stderr\\n' >&2")
                        result.setdefault("output_after_full", []).append(
                            {
                                "case": name,
                                "exit_code": echo.exit_code,
                                "stdout": echo.stdout.decode(errors="replace"),
                                "stderr": echo.stderr.decode(errors="replace"),
                            }
                        )
                        check(
                            f"{name}/exec-output-after-ENOSPC",
                            echo.exit_code == 0
                            and echo.stdout == b"after-full-stdout\n"
                            and echo.stderr == b"after-full-stderr\n",
                            fatal=False,
                        )
                        check(f"{name}/read-file-after-ENOSPC", len(await agent.read_file("/tmp/fill")) == counts[3])
                        await agent.write_file("/tmp/fill", b"")
                        recovered = await agent.shell("printf 'after-free\\n'")
                        check(
                            f"{name}/exec-output-after-free",
                            recovered.exit_code == 0 and recovered.stdout == b"after-free\n",
                        )
                if tmpfs:
                    async with fleet.environment(definition, OpenOptions(startup_timeout=600)) as fresh:
                        result.setdefault("environments", []).append(
                            {"case": name + "-fresh", **asdict(await fresh.info())}
                        )
                        check(
                            f"{name}/new-instance-empty",
                            (await fresh.agent.shell("test ! -e /tmp/flotilla-marker && test ! -e /tmp/fill")).exit_code
                            == 0,
                        )
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        print(f"contract failed: {type(exc).__name__}", flush=True)
    finally:
        try:
            cleanup = await fleet.close(timeout=90)
            result["cleanup"] = asdict(cleanup)
            result["independent_instances_absent"] = not await observer.list({labels.LAUNCH: cleanup.launch_id})
            result["local_temporary_directory_absent"] = (
                "local_temporary_directory" in result and not Path(result["local_temporary_directory"]).exists()
            )
            audit = Anchor(observer, SystemClock(), "audit-" + uuid.uuid4().hex[:16], cfg.anchor_settings())
            result["audit_launch_id"] = audit.launch_id
            save()
            try:
                await audit.start()
                result["independent_directory_absent"] = not await audit.exists(
                    f"launches/{cleanup.launch_id}", stage="run"
                )
            finally:
                await audit.close()
                result["audit_anchor_absent"] = audit.iid is None or await Deleter(
                    observer, SystemClock(), retry_deadline_s=30
                ).delete(audit.iid)
            result["clean"] = cleanup.clean and all(
                result.get(key) is True
                for key in (
                    "independent_instances_absent",
                    "local_temporary_directory_absent",
                    "independent_directory_absent",
                    "audit_anchor_absent",
                )
            )
        except Exception as exc:
            result["clean"] = False
            result["cleanup_error_type"] = type(exc).__name__
        finally:
            await observer.aclose()
            save()
    return bool(result["clean"] and "error_type" not in result and all(item["passed"] for item in result["checks"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--case", choices=tuple(CASES), action="append", help="repeat to select cases; default: all")
    args = parser.parse_args()
    raise SystemExit(
        0 if asyncio.run(run(args.deployment, args.image, args.out, cases=tuple(args.case or CASES))) else 1
    )


if __name__ == "__main__":
    main()
