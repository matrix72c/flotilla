# Quick start

This tutorial writes a two-service task, checks it locally, and runs it from Python. It assumes you have a working `deployment.toml` and the platform credential. If you don't, use the [local quick start](Local_Quick_Start.md) to set one up on a single machine, or the [deployment guide](Deployment.md) for a shared platform.

You need Linux x86_64, Python 3.12, and Docker with BuildKit on the machine that runs Flotilla, because it builds the task images.

```sh
python -m pip install .
export FLOTILLA_OPENSANDBOX_CREDENTIAL=...   # the variable named by credential_env in deployment.toml
```

## 1. Write a task

The task is an agent container (`main`) that talks to a web server (`web`):

```text
hello-web/
  task.toml
  environment/
    Dockerfile
    docker-compose.yaml
    site/
      index.html
```

```sh
mkdir -p hello-web/environment/site
printf '[environment]\n' > hello-web/task.toml
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

## 3. Run it

`run_hello.py`:

```python
import asyncio
from pathlib import Path

from flotilla import Flotilla, OpenOptions


async def main():
    async with Flotilla(deployment="deployment.toml") as fleet:
        options = OpenOptions(startup_timeout=900, lifetime=600)
        async with fleet.environment("hello-web", options) as env:
            page = await env.agent.exec(["curl", "-fsS", "http://web/"], timeout=30)
            print(page.stdout.decode().strip())

            await env.agent.write_file("/tmp/notes.txt", b"web answered\n")
            print((await env.agent.read_file("/tmp/notes.txt")).decode().strip())

            await env.service("web").download_file("/etc/nginx/nginx.conf", Path("nginx.conf"))
    print("cleanup clean:", fleet.cleanup_report.clean)


asyncio.run(main())
```

```sh
python run_hello.py
```

What happens:

1. **Preparation.** Flotilla builds `main` from the Dockerfile and pushes it to `[build].target`. `nginx:1.27-alpine` is used as is when its registry is listed in `[build].pullable_registries`, and is otherwise copied to `[build].target` first; `[build].image_replacements` can point it at an internal mirror instead. Flotilla then publishes `site/` to shared storage once; published task files are content-addressed and kept for reuse. Docker's build cache makes later runs faster; Flotilla keeps no image cache of its own.
2. **Startup.** It creates one sandbox per service, applies the network policy, writes `/etc/hosts` entries so `web` resolves from `main`, starts `web`, waits for its healthcheck, then starts `main`.
3. **Your code runs.** `env.agent` is the `main` service; `env.service("web")` selects the sidecar.
4. **Cleanup.** Leaving `fleet.environment(...)` deletes both sandboxes and waits until the platform confirms deletion. Leaving `Flotilla(...)` deletes the client's anchor sandbox and records the result in `fleet.cleanup_report`.

`startup_timeout` bounds steps 1 and 2. `lifetime` starts when the environment is ready; after it passes, the environment is released even if your code is still using it.

The first run usually spends most of its time on building and pushing images. Run the script a second time to see the startup time without the build.

## 4. Commands, files, and processes

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

## 5. Options

```python
OpenOptions(
    startup_timeout=900,
    lifetime=600,
    disable_internet=True,                           # no outbound access for any service
    resources={"main": {"cpu": "2", "memory": "4Gi"}},
    exec_env={"main": {"API_BASE": "http://proxy"}}, # added to every command in main
    runtime_params={"DB_PASSWORD": "example"},       # values for variables .env leaves undefined
    force_build=True,                                # build from the Dockerfile even if an image is set
)
```

Deployment-wide defaults for resources, timeouts, and limits live in `deployment.toml`; see the [configuration reference](Configuration.md).

## 6. Long-lived handles

If an environment must outlive a `with` block, open it and release it yourself:

```python
env = await fleet.open_task("hello-web", OpenOptions(startup_timeout=900, lifetime=600))
try:
    ...
finally:
    await env.release()
```

`release()` raises `CleanupError` with a structured report if deletion cannot be confirmed.

## Next steps

- [Runtime API](Runtime_API.md): timeouts, cancellation, `ExecOutcomeUnknown`, and cleanup reports.
- [Compose support](Compose_Support.md): every supported and rejected key, volumes, networking, and interpolation.
- [Integration guide](Integration_Guide.md): running many environments from an evaluation or RL framework.
