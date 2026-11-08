"""Opt-in real workspace/Harbor contract; metadata-only unless --runtime is selected.

Requires the configured platform credential in the environment and a management image.
It creates only uniquely named test sources/attempts and confirms their cleanup.
--runtime additionally requires a valid capability report and runs deterministic shared/
separate verifier roles through normal compilation and admission. It is not an LLM rollout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import traceback
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from flotilla.config import FlotillaConfig
from flotilla.core import labels
from flotilla.core.anchor import Anchor
from flotilla.core.deletion import Deleter
from flotilla.platform.clock import SystemClock
from flotilla.runtime.workspace import WorkspaceStore


async def run(
    deployment: Path,
    output: Path,
    image: str | None = None,
    *,
    runtime: bool = False,
    runtime_image: str = "python:3.12-slim",
) -> bool:
    cfg = FlotillaConfig.load(deployment)
    if runtime:
        cfg.check_against(cfg.load_report())
    clock = SystemClock()
    platform = cfg.build_platform(clock=clock, caps=cfg.management_capabilities())
    launch = "workspace-check-" + uuid.uuid4().hex[:12]
    settings = cfg.anchor_settings(workspace=True)
    anchor = Anchor(platform, clock, launch, replace(settings, image=image or settings.image))
    store = WorkspaceStore(anchor, cfg)
    fixture = "datasets/" + launch
    result: dict[str, Any] = {
        "launch": launch,
        "image": image or settings.image,
        "runtime_image": runtime_image if runtime else None,
        "scope": "workspace-and-deterministic-runtime" if runtime else "workspace-copy-and-native-description-only",
        "checks": [],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    started = False

    def check(name: str, passed: bool) -> None:
        result["checks"].append({"name": name, "passed": passed})
        print(f"{name}: {passed}", flush=True)
        if not passed:
            raise AssertionError(name)

    try:
        await anchor.start()
        started = True
        check("management_image_platform_boot", True)
        tool = await anchor.execute(
            ("python3", "-I", "-c", 'import flotilla.integrations.workspace,harbor; print("imports-ok")'),
            timeout_s=60,
            stage="prepare",
        )
        check("installed_compiler_with_controlled_path", tool.stdout.strip() == b"imports-ok")
        key = fixture + "/same-id"
        other = fixture + "/other/same-id"
        for directory in ("environment", "tests"):
            await anchor.mkdir(key + "/" + directory, stage="prepare")
        files = {
            "task.toml": b'[environment]\ndocker_image = "python:3.12-slim"\n',
            "instruction.md": b"Create /result with the exact text accepted.\n",
            "tests/test.sh": b"#!/bin/sh\nmkdir -p /logs/verifier\nprintf 1 > /logs/verifier/reward.txt\n",
            "environment/payload": b"original-content\n",
        }
        for name, data in files.items():
            await anchor.upload(key + "/" + name, data, mode=0o644, uid=0, gid=0, stage="prepare")
        await anchor.execute(
            (
                "/bin/sh",
                "-c",
                'chmod 640 "$1/environment/payload"; '
                'ln "$1/environment/payload" "$1/environment/hardlink"; '
                'ln -s payload "$1/environment/relative-link"',
                "sh",
                anchor.path(key),
            ),
            timeout_s=30,
            stage="prepare",
        )
        await anchor.copy_source_tree(key, other, stage="prepare")
        owned = [await store.create(source) for source in (key, key, other)]
        check("duplicate_basename_preserved", [item.task_id for item in owned] == ["same-id"] * 3)
        check("independent_attempt_paths", len({item.key for item in owned}) == 3)
        first = owned[0]
        for index, item in enumerate(owned):
            stat = await anchor.stat(item.source_key + "/environment/payload", stage="prepare")
            check(f"mode_preserved_{index}", stat.mode == 0o640)
            links = await anchor.execute(
                (
                    "/bin/sh",
                    "-c",
                    'test "$1/environment/payload" -ef "$1/environment/hardlink" && '
                    'test "$(readlink "$1/environment/relative-link")" = payload',
                    "sh",
                    anchor.path(item.source_key),
                ),
                timeout_s=30,
                stage="prepare",
            )
            check(f"hardlink_and_symlink_preserved_{index}", links.exit_code == 0)
        await store.write(first.id, "source/same-id/environment/payload", b"private-change\n")
        check(
            "original_unchanged",
            await anchor.download(key + "/environment/payload", stage="prepare") == files["environment/payload"],
        )
        check(
            "other_attempt_unchanged",
            await anchor.download(owned[1].source_key + "/environment/payload", stage="prepare")
            == files["environment/payload"],
        )
        request = {
            "workspace": anchor.path(first.key),
            "source": anchor.path(first.source_key),
            "task_id": first.task_id,
            "describe": True,
        }
        await store.write(first.id, "request.json", json.dumps(request).encode())
        await anchor.execute(
            (*cfg.prepare.workspace_command, anchor.path(first.key + "/request.json")),
            timeout_s=60,
            stage="prepare",
        )
        metadata = json.loads(await store.read(first.id, "result.json"))
        check("native_instruction_description", metadata["instruction"] == files["instruction.md"].decode())
        check("native_basename_identity", metadata["task_id"] == "same-id")
        check(
            "hidden_tests_private",
            await anchor.exists(first.key + "/private-tests/test.sh", stage="prepare")
            and not await anchor.exists(first.key + "/tests/test.sh", stage="prepare"),
        )
        check(
            "original_test_mode_unchanged",
            (await anchor.stat(key + "/tests/test.sh", stage="prepare")).mode == 0o644,
        )
        check(
            "copied_test_executable",
            bool((await anchor.stat(first.key + "/private-tests/test.sh", stage="prepare")).mode & 0o111),
        )
        if runtime:
            await runtime_checks(cfg, anchor, fixture + "/runtime", check, runtime_image)
    except Exception as exc:
        # Remote exception messages may include credentials; report only the category.
        result["error_type"] = type(exc).__name__
        result["error_details"] = {
            "stage": getattr(exc, "stage", None),
            "category": getattr(exc, "category", None),
            "frames": [
                {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(exc.__traceback__)
            ],
        }
    finally:
        try:
            await store.settle()
            for wid in tuple(store.records):
                await store.remove(wid)
            if started:
                await anchor.remove(fixture, stage="prepare")
                await anchor.remove("launches/" + launch, stage="prepare")
                result["storage_cleanup"] = not await anchor.exists(
                    fixture, stage="prepare"
                ) and not await anchor.exists("launches/" + launch, stage="prepare")
        except Exception as exc:
            result["storage_cleanup_error"] = type(exc).__name__
        finally:
            try:
                iid = anchor.iid
                await anchor.close()
                result["instance_cleanup"] = iid is None or await Deleter(platform, clock, retry_deadline_s=60).delete(
                    iid
                )
                result["independent_instances_absent"] = not await platform.list({labels.LAUNCH: launch})
            except Exception as exc:
                result["instance_cleanup_error"] = type(exc).__name__
            finally:
                await platform.aclose()
                output.write_text(json.dumps(result, indent=2) + "\n")
    return "error_type" not in result and all(
        result.get(name) is True for name in ("storage_cleanup", "instance_cleanup", "independent_instances_absent")
    )


async def runtime_checks(
    cfg: FlotillaConfig, anchor: Anchor, fixture: str, check: Callable[[str, bool], None], runtime_image: str
) -> None:
    """Use the actual compiler/admission/runtime for shared and separate verifier roles, without an LLM."""
    from flotilla.integrations.harbor_attempt import HarborAttempt
    from flotilla.runtime.client import Flotilla

    script = (
        b"#!/bin/sh\nset -eu\n"
        b"path=/harbor-handoff/result\n"
        b"if test -f /harbor-handoff/main/result; then path=/harbor-handoff/main/result; fi\n"
        b'test "$(cat "$path")" = accepted\n'
        b"mkdir -p /logs/verifier\nprintf 1 > /logs/verifier/reward.txt\n"
    )
    for role in ("shared", "separate"):
        key = fixture + "/" + role + "/same-id"
        for directory in ("environment", "tests"):
            await anchor.mkdir(key + "/" + directory, stage="prepare")
        task = f"[environment]\ndocker_image={json.dumps(runtime_image)}\n"
        if role == "separate":
            # Inherit the main image: explicit verifier images promise bundled tests,
            # whereas this fixture exercises native tests staged into the workspace.
            task += '[verifier]\nenvironment_mode="separate"\n'
        for name, data in {
            "task.toml": task.encode(),
            "instruction.md": b"Write accepted into /harbor-handoff/result.\n",
            "tests/test.sh": script,
        }.items():
            await anchor.upload(key + "/" + name, data, mode=0o644, uid=0, gid=0, stage="prepare")

    fleet = Flotilla(deployment=cfg)
    workspaces = []
    try:
        for index, role in enumerate(("shared", "shared", "separate")):
            attempt = await HarborAttempt.create(fleet, fixture + "/" + role + "/same-id")
            workspaces.append(attempt.workspace.key)
            prefix = f"runtime-{index}-{role}"
            try:
                check(prefix + "/basename", attempt.task.name == "same-id")
                main = await attempt.open()
                hidden = await main.agent.shell(
                    'test -z "$(ls -A /tests)" && test ! -e /harbor-handoff/result '
                    "&& test -r /harbor-instruction/instruction.md && test ! -e /sandbox-data"
                )
                check(prefix + "/private-tests-and-fresh-handoff", hidden.exit_code == 0)
                write = await main.agent.shell("printf accepted > /harbor-handoff/result")
                check(prefix + "/agent-artifact", write.exit_code == 0)
                if role == "separate":
                    await attempt.release("main")
                await attempt.stage_tests()
                verifier = await attempt.open("verifier") if role == "separate" else main
                if role == "separate":
                    readonly = await verifier.agent.shell("! touch /harbor-handoff/forbidden")
                    check(prefix + "/verifier-handoff-readonly", readonly.exit_code == 0)
                verified = await verifier.agent.shell("/bin/sh /tests/test.sh")
                check(prefix + "/verifier-script", verified.exit_code == 0 and not verified.timed_out)
                check(prefix + "/reward", await verifier.agent.read_file("/logs/verifier/reward.txt") == b"1")
            finally:
                await attempt.close()
            check(prefix + "/workspace-removed", not await anchor.exists(workspaces[-1], stage="prepare"))
        check("runtime-duplicate-workspaces-independent", len(set(workspaces)) == 3)
    finally:
        cleanup = await fleet.close()
        check("runtime-cleanup-report", cleanup.clean)
        check(
            "runtime-independent-instances-absent",
            not await anchor._platform.list({labels.LAUNCH: cleanup.launch_id}),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--image", help="override only the anchor image for candidate validation")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--runtime", action="store_true", help="also run admitted shared/separate verifier fixtures")
    parser.add_argument(
        "--runtime-image", default="python:3.12-slim", help="fixture image with Python and a POSIX shell"
    )
    args = parser.parse_args()
    passed = asyncio.run(
        run(args.deployment, args.out, args.image, runtime=args.runtime, runtime_image=args.runtime_image)
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
