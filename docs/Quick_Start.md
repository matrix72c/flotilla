# Quick start

This tutorial publishes a directory of Harbor tasks to shared storage, declares where it is, and runs
one task using the installed Flotilla package. It assumes a working `deployment.toml`, platform
credentials, a matching Anchor and shared tools. See [Native tasks](Native_Tasks.md) for installation
and protocol details, and [Compose support](Compose_Support.md) for which Compose keys are supported.

## 1. Probe the deployment

Install this repository's fixed commit with the `harbor` extra in a Python 3.12 Linux x86_64
environment. Nothing needs Docker or a Compose binary. Do not install the unrelated public PyPI
`flotilla` project.

```sh
flotilla probe --deployment deployment.toml --env sandbox.env
```

Probe records measured capabilities and requires confirmed cleanup. Run it once per deployment, before
running any task on it.

## 2. Publish the dataset

A dataset is a directory of task directories. Copy it to shared storage with your existing tools,
preserving contents and file metadata. Nothing is generated in between: the directory the platform
mounts is the one you published.

Each image must be published and referenced by digest (`repo@sha256:...`), and must declare a root
`USER`: the exec daemon is the sandbox entrypoint. A task that declares `build:` instead of an image is
rejected, because Flotilla does not build images.

## 3. Declare where it is

```toml
[datasets.demo]
local_root = "./tasks"
platform_root = "/shared/datasets/demo-v1"
```

Both views are declared because neither side can infer the other: a client path says nothing about how
the platform mounts the storage. On host storage the platform root is either a key relative to the
shared root or an absolute path under it; a `pvc:` value is refused. PVC deployments require the
explicit `pvc:<claim>/<subpath>/...` form and refuse a relative key. See
[Native tasks](Native_Tasks.md#bind-local-and-platform-roots). Local relative paths resolve against the
deployment file.

## 4. Execute

```python
import asyncio
from pathlib import Path

from flotilla import Flotilla, OpenOptions
from flotilla.config import FlotillaConfig
from flotilla.native import NativeAttempt


async def main():
    config = FlotillaConfig.load("deployment.toml")
    dataset = config.resolve_datasets()["demo"]
    async with Flotilla(deployment=config) as fleet:
        for key in dataset.tasks():
            async with await NativeAttempt.create(fleet, dataset.source(key)) as attempt:
                env = await attempt.open("main", OpenOptions(startup_timeout=900, lifetime=600))
                result = await env.agent.exec(["id"], timeout=30)
                print(result.stdout.decode())
                verification = await attempt.verify()
                print(verification)
    assert fleet.cleanup_report is not None and fleet.cleanup_report.clean


asyncio.run(main())
```

`dataset.tasks()` lists every task directory below the root, in a stable order; `dataset.source(key)`
names one of them on both sides. Your own selection decides which keys to run and how often, so
duplicates stay duplicate attempts.

The attempt starts services according to the task's dependency and healthcheck rules, using the
resolved application UID/GID/env/cwd. The task's own files are mounted read-only from where they are
published, and only a writable volume is materialized per attempt. Hidden tests are staged only for
verification; separate verifier roles and declared handoffs use the same lifecycle. Closing waits for
confirmed cleanup. Infrastructure and cleanup failures are not task scores.

## Commands and files

Once an environment is open, its regular service APIs remain available:

```python
await env.agent.exec(["ls", "-l", "/"], timeout=10)
await env.agent.shell("printf '%s' hello > out.txt", cwd="/tmp", timeout=10)
await env.agent.exec(["id"], uid=1000, gid=1000, timeout=10)
await env.agent.download_file("/tmp/out.txt", Path("out.txt"))
```

Only use writable paths declared by the task: readonly-rootfs tasks do not implicitly gain a writable `/tmp`. Runtime options may override resources, timeouts and execution settings; they cannot change the task's images or topology. Change the task directory for anything else.

`exec` returns bytes stdout/stderr and an exit code. A command is not silently replayed after an uncertain response. See [Runtime API](Runtime_API.md) for timeout, cancellation, background process and `ExecOutcomeUnknown` behavior.

## Next steps

- [Native tasks](Native_Tasks.md): protocol, installed Anchor builder and path binding.
- [Harbor quick start](Harbor_Quick_Start.md): the optional Harbor evaluation consumer.
- [Configuration](Configuration.md): deployment policies, storage and admission.
- [Compose support](Compose_Support.md): which Compose keys are supported.
