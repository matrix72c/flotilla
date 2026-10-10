# flotilla

Run a Compose task environment as a group of sandboxes, one sandbox per service.

flotilla translates service dependencies, networks, shared volumes, health checks, and cleanup into a platform-neutral `Platform` protocol. The first backend targets OpenSandbox. XTuner and Harbor integrations are planned.

**Status: early development / alpha.** This repository contains working library components and a deployment probe, alongside interfaces and design documents for unfinished features. A complete workflow on a public OpenSandbox deployment has not yet been validated.

## What works today

| Component | Status |
|---|---|
| Compose normalization, manifest generation, field classification | Implemented as Python library modules |
| Trial lifecycle, topology, hosts, shared-volume preparation, cleanup, runtime monitoring | Implemented with unit tests and a fake platform |
| OpenSandbox lifecycle, execution, files, CIDR policies, standard host and PVC volumes | Implemented; deployment validation remains in progress |
| Configuration, capability reports, `flotilla probe` | Implemented; probe combines measured results with explicit deployment declarations |
| Image building and task publishing | Planned; library packages and CLI commands are placeholders |
| `flotilla scan`, `gc`, and `share` commands | Planned; supporting library functionality is partially implemented |
| XTuner `FlotillaProvider` and Harbor environment adapter | Design only; adapter modules are placeholders |

The design documents describe the intended system, including features that are not implemented yet. They are not a statement of release completeness or a certification of any deployment.

## Development

The initial development target is Linux x86_64 with Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/matrix72c/flotilla.git
cd flotilla
uv sync --all-groups --frozen
uv run flotilla --help
```

The Python distribution is named `flotilla-compose`; the import package is `flotilla`. Development uses this source checkout.

Run the local checks without connecting to a sandbox platform:

```sh
uv run ruff check
uv run ruff format --check
uv run mypy
uv run lint-imports
uv run pytest
```

## Deployment probe

Start with the templates in [deployments/examples](deployments/examples/README.md). Copy them into `deployments/local/`, replace the example values, and review every capability declaration against your deployment.

`flotilla probe` creates temporary sandboxes, executes checks, and attempts to delete them before returning. It requires a configured OpenSandbox deployment, shared storage, a prepared share release containing BusyBox, and suitable anchor and probe images. It does not prepare these resources for you.

```sh
uv run flotilla probe \
  --deployment deployments/local/opensandbox.toml \
  --declared deployments/local/declared.json \
  --image '<your-registry>/flotilla:probe@sha256:<digest>' \
  --out deployments/local/capabilities.json
```

Credentials are read from the environment variable named by `opensandbox.credential_env`. Deployment files, capability reports, credentials, and operational logs should stay outside version control.

The probe currently measures a subset of the platform contract. Unmeasured properties are marked `declared`; a declaration is not an automatically verified result. Network isolation and authentication must be assessed on the actual deployment. See the [platform requirements](docs/Platform_Requirements.md).

After the probe has written a capability report, run a complete web + app + db trial on the same deployment:

```sh
uv run python -m tests.e2e.web_app_db \
  --deployment deployments/local/opensandbox.toml \
  --image '<your-registry>/flotilla:probe@sha256:<digest>' \
  --out deployments/local/e2e.json
```

The script starts the trial through the orchestration core, checks name resolution, network isolation between Compose networks, UDP, a shared trial volume, external egress, and execution-channel authentication between units, and then confirms that every instance has been deleted. It prints the report's gaps but does not refuse a deployment because of them.

Image build helpers accept a complete output image reference and an optional `--push`:

```sh
images/anchor/build.sh '<your-registry>/flotilla:anchor-dev'
images/probe/build.sh '<your-registry>/flotilla:probe-dev'
```

## Design documents

The detailed design documents are currently in Chinese.

| Document | Contents |
|---|---|
| [PRD](docs/PRD.md) | Goals, scope, requirements, and roadmap |
| [Architecture](docs/Architecture.md) | Platform protocol, lifecycle, networks, volumes, and planned integrations |
| [Platform requirements](docs/Platform_Requirements.md) | Platform capabilities C1–C16 and acceptance criteria |
| [OpenSandbox backend](docs/backends/opensandbox.md) | Implementation, configuration, and validation limits |
| [XTuner environment proposal](docs/XTuner_Environment_Design.md) | Proposed environment interface; [upstream design PR](https://github.com/InternLM/xtuner/pull/2135) |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Keep deployment-specific configuration out of the library, and update the implementation status when adding a feature.

## License

[Apache License 2.0](LICENSE). Runtime tools fetched by image build helpers retain their own licenses.
