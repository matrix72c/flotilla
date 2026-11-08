# Quick start

This tutorial writes a two-service Harbor task, checks it locally, stages its original directory on platform storage, and runs an isolated attempt from Python. It assumes you have a working `deployment.toml` and the platform credential. If you don't, use the [local quick start](Local_Quick_Start.md) to set one up on a single machine, or the [deployment guide](Deployment.md) for a shared platform.

You need Linux x86_64 and Python 3.12 with the `harbor` extra. The platform management image must contain the same Flotilla version, pinned Compose (default 2.39.4), and build tools. Configure `prepare.management_network`, read-only `management_mounts` for credentials, and nonsecret `management_env` such as `DOCKER_HOST`/`DOCKER_CONFIG` explicitly for the remote build service. No automatic DinD or unrestricted egress is enabled. This redesign has not completed real platform acceptance and has no compatible published management image yet; the old anchor image is insufficient.

```sh
python -m pip install '.[harbor]'
export FLOTILLA_OPENSANDBOX_CREDENTIAL=...   # the variable named by credential_env in deployment.toml
```

## 1. Write a task

The task is an agent container (`main`) that talks to a web server (`web`):

```text
hello-web/
  instruction.md
  tests/test.sh
  task.toml
  environment/
    Dockerfile
    docker-compose.yaml
    site/
      index.html
```

```sh
mkdir -p hello-web/environment/site hello-web/tests
printf '[environment]\n' > hello-web/task.toml
printf 'Fetch the web page.\n' > hello-web/instruction.md
printf '#!/bin/sh\nset -eu\ncurl -fsS http://web/ >/dev/null\n' > hello-web/tests/test.sh
printf 'FROM alpine:3.20\nRUN apk add --no-cache curl\n' > hello-web/environment/Dockerfile
printf '<h1>hello from web</h1>\n' > hello-web/environment/site/index.html
```

`hello-web/environment/docker-compose.yaml`:

```yaml
services:
  main:
    depends_on:
      web:
        condition: service_healthy
  web:
    image: nginx:1.27-alpine
    volumes:
      - ./site:/usr/share/nginx/html:ro
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "/dev/null", "http://localhost/"]
      interval: 2s
      retries: 15
```

A few rules are worth knowing from the start (the full list is in [Compose support](Compose_Support.md)):

- The agent service is always named `main`. When `environment/Dockerfile` exists, `main` is built from it, and its default command is `sleep infinity`, so it stays up for you to run commands in.
- Bind-mount sources must be inside `environment/`. A source that is only mounted read-only, like `./site` here, is published once with the task files and mounted read-only.
- Services on a shared network reach each other by service name. Ports don't need to be published.

## 2. Inspect it

`inspect` parses the task with the deployment's capabilities and reports what Flotilla would do. It builds nothing and creates no sandboxes:

```sh
flotilla inspect hello-web --deployment deployment.toml
```

The output is a JSON object with an `environments` array. `"status": "accepted"` means the task can run on this deployment. Each finding has a field path, a kind (`equivalent`, `warn`, or `reject`), and a reason. One `reject` finding rejects the task. For example, publishing a port range gives:

```json
{"path": "services.web.ports[0]", "kind": "reject", "reason": "target must be one numeric port; ranges are unsupported"}
```

## 3. Stage the original task; optionally warm it

Copy the complete `hello-web/` directory to `datasets/demo/hello-web` below the configured platform storage root using your deployment's storage tooling. Preserve modes, environment files, instruction and test directories. Training clients do not need that local directory. Do not put sources under reserved `share/`, `tasks/`, or `launches/` trees.

```sh
flotilla prepare datasets/demo/hello-web --deployment deployment.toml --out diagnostics.json
```

This optional command exercises platform-side preparation and emits diagnostics. It is not a prerequisite or a prepared JSONL training dataset. Every `HarborAttempt` reads the original platform task into its own owned workspace, compiles the selected roles there, and uses the configured image build/cache facilities. Do not mutate source directories during experiments; version their storage keys instead.

## 4. Run it

`run_hello.py`:

```python
import asyncio
from pathlib import Path

from flotilla import Flotilla, OpenOptions
from flotilla.integrations.harbor_attempt import HarborAttempt


async def main():
    async with Flotilla(deployment="deployment.toml") as fleet:
        options = OpenOptions(startup_timeout=900, lifetime=600)
        attempt = await HarborAttempt.create(fleet, "datasets/demo/hello-web")
        try:
            env = await attempt.open("main", options)
            page = await env.agent.exec(["curl", "-fsS", "http://web/"], timeout=30)
            print(page.stdout.decode().strip())

            await env.agent.write_file("/tmp/notes.txt", b"web answered\n")
            print((await env.agent.read_file("/tmp/notes.txt")).decode().strip())

            await env.service("web").download_file("/etc/nginx/nginx.conf", Path("nginx.conf"))
        finally:
            await attempt.close()
    print("cleanup clean:", fleet.cleanup_report.clean)


asyncio.run(main())
```

```sh
python run_hello.py
```

What happens:

1. **Startup.** Flotilla creates one sandbox per service, applies the network policy, writes `/etc/hosts` entries so `web` resolves from `main`, starts `web`, waits for its healthcheck, then starts `main`.
2. **Your code runs.** `env.agent` is the `main` service; `env.service("web")` selects the sidecar.
3. **Cleanup.** `attempt.close()` deletes role sandboxes and the attempt workspace, waiting for confirmed cleanup. Leaving `Flotilla(...)` deletes the client's anchor sandbox and records the result in `fleet.cleanup_report`.

`startup_timeout` bounds step 1, counted from when the open gets an admission slot; waiting for a slot is bounded by `admission_timeout`. `lifetime` starts when the environment is ready; after it passes, the environment is released even if your code is still using it.

Attempt creation may compile/build on the platform; runtime service startup is a separate stage. Source, hidden tests and handoff artifacts do not round-trip through the training machine. Native Harbor CLI/Trial is not extended to accept `task_key`.

## 5. Commands, files, and processes

```python
# argv, no shell
await env.agent.exec(["ls", "-l", "/"], timeout=10)
# an explicit shell, with a working directory and extra environment variables
await env.agent.shell("echo $GREETING > out.txt", cwd="/tmp", env={"GREETING": "hi"}, timeout=10)
# run as another user
await env.agent.exec(["id"], uid=1000, gid=1000, timeout=10)

# move local files and directories in and out
await env.agent.upload_dir(Path("fixtures"), "/srv/fixtures")
await env.agent.download_dir("/srv/fixtures", Path("fixtures-copy"))

# background processes
worker = await env.agent.start_process(["sh", "-c", "while sleep 1; do date >> /tmp/ticks; done"])
status = await worker.wait(timeout=2)  # stops waiting; the process keeps running
await worker.stop()                    # returns once termination is confirmed
```

`exec` returns `ExecResult(exit_code, stdout, stderr, timed_out)` with bytes output. A nonzero exit code is a normal result, not an exception.

## 6. Options

```python
OpenOptions(
    startup_timeout=900,
    lifetime=600,
    disable_internet=True,                           # no outbound access for any service
    resources={"main": {"cpu": "2", "memory": "4Gi"}},
    exec_env={"main": {"API_BASE": "http://proxy"}}, # added to every command in main
    runtime_params={"DB_PASSWORD": "example"},       # values for variables .env leaves undefined
)
```

Deployment-wide defaults for resources, timeouts, and limits live in `deployment.toml`; see the [configuration reference](Configuration.md).

## 7. Long-lived handles

If an environment must outlive a `with` block, open it and release it yourself:

```python
attempt = await HarborAttempt.create(fleet, "datasets/demo/hello-web")
env = await attempt.open("main", OpenOptions(startup_timeout=900, lifetime=600))
try:
    ...
finally:
    await attempt.close()
```

`release()` raises `CleanupError` with a structured report if deletion cannot be confirmed.

## Next steps

- [Runtime API](Runtime_API.md): timeouts, cancellation, `ExecOutcomeUnknown`, and cleanup reports.
- [Compose support](Compose_Support.md): every supported and rejected key, volumes, networking, and interpolation.
- [Integration guide](Integration_Guide.md): running many environments from an evaluation or RL framework.
