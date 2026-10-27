# Contributing

Thanks for your interest in Flotilla. Bug reports, documentation fixes, and code contributions are all welcome. By participating you agree to follow the [code of conduct](CODE_OF_CONDUCT.md). Report security issues privately as described in [SECURITY.md](SECURITY.md), not in public issues.

## Development setup

Develop on Linux x86_64 with Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/matrix72c/flotilla.git
cd flotilla
uv sync --all-groups --frozen
```

Run the same checks as CI before opening a pull request:

```sh
uv run ruff check
uv run ruff format --check
uv run mypy
uv run lint-imports
uv run pytest
uv run --extra harbor --frozen pytest tests/unit/test_harbor_adapter.py tests/unit/test_harbor_trial.py
```

Unit tests need local asyncio sockets but no deployment, credentials, or Docker. Tests against a real deployment live in `tests/contract/` and are run explicitly by maintainers; see [OpenSandbox compatibility verification](docs/backends/opensandbox.md#compatibility-verification).

## Code structure

[Architecture](docs/Architecture.md) describes the components and terminology. A few boundaries are enforced by Ruff and import-linter:

- `core` depends only on `platform.base` and reads time through an injected `Clock`, never the wall clock.
- `compose`, `build`, and `scan` never call a real backend.
- Adapters (`integrations`), `probe`, `cli`, and the `testing` doubles are outer layers; nothing internal imports them.
- Runtime and CLI code constructs platforms through `FlotillaConfig.build_platform`, not a backend factory directly, so the runtime and every CLI command use the same settings. New platforms are usually better as a separate package; see [Writing a backend](docs/Writing_a_Backend.md).

Shared test clocks, fixtures, fake builders, and the fake platform are in `src/flotilla/testing`, packaged as the public testing surface: downstream contract tests import them from `flotilla.testing`, and runtime packages never do (enforced by import-linter). Test modules must not import other test modules. The fake platform deliberately fails on operations it does not model; extend it rather than loosening it.

## Guidelines

- Add behavior tests for fixes, especially around cancellation, uncertain outcomes, publication, and cleanup ownership.
- Comments, docstrings, messages, and documentation are written in English. Comments explain reasons and invariants rather than restating the code.
- Error messages start lowercase, have no trailing period, and never include credentials, command output, or full configuration objects.
- Update the docs and [CHANGELOG.md](CHANGELOG.md) when behavior, public API, or configuration changes.
- Changes to the support matrix (Python, Harbor, or OpenSandbox protocol versions) need acceptance evidence on a real deployment; mention this in the pull request so a maintainer can run it.

## Pull requests

1. Fork the repository and create a topic branch from `main`.
2. Keep each pull request focused on one change.
3. Fill in the pull request template: the problem, the resulting behavior, and how you validated it. For deployment-facing changes, name the protocol and scope you tested, without credentials, private endpoints, or instance IDs.
4. The `fast` CI job must pass and review conversations must be resolved before merging.

Real-platform checks never run automatically on pull requests; maintainers trigger them on trusted runners when needed. Maintainers follow [RELEASING.md](RELEASING.md) to publish releases.
