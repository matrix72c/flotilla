# Contributing

flotilla is under active development. Please open an issue for substantial interface changes and keep pull requests focused.

Use Python 3.12 on Linux x86_64:

```sh
uv sync --all-groups --frozen
uv run ruff check
uv run ruff format --check
uv run mypy
uv run lint-imports
uv run pytest
```

Unit tests use a fake platform and require no deployment credentials. Add tests for meaningful behavior changes; real deployment validation is separate.

The orchestration core depends only on `flotilla.platform.base`. It uses the injected `Clock` for time and waits. Offline Compose and manifest processing must not call a platform. Import-linter and Ruff enforce these boundaries.

Keep deployment configuration under the ignored `deployments/local/` directory. Do not commit credentials, capability reports from live deployments, instance identifiers, operational logs, or private registry addresses. Examples must use placeholders and describe their prerequisites.

Update the README status table when a placeholder becomes functional. Design documents may describe planned behavior, but user-facing instructions must reflect the implementation.
