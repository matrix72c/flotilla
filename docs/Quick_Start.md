# Quick start

This tutorial prepares existing Harbor tasks on a CPU host, publishes a native release with your storage tools, and executes it using the installed Flotilla package. It assumes a working `deployment.toml`, platform credentials, a matching native Anchor and shared tools. See [Native tasks](Native_Tasks.md) for installation, protocol and migration details, and [Compose support](Compose_Support.md) for importer support.

## 1. Probe and select

Install this repository's fixed commit with the `harbor` extra in a Python 3.12 Linux x86_64 environment. Host preparation also needs the pinned official Compose tool and image tools. The execution Anchor does **not** need those optional dependencies. Do not install the unrelated public PyPI `flotilla` project.

Place complete source tasks below `./source-tasks` on your preparation host. Each task includes its `task.toml`, instruction, environment files and tests. Keep credentials separate.

```sh
flotilla probe --deployment deployment.toml --env sandbox.env
flotilla select --host-root ./source-tasks --deployment deployment.toml \
  --out selected.jsonl --report selection-report.json
```

Probe records measured capabilities and requires confirmed cleanup. Selection normalizes supported task definitions but does not build or open task sandboxes. Inspect its report before preparing. `--require-prebuilt-images` rejects tasks that would need application builds; Voyager applies this constraint automatically.

## 2. Prepare once

```sh
flotilla prepare selected.jsonl --deployment deployment.toml \
  --out ./release --name example --report prepare-report.json \
  --image-repo registry.example.org/team/task-runtime
```

This command may build and publish required images. Provide registry credentials explicitly via your tool configuration or `--env prepare.env`. The repository argument has no tag or digest. Non-root source images get a minimal USER0 adaptation after the original application identity is resolved. Runtime images are fixed by remote digest. Static bind files, hidden tests and volume seeds are captured in the release. tmpfs/readonly rootfs do not by themselves require derived images.

Preparation is atomic: failed selected rows do not silently disappear. Output must be a new directory. Keep reports and resumable work outside it. No platform task instances or static uploads are created by this command.

## 3. Publish and bind

Use existing storage tools to copy the **complete release**, preserving its contents and required file metadata, to a new immutable platform location. Do not copy the original Harbor task as the execution input. Grant the platform access to the fixed runtime images.

```toml
[task_releases.demo]
local_root = "./release"
platform_root = "/shared/releases/demo-v1"
```

The platform root must be under your configured host storage root. PVC deployments instead use an explicit PVC location or relative storage key; see [Native tasks](Native_Tasks.md#bind-local-and-platform-roots). Local relative paths resolve against the deployment file. Consumers never repair missing files by rebuilding or reimporting source tasks.

## 4. Execute

```python
import asyncio
from pathlib import Path

from flotilla import Flotilla, OpenOptions
from flotilla.config import FlotillaConfig
from flotilla.native import NativeAttempt


async def main():
    config = FlotillaConfig.load("deployment.toml")
    bindings = config.release_bindings()
    rows = bindings.read_dataset("release/dataset.jsonl")
    async with Flotilla(deployment=config) as fleet:
        for row in rows:
            bound = bindings.resolve(row)
            async with await NativeAttempt.create(fleet, bound) as attempt:
                env = await attempt.open("main", OpenOptions(startup_timeout=900, lifetime=600))
                result = await env.agent.exec(["id"], timeout=30)
                print(result.stdout.decode())
                verification = await attempt.verify()
                print(verification)
    assert fleet.cleanup_report is not None and fleet.cleanup_report.clean


asyncio.run(main())
```

A dataset row identifies its release, relative task reference and digest. Preserve the complete row, including annotations and parameters, in snapshots. Duplicate rows remain duplicate attempts. Filtering/reordering rows does not alter release identity.

The attempt starts services according to prepared dependency and healthcheck rules, using resolved application UID/GID/env/cwd. Writable state is attempt-private. Hidden tests are staged only for verification; separate verifier roles and declared handoffs use the same lifecycle. Closing waits for confirmed cleanup. Infrastructure and cleanup failures are not task scores.

## Commands and files

Once an environment is open, its regular service APIs remain available:

```python
await env.agent.exec(["ls", "-l", "/"], timeout=10)
await env.agent.shell("printf '%s' hello > out.txt", cwd="/tmp", timeout=10)
await env.agent.exec(["id"], uid=1000, gid=1000, timeout=10)
await env.agent.download_file("/tmp/out.txt", Path("out.txt"))
```

Only use writable paths declared by the task: readonly-rootfs tasks do not implicitly gain a writable `/tmp`. Runtime options may override modeled resources, timeouts and execution settings; they cannot re-run Compose interpolation or change prepared images/topology. Prepare a new variant for compilation changes.

`exec` returns bytes stdout/stderr and an exit code. A command is not silently replayed after an uncertain response. See [Runtime API](Runtime_API.md) for timeout, cancellation, background process and `ExecOutcomeUnknown` behavior.

## Next steps

- [Native tasks](Native_Tasks.md): protocol, installed Anchor builder, path binding and migration.
- [Harbor quick start](Harbor_Quick_Start.md): the optional Harbor evaluation consumer.
- [Configuration](Configuration.md): deployment policies, storage and admission.
- [Compose support](Compose_Support.md): supported source semantics.
