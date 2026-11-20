# Quick start

This tutorial publishes a directory of Harbor tasks to shared storage, declares where it is, and runs
one task using the installed Flotilla package. It assumes deployment values from whoever runs your
sandbox platform and platform credentials. See [Compose support](Compose_Support.md) for which
Compose keys are supported.

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
preserving contents and file metadata; the directory the platform mounts is the one you published.
Every image must be named by digest (`repo@sha256:...`) and declare a root `USER`. `datasets` above
declares both the client path and the platform path, because neither side can infer the other. See
[Running published tasks](Native_Tasks.md) for the task layout and binding rules.

## 4. Execute

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

Once an environment is open, its service APIs remain available:

```python
await env.agent.exec(["ls", "-l", "/"], timeout=10)
await env.agent.shell("printf '%s' hello > out.txt", cwd="/tmp", timeout=10)
await env.agent.download_file("/tmp/out.txt", Path("out.txt"))
```

Only use writable paths the task declares. See [Runtime API](Runtime_API.md) for identities,
timeouts, cancellation, background processes and `ExecOutcomeUnknown`.

## Next steps

- [Running published tasks](Native_Tasks.md): task layout, datasets, grading phases and `flotilla inspect`.
- [Integration guide](Integration_Guide.md): use Flotilla from an evaluation runner or RL framework.
- [Configuration](Configuration.md): every field of a deployment spec.
- [Compose support](Compose_Support.md): which Compose keys are supported.
