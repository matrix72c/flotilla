"""Opt-in real-platform acceptance of native Harbor CLI and SandboxRL Runner.

Requires patched Harbor, SandboxRL, a compiler-capable deployment, platform
credentials supplied by the caller's environment, and a real immutable Lagent
runtime package already present in the deployment storage. No LLM is called.

Run from the Flotilla checkout with both consumer sources available on PYTHONPATH:
python -m tests.contract.native_consumers --deployment DEPLOYMENT.toml \
    --runtime-image IMAGE --agent-runtime RUNTIME.json --out NEW_OUTPUT_DIRECTORY

This creates uniquely owned neutral task sources and platform resources. It does
not delete unrelated resources. Failed cleanup is reported, never treated as a pass.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import traceback
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from harbor.agents.nop import NopAgent

from flotilla.config import FlotillaConfig
from flotilla.core import labels
from flotilla.core.anchor import Anchor
from flotilla.core.deletion import Deleter
from flotilla.platform.clock import SystemClock

MODES = ("shared", "separate", "compose")
MAIN_COMMAND = (
    'set -eu; test -z "$(ls -A /tests)"; '
    "test -r /harbor-instruction/instruction.md; "
    "mkdir -p /evidence; printf 'main-owned\\n' > /evidence/main.txt"
)


def ownership(workspace: str, task_key: str) -> dict[str, str]:
    parts = PurePosixPath(workspace).parts
    if len(parts) != 4 or parts[0] != "launches" or parts[2] != "attempts":
        raise ValueError("unexpected workspace ownership path")
    return {"launch": parts[1], "workspace": workspace, "task_key": task_key}


class NeutralShellAgent(NopAgent):
    """Actual Harbor agent entry point; fixed shell command, no model/provider."""

    @staticmethod
    def name() -> str:
        return "flotilla-neutral-contract"

    async def run(self, instruction, environment, context):
        attempt = environment.runtime_context.attempt
        if attempt is None:
            raise RuntimeError("native runtime has no active attempt")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "contract-ownership.json").write_text(
            json.dumps(ownership(attempt.workspace.key, attempt.workspace.task_key)) + "\n"
        )
        result = await environment.exec(MAIN_COMMAND, user="root", timeout_sec=30)
        if result.return_code != 0:
            raise RuntimeError("neutral shell agent failed")


def fixture_files(mode: str, image: str) -> dict[str, bytes]:
    config = '[[artifacts]]\nsource="/evidence/main.txt"\ndestination="main.txt"\n'
    if mode == "compose":
        config += '[[artifacts]]\nsource="/evidence/db.txt"\ndestination="db.txt"\nservice="db"\n'
    config += f"[environment]\ndocker_image={json.dumps(image)}\n"
    config += f'[verifier]\nenvironment_mode="{"shared" if mode == "shared" else "separate"}"\ntimeout_sec=120\n'
    if mode == "compose":
        config += (
            '[[verifier.collect]]\nservice="db"\nuser="root"\ntimeout_sec=30\n'
            'command="until test -s /evidence/db.txt; do sleep 0.1; done"\n'
        )
    script = '#!/bin/sh\nset -eu\ntest "$(cat /evidence/main.txt)" = main-owned\n'
    if mode == "compose":
        script += 'test "$(cat /evidence/db.txt)" = sidecar-owned\n'
    script += 'mkdir -p /logs/verifier\nprintf "1\\n" > /logs/verifier/reward.txt\n'
    files = {
        "task.toml": config.encode(),
        "instruction.md": b"Write main-owned into /evidence/main.txt. This is a deterministic contract fixture.\n",
        "tests/test.sh": script.encode(),
    }
    if mode == "compose":
        # JSON is valid YAML; no image building or registry credentials are embedded.
        files["environment/docker-compose.yaml"] = json.dumps(
            {
                "services": {
                    "main": {"image": image},
                    "db": {
                        "image": image,
                        # The fixture command must not inherit an image's ENTRYPOINT.
                        "entrypoint": [],
                        "command": [
                            "/bin/sh",
                            "-c",
                            "mkdir -p /evidence; printf 'sidecar-owned\\n' > /evidence/db.txt; exec sleep infinity",
                        ],
                    },
                }
            }
        ).encode()
    return files


async def publish_fixture(anchor: Anchor, local_root: Path, prefix: str, image: str) -> None:
    local_root.mkdir()
    for mode in MODES:
        local = local_root / mode
        for directory in ("environment", "tests"):
            (local / directory).mkdir(parents=True, exist_ok=True)
            await anchor.mkdir(f"{prefix}/{mode}/{directory}", stage="prepare")
        for name, data in fixture_files(mode, image).items():
            path = local / name
            path.write_bytes(data)
            permissions = 0o755 if name == "tests/test.sh" else 0o644
            path.chmod(permissions)
            await anchor.upload(f"{prefix}/{mode}/{name}", data, mode=permissions, uid=0, gid=0, stage="prepare")


def native_job_config(deployment: Path, root: Path, prefix: str, output: Path) -> dict:
    """Build/validate the exact CLI config without contacting a platform."""
    from harbor.models.job.config import JobConfig

    data = {
        "job_name": "native-consumers",
        "jobs_dir": str(output / "harbor-jobs"),
        "datasets": [{"path": str(root)}],
        "task_source": {
            "import_path": "flotilla.integrations.harbor_native:FlotillaTaskSource",
            "kwargs": {"sources": [{"local_root": str(root), "platform_prefix": prefix}], "metadata_workers": 1},
        },
        "runtime": {
            "import_path": "flotilla.integrations.harbor_native:NativeFlotillaRuntime",
            "kwargs": {"deployment": str(deployment)},
        },
        "environment": {"import_path": "flotilla.integrations.harbor_native:NativeFlotillaEnvironment"},
        "agents": [{"import_path": "tests.contract.native_consumers:NeutralShellAgent"}],
        "n_concurrent_trials": 1,
        "n_attempts": 1,
        "retry": {"max_retries": 0},
    }
    JobConfig.model_validate(data)
    return data


async def run_native(deployment: Path, root: Path, prefix: str, output: Path) -> list[dict[str, str]]:
    data = native_job_config(deployment, root, prefix, output)
    config = output / "harbor-config.json"
    config.write_text(json.dumps(data, indent=2) + "\n")
    with (output / "harbor.stdout.log").open("wb") as stdout, (output / "harbor.stderr.log").open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "harbor.cli.main", "run", "--config", str(config), stdout=stdout, stderr=stderr
        )
        try:
            returncode = await process.wait()
        except BaseException:
            if process.returncode is None:
                process.send_signal(signal.SIGINT)
                await asyncio.shield(process.wait())
            raise
    (output / "harbor-exit-code.json").write_text(json.dumps({"returncode": returncode}) + "\n")
    if returncode:
        raise RuntimeError("Harbor CLI subprocess failed; inspect private output logs")
    owners = []
    for path in (output / "harbor-jobs").glob("*/*/agent/contract-ownership.json"):
        owner = json.loads(path.read_text())
        trial = path.parent.parent
        result = json.loads((trial / "result.json").read_text())
        if result["exception_info"] is not None or result.get("cleanup_exception_info"):
            raise AssertionError("native Trial failed or cleanup was incomplete")
        if result["verifier_result"]["rewards"] != {"reward": 1.0}:
            raise AssertionError("native Trial reward mismatch")
        artifacts = trial / "artifacts"
        if (artifacts / "main.txt").read_text() != "main-owned\n":
            raise AssertionError("native main artifact missing")
        manifest = json.loads((artifacts / "manifest.json").read_text())
        expected = {"/evidence/main.txt"}
        if owner["task_key"].endswith("/compose"):
            if (artifacts / "db.txt").read_text() != "sidecar-owned\n":
                raise AssertionError("native db artifact missing")
            expected.add("/evidence/db.txt")
        if not expected <= {entry["source"] for entry in manifest if entry["status"] == "ok"}:
            raise AssertionError("native artifact manifest incomplete")
        owners.append(owner)
    if sorted(owner["task_key"] for owner in owners) != sorted(f"{prefix}/{mode}" for mode in MODES):
        raise AssertionError("native CLI did not execute exactly the selected tasks")
    return owners


async def run_sandbox(deployment: Path, root: Path, prefix: str, runtime: dict, output: Path) -> list[dict[str, str]]:
    from sandbox_rl.harbor.provider import HarborEnvironmentProvider
    from sandbox_rl.harbor.recipe import HarborRecipe
    from sandbox_rl.harbor.verifier import HarborJudger
    from sandbox_rl.rollout.execution import ReadFileHook, RolloutStatus, Runner, SandboxStage, ShellEntry

    provider = HarborEnvironmentProvider(
        deployment,
        agent_runtime=runtime,
        agent_kind="interndp",
        sources=[{"local_root": root, "platform_prefix": prefix}],
        output_dir=output / "sandbox",
    )
    runner = Runner(
        infer=SandboxStage(
            entries=[ShellEntry(name="neutral-shell", cmd=MAIN_COMMAND, timeout=30)],
            post=[ReadFileHook("/evidence/main.txt", "main_artifact")],
        ),
        validate=HarborJudger(timeout=120),
    )
    owners = []
    recipe = HarborRecipe()
    try:
        for uid, mode in enumerate(MODES, start=1):
            item = recipe.build_rollout_item(recipe.parse_record({"task_key": f"{prefix}/{mode}"}), uid=uid)
            async with provider.open(item) as episode:
                owners.append(ownership(episode.attempt.workspace.key, item.environment["task_key"]))
                (output / "sandbox-ownership.json").write_text(json.dumps(owners, indent=2) + "\n")
                if mode == "compose":
                    client = await episode.get("db")
                    ready = await client.execute("until test -s /evidence/db.txt; do sleep 0.1; done", timeout_sec=30)
                    if ready["return_code"] != 0:
                        raise AssertionError("neutral db sidecar did not become ready")
                    item.artifacts["db_artifact"] = (await client.download_file("/evidence/db.txt")).decode()
                result = await runner.run(item, episode)
                (output / f"sandbox-{mode}.json").write_text(result.model_dump_json(indent=2) + "\n")
                if result.status != RolloutStatus.COMPLETED or result.reward != 1:
                    raise AssertionError("SandboxRL Runner did not complete with reward one")
                if result.artifacts["main_artifact"] != "main-owned\n":
                    raise AssertionError("SandboxRL captured artifact mismatch")
                if mode == "compose" and result.artifacts["db_artifact"] != "sidecar-owned\n":
                    raise AssertionError("SandboxRL captured db artifact mismatch")
                if result.artifacts["scoring_evidence"]["rewards"] != {"reward": 1.0}:
                    raise AssertionError("SandboxRL verifier evidence mismatch")
    finally:
        await provider.close()
        report = provider.fleet.cleanup_report
        if report is None or not report.clean:
            raise AssertionError("SandboxRL cleanup report is not clean")
    return owners


async def run(deployment: Path, image: str, runtime_file: Path, output: Path) -> bool:
    from sandbox_rl.config.spec.config_groups import AgentRuntimeConfig

    deployment = deployment.expanduser().resolve(strict=True)
    cfg = FlotillaConfig.load(deployment)
    cfg.check_against(cfg.load_report())
    runtime = AgentRuntimeConfig.model_validate_json(runtime_file.read_text())
    if runtime.storage_root_identity != cfg.storage.settings().root:
        raise ValueError("agent runtime storage identity differs from deployment")
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    local_root = output / "originals"
    launch = "native-consumers-" + uuid.uuid4().hex[:12]
    prefix = f"datasets/{launch}"
    clock = SystemClock()
    platform = cfg.build_platform(clock=clock, caps=cfg.management_capabilities())
    anchor = Anchor(platform, clock, launch, cfg.anchor_settings(workspace=True))
    result: dict[str, Any] = {"launch": launch, "fixture": prefix, "runtime_image": image, "checks": []}
    started = False

    async def assert_no_leaks(consumer: str, owners: list[dict[str, str]]) -> None:
        for owner in owners:
            if await anchor.exists(owner["workspace"], stage="prepare"):
                raise AssertionError(f"{consumer} workspace remains")
            if await platform.list({labels.LAUNCH: owner["launch"]}):
                raise AssertionError(f"{consumer} platform instances remain")
        result["checks"].append({"consumer": consumer, "owners": owners, "no_leaks": True})

    try:
        await anchor.start()
        started = True
        await publish_fixture(anchor, local_root, prefix, image)
        native = await run_native(deployment, local_root, prefix, output)
        await assert_no_leaks("harbor-cli", native)
        sandbox = await run_sandbox(deployment, local_root, prefix, runtime.model_dump(mode="json"), output)
        await assert_no_leaks("sandbox-runner", sandbox)
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error_stage"] = getattr(exc, "stage", None)
        result["error_category"] = getattr(exc, "category", None)
        # Keep native platform failure reasons in the private log, not stdout.
        (output / "error.private.txt").write_text(str(exc) + "\n")
        result["error_frames"] = [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)
        ]
    finally:
        try:
            if started:
                await anchor.remove(prefix, stage="prepare")
                await anchor.remove("launches/" + launch, stage="prepare")
                result["storage_cleanup"] = not await anchor.exists(prefix, stage="prepare")
        except Exception as exc:
            result["storage_cleanup_error"] = type(exc).__name__
        finally:
            try:
                iid = anchor.iid
                await anchor.close()
                result["anchor_cleanup"] = iid is None or await Deleter(platform, clock, retry_deadline_s=60).delete(
                    iid
                )
                result["anchor_instances_absent"] = not await platform.list({labels.LAUNCH: launch})
            except Exception as exc:
                result["anchor_cleanup_error"] = type(exc).__name__
            finally:
                try:
                    await platform.aclose()
                finally:
                    # Retain ownership even when a consumer failed before its assertions,
                    # so operators can inspect exact resources rather than broad deletion.
                    result["native_ownership"] = [
                        json.loads(path.read_text())
                        for path in (output / "harbor-jobs").glob("*/*/agent/contract-ownership.json")
                    ]
                    sandbox_ownership = output / "sandbox-ownership.json"
                    result["sandbox_ownership"] = (
                        json.loads(sandbox_ownership.read_text()) if sandbox_ownership.exists() else []
                    )
                    (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    return (
        "error_type" not in result
        and len(result["checks"]) == 2
        and all(result.get(name) is True for name in ("storage_cleanup", "anchor_cleanup", "anchor_instances_absent"))
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument(
        "--runtime-image", required=True, help="Linux fixture image with bash, Python, tar and coreutils"
    )
    parser.add_argument(
        "--agent-runtime", type=Path, required=True, help="JSON AgentRuntimeConfig for a real staged runtime package"
    )
    parser.add_argument("--out", type=Path, required=True, help="new private output directory")
    args = parser.parse_args()
    os.umask(0o077)
    passed = asyncio.run(run(args.deployment, args.runtime_image, args.agent_runtime, args.out))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
