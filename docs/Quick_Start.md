# Library quick start

You need Python 3.12 on Linux x86_64, Docker with BuildKit/buildx, and a working `deployment.toml` (see the [deployment guide](Deployment.md) if you operate the platform yourself).

```sh
python -m pip install .
```

## Create a task

```sh
mkdir -p task/environment
printf '[environment]\n' > task/task.toml
printf 'FROM alpine:3.20\n' > task/environment/Dockerfile
```

For repeatable runs, pin base images by digest and keep the task source under version control. Images that need to be built or mirrored are pushed to the registry named by `[build].target` in `deployment.toml`. Flotilla relies on Docker's build cache and does not keep its own image cache.

## Inspect it locally

`inspect` parses the task and reports what Flotilla would do, without building images or creating sandboxes:

```sh
flotilla inspect task --deployment deployment.toml --out inspection.json
```

The output is one JSON object with an `environments` array. Every rejection includes a field path and a reason. `inspect` does not prove network isolation, and it only covers the task's main environment (not a separate Harbor verifier environment).

## Run commands

The [README](../README.md#run-a-task) has a complete example. `env.agent` is the main service and `env.service("db")` selects a sidecar:

```python
async with fleet.environment("task") as env:
    await env.service("db").exec(["pg_isready"], timeout=30)
    result = await env.agent.shell("curl -s http://web:8080/health", timeout=30)
```

If you need an environment handle that outlives a `with` block, use `open_task()` and release it yourself:

```python
env = await fleet.open_task("task", OpenOptions(startup_timeout=600, lifetime=300))
try:
    ...
finally:
    await env.release()
```

## Explicit environment definitions

Frameworks that select the environment themselves (such as the Harbor adapter) pass an `EnvironmentDefinition`:

```python
from pathlib import Path

from flotilla import EnvironmentDefinition, OpenOptions

definition = EnvironmentDefinition(
    task_root=Path("task"),
    directory=Path("task/environment"),
    name="example",
)
env = await fleet.open_environment(definition, OpenOptions(request_id="example-1", startup_timeout=600, lifetime=300))
```

Next: the [Runtime API](Runtime_API.md) covers execution, file transfer, timeouts, cancellation, and cleanup reports.
