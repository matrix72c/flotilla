# Flotilla

Flotilla runs a Docker Compose task environment on a remote sandbox platform, one sandbox per Compose service. It builds or resolves the service images, publishes task files to shared storage, connects the services under an explicit network policy, and confirms that every sandbox and directory it created has been deleted.

Use it as an async Python library or as an environment plugin for the [Harbor](https://github.com/harbor-framework/harbor) agent evaluation framework (not the Harbor container registry). The current backend is [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox).

## Why

Agent training and evaluation tasks are often written as Compose projects: an agent container plus databases, web servers, or other sidecars. Running thousands of them concurrently on a single Docker host does not scale, and a sandbox platform usually gives you single containers rather than Compose. Flotilla maps a Compose project onto a group of sandboxes and adds the parts that are easy to get wrong at scale:

- **Explicit isolation.** Network policies are compiled from the Compose networks and the deployment configuration. Security properties the deployment cannot demonstrate must be explicitly accepted.
- **Confirmed cleanup.** Sandboxes are labeled and tracked. A lost create response is reconciled, and capacity is only released after deletion is confirmed. A crashed client's resources can be collected later with `flotilla gc`.
- **Safe execution semantics.** Commands are never replayed automatically. A timeout means the remote process was confirmed stopped; anything else is reported as an unknown outcome.
- **Early rejection.** Unsupported Compose features are rejected with a field path before any sandbox is created.

## Status

Flotilla is alpha software. The supported target is **Linux x86_64 and Python 3.12**, with the OpenSandbox `current` and `legacy` execd protocols. The optional Harbor adapter targets **Harbor 0.24.0**. APIs and configuration may change between minor versions before 1.0; see the [changelog](CHANGELOG.md).

## Requirements

- An OpenSandbox deployment, with shared storage that every sandbox worker can mount. See [Platform requirements](docs/Platform_Requirements.md).
- On the machine that runs Flotilla: Docker with BuildKit/buildx, push access to an image registry, and network access to the OpenSandbox server.
- The sandbox platform must be able to pull the images from that registry.

## Install

```sh
git clone https://github.com/matrix72c/flotilla.git
cd flotilla
python -m pip install .
# With the Harbor adapter:
python -m pip install '.[harbor]'
```

The distribution is named `flotilla-compose`; the import name and CLI are `flotilla`.

## Run a task

Running a task requires a deployment configuration (`deployment.toml`). If someone operates the platform for you, ask them for it. Otherwise, set one up once with the [deployment guide](docs/Deployment.md).

A minimal task looks like this:

```text
task/
  task.toml            # may contain an [environment] table
  environment/
    Dockerfile         # e.g. FROM alpine:3.20
```

Multi-service tasks add `environment/docker-compose.yaml`. See [Compose support](docs/Compose_Support.md) for what is accepted.

```python
import asyncio

from flotilla import Flotilla, OpenOptions


async def main():
    async with Flotilla(deployment="deployment.toml") as fleet:
        async with fleet.environment("task", OpenOptions(startup_timeout=600, lifetime=300)) as env:
            result = await env.agent.exec(["/bin/sh", "-c", "echo hello"], timeout=10)
            print(result.stdout.decode())
            assert result.exit_code == 0 and not result.timed_out
    assert fleet.cleanup_report is not None and fleet.cleanup_report.clean


asyncio.run(main())
```

`startup_timeout` covers parsing, image preparation, file publication, and sandbox startup. `lifetime` starts when the environment is ready. Leaving either context manager waits until cleanup is confirmed; if it cannot be confirmed, `CleanupError` is raised with a structured report.

## Documentation

| Guide | For |
|---|---|
| [Library quick start](docs/Quick_Start.md) | Running tasks from Python |
| [Runtime API](docs/Runtime_API.md) | Execution, file transfer, timeouts, cancellation, errors |
| [Harbor quick start](docs/Harbor_Quick_Start.md) | Running Harbor evaluations on Flotilla |
| [Compose support](docs/Compose_Support.md) | Which Compose features are supported or rejected |
| [Deployment guide](docs/Deployment.md) | Setting up a deployment once |
| [Configuration reference](docs/Configuration.md) | `deployment.toml` and `declared.json` fields |
| [OpenSandbox backend](docs/backends/opensandbox.md) | execd protocols, storage, and network modes |
| [Platform requirements](docs/Platform_Requirements.md) | What a deployment must provide and how it is verified |
| [Network validation](docs/Network_Validation.md) | Verifying isolation on a real deployment |
| [Architecture](docs/Architecture.md) | Internals and terminology |

## Contributing

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md). This project follows a [code of conduct](CODE_OF_CONDUCT.md).

## License

Flotilla is licensed under the [Apache License 2.0](LICENSE). The anchor image includes BusyBox, which is licensed under GPLv2; see [third-party components](THIRD_PARTY.md).
