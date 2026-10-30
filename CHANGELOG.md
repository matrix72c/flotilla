# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). Before 1.0, minor versions may contain breaking changes.

## [Unreleased]

### Added

- Pluggable sandbox backends. A package registers a `flotilla.backends.Backend` subclass in the `flotilla.backends` entry-point group; a deployment selects it with `backend = "<name>"` and configures it in `[backends.<name>]`. The built-in OpenSandbox backend keeps its `[opensandbox]` table, and existing configurations and capability-report fingerprints are unchanged. See [Writing a backend](docs/Writing_a_Backend.md).
- Opt-in internal lifecycle support for Compose read-only root filesystems and native tmpfs, with independent behavioral probes, effective-configuration verification, private framework scratch mounts, and strict upstream-compatible request defaults.
- Compose `shm_size` on the internal filesystem dialect, sent per instance as the `sandbox-system.io/shm-size` extension, sharing the tmpfs memory budget, gated by the new `probe_shm_size` measurement, and verified per instance against the size the platform echoes before startup. Upstream OpenSandbox has no equivalent setting and rejects such tasks.

- Task-level `network_mode = "no-network"` and `allow_internet = false`, preserving service links while removing external access throughout preparation and execution.
- Explicit cluster DNS declarations: offline policies tolerate only the declared resolver IPs on TCP/UDP 53 and reject every other implicit egress exception.
- Opt-in sandbox ingress enforcement using the shared OpenSandbox lifecycle route. Compose `network_mode: none` now checks both outbound exceptions and inbound isolation; upstream requests retain the standard egress-only schema by default.
- Correlated execd request timing logs for send, response headers, command ID, and stream closure, without command contents or credentials.
- Compose `${VAR:+alt}` / `${VAR+alt}` and nested references inside defaults and alternatives, plus Harbor's `CONTEXT_DIR` interpolation variable (the environment directory).

### Changed

- `flotilla probe`, `gc`, and `share` construct their platform through the same factory as the runtime, so `opensandbox.privileged_extensions` now also applies to privileged instances these commands create.
- tmpfs now requires native support; the previous empty-directory approximation is removed without fallback. Private preparation manifests use schema 5 and must be regenerated.

### Fixed

- Preserve non-root image `USER` for workload execution while preparing a root-default runtime image for the exec daemon, including linked and mirrored source images.
- Retry confirmed timeouts of the internal no-op health probe within the existing check budget instead of immediately losing the environment; user commands are never replayed.
- Treat execd command-stream `409 CONFLICT` responses as explicit `CommandBusy` rejections: healthchecks skip that round, foreground and background calls preserve the rejection, and commands are never automatically replayed. Other conflicts retain their existing classification.
- Send each sandbox's execd access token when replacing its network policy. Upstream OpenSandbox's Docker runtime serves `PUT /sandboxes/{id}/networkpolicy` through execd's proxy and rejected the unauthenticated request with HTTP 401, so linking multi-service environments failed.
- Stop doubling line terminators in command output from execd 1.1.1 and later, which keeps each line's terminator in stdout/stderr events.

## [0.1.1] - 2026-10-20

Initial public release plus downstream orchestration contracts.

### Added

- Async Python API (`Flotilla`, `open_task`, `open_environment`, `environment`) for running a Compose task as one sandbox per service, with confirmed cleanup reports.
- Command execution with explicit argv, identity, and environment; background processes; binary file and directory transfers.
- Cancellation and timeout semantics that never replay commands and report unknown outcomes as `ExecOutcomeUnknown`.
- Automatic image preparation with Docker BuildKit, including digest resolution, mirroring, and derived images.
- Explicit network policies (`none`, allowlist, `any`) with CIDR or sandbox-ID peer links.
- OpenSandbox backend supporting the `current` and `legacy` execd protocols.
- CLI: `inspect`, `probe`, `gc`, and `share publish|verify|gc`.
- Optional Harbor 0.24.0 environment adapter (`flotilla.integrations.harbor:FlotillaEnvironment`).

- Environment-specific inspection and public resource reservations, including per-client anchor overhead.
- Process waits with bounded observation timeouts, and explicit stop with confirmed termination.
- Public `flotilla.testing` helpers for downstream contract tests, separate from runtime imports.
