# Quick start

This tutorial publishes a directory of Harbor tasks to shared storage, declares where it is, and runs
one task using the installed Flotilla package. It assumes deployment values from whoever runs your
sandbox platform, platform credentials, a matching Anchor and shared tools. See [Native tasks](Native_Tasks.md) for installation
and protocol details, and [Compose support](Compose_Support.md) for which Compose keys are supported.

## 1. Write the deployment spec

Install this repository's fixed commit in a Python 3.12 Linux x86_64 environment. Nothing needs Docker
or a Compose binary. Do not install the unrelated public PyPI `flotilla` project.

A deployment is a `FlotillaConfig` you construct. There is no deployment file: put the spec in a module
of your own, alongside the rest of your configuration, and the type checker tells you what is missing.

```python
# myproject/deployment.py
from flotilla.config import DatasetBinding, FlotillaConfig, OpenSandbox, OpenSandboxNetwork, Reaper, Storage

PLATFORM = FlotillaConfig(
    opensandbox=OpenSandbox(
        endpoint="https://sandbox.example.com",
        credential_env="SANDBOX_CREDENTIAL",
        network=OpenSandboxNetwork(sandbox_cidrs=["10.42.0.0/16"]),
    ),
    storage=Storage(host_path="/shared"),
    datasets={"demo": DatasetBinding(local_root="/srv/tasks", platform_root="/shared/datasets/demo-v1")},
    reaper=Reaper(anchor_image="registry.example.com/team/anchor@sha256:..."),
)
```

## 2. Measure the deployment's isolation

Commands outside your process name the spec by where to import it from, because a crashed launch
leaves nobody to call the SDK:

```sh
flotilla probe --deployment myproject.deployment:PLATFORM --env sandbox.env
```

The probe measures whether the restrictions a deployment can lack silently actually hold, and requires
confirmed cleanup. Run it once per deployment, before running any task on it.

## 3. Publish the dataset

A dataset is a directory of task directories. Copy it to shared storage with your existing tools,
preserving contents and file metadata. Nothing is generated in between: the directory the platform
mounts is the one you published.

Each image must be published and referenced by digest (`repo@sha256:...`), and must declare a root
`USER`: the exec daemon is the sandbox entrypoint. A task that declares `build:` instead of an image is
rejected, because Flotilla does not build images.

## 4. Both sides of the binding

`datasets` above declares both views because neither side can infer the other: a client path says
nothing about how the platform mounts the storage. On host storage the platform root is either a key
relative to the shared root or an absolute path under it; a `pvc:` value is refused. PVC deployments
require the explicit `pvc:<claim>/<subpath>/...` form and refuse a relative key. `local_root` must be
absolute, because a spec outlives the directory it was written in. See
[Native tasks](Native_Tasks.md#bind-local-and-platform-roots).

## 5. Execute

```python
import asyncio
from pathlib import Path

from flotilla import Flotilla, OpenOptions
from flotilla.native import NativeAttempt

from myproject.deployment import PLATFORM


async def main():
    dataset = PLATFORM.resolve_datasets()["demo"]
    async with Flotilla(deployment=PLATFORM) as fleet:
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
- [Configuration](Configuration.md): every field of a deployment spec.
- [Compose support](Compose_Support.md): which Compose keys are supported.
