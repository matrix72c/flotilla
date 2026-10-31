# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). Before 1.0, minor versions may contain breaking changes.

## [Unreleased]

### Added

- `prepare.export_timeout` (default 600 s, the previous fixed value) bounds each `docker cp` that exports an image volume's initial content, so tasks with large volumes can be prepared. Export failures now include docker's error output.
- `flotilla prepare`: builds images and publishes task files once per task, offline, and writes a jsonl with one row per task (`load_prepared`, `PreparedTask`, `PreparedEnvironment`). Rows carry filter attributes and the digest-pinned main and separate verifier environments; filtering a dataset is filtering lines. Interrupted runs resume, `--force` rebuilds, and each dataset's images go to `<build.target>/<dataset>`. Task files are published flat under `tasks/<dataset>/<task>/<role>`.
- Pluggable sandbox backends. A package registers a `flotilla.backends.Backend` subclass in the `flotilla.backends` entry-point group; a deployment selects it with `backend = "<name>"` and configures it in `[backends.<name>]`. The built-in OpenSandbox backend keeps its `[opensandbox]` table, and existing configurations and capability-report fingerprints are unchanged. See [Writing a backend](docs/Writing_a_Backend.md).
- Opt-in internal lifecycle support for Compose read-only root filesystems and native tmpfs, with independent behavioral probes, effective-configuration verification, private framework scratch mounts, and strict upstream-compatible request defaults.
- Compose `shm_size` on the internal filesystem dialect, sent per instance as the `sandbox-system.io/shm-size` extension, sharing the tmpfs memory budget, gated by the new `probe_shm_size` measurement, and verified per instance against the size the platform echoes before startup. Upstream OpenSandbox has no equivalent setting and rejects such tasks.

- Task-level `network_mode = "no-network"` and `allow_internet = false`, preserving service links while removing external access throughout preparation and execution.
- Explicit cluster DNS declarations: offline policies tolerate only the declared resolver IPs on TCP/UDP 53 and reject every other implicit egress exception.
- Opt-in sandbox ingress enforcement using the shared OpenSandbox lifecycle route. Compose `network_mode: none` now checks both outbound exceptions and inbound isolation; upstream requests retain the standard egress-only schema by default.
- Correlated execd request timing logs for send, response headers, command ID, and stream closure, without command contents or credentials.
- Compose `${VAR:+alt}` / `${VAR+alt}` and nested references inside defaults and alternatives, plus Harbor's `CONTEXT_DIR` interpolation variable (the environment directory).

### Changed

- `flotilla gc` finds launches from sandbox labels as well as `launches/`, and when its anchor cannot start it deletes sandboxes by label only (`GcReport.mode = "platform"`, `directories_skipped`) and exits 1. With an anchor it also removes `tasks/.staging`, `tasks/.trash` and `share/staging` entries older than `--staging-age` (default 24 h). Share release staging names now carry a timestamp.
- `CleanupRecord` gains `reason` and `error`, naming what is still unconfirmed and the last related error.
- **Breaking:** an open's budgets are split by phase. Waiting for a `trial_concurrency` / `instance_budget` slot is bounded by `OpenOptions.admission_timeout` (default `limits.admission_timeout`, 3600 s) and raises the new `AdmissionTimeout` (a `TimeoutError`) with the current counts. `startup_timeout` now starts at admission, so queueing no longer consumes it. Client start (anchor and share check) is bounded by the new `timeouts.client_start` (300 s) instead of `timeouts.prepare`.
- The share check at client start downloads only `RELEASE.json`, compares its sha256 and confirms `bin/busybox`. Per-file verification stays in `flotilla share verify`.
- **Breaking:** opening no longer prepares anything. `fleet.open(prepared)` / `fleet.environment(prepared)` take a `PreparedEnvironment` from a prepared row and run no Docker, builds, or uploads; `open_task`, `open_environment`, `inspect`, `inspect_environment`, `EnvironmentDefinition`, `EnvironmentInspection`, `OpenOptions.force_build`, `PreparationResidual` and `CleanupReport.preparation_residuals` are removed. Use `fleet.reservation(prepared)` for admission budgets and `flotilla inspect` for Docker-free checks.
- **Breaking:** `[preparation]` is removed and rejected with a migration message: `docker_command` and `concurrency` move to `[prepare]`, and `timeout_s` becomes `runtime.startup_timeout_s` (default 1800 s), the default open budget.
- **Breaking:** the Harbor adapter takes `--ek prepared=<jsonl>` and opens the row's environment for the environment directory Harbor gives it; `force_build` is rejected.
- `flotilla probe`, `gc`, and `share` construct their platform through the same factory as the runtime, so `opensandbox.privileged_extensions` now also applies to privileged instances these commands create.
- tmpfs now requires native support; the previous empty-directory approximation is removed without fallback. Private preparation manifests use schema 5 and must be regenerated.

### Fixed

- A sandbox no longer expires while its environment is still starting. Sandboxes are renewed only once the environment is ready, but they were created with `reaper.trial_ttl` (1800 s by default), so an open whose startup ran past that TTL (for example `startup_timeout` above 1800 s, or long health checks) had its sandboxes reclaimed by the platform mid-start and failed with "instance vanished during start". Each sandbox is now created with at least the open's startup budget plus `reaper.renew_before_s`.
- Environment variable names that are not shell identifiers but that Docker accepts (for example `discovery.type` or `APT::Get::AllowUnauthenticated`) are no longer rejected. The OpenSandbox exec wrapper passes them to execd under internal `FLOTILLA_ENV_<n>` aliases and restores the original names when it clears the environment. Values still travel only in the request `envs`. Names that are empty, contain `=` or NUL, start with `-`, or start with `FLOTILLA_ENV_` are still rejected.
- A sandbox the platform never starts no longer holds its trial and quota for the whole `timeouts.create` budget. After `timeouts.pending` (new, default 300 s) it is deleted and created again once, through the existing recreate path; a second stall fails the open.
- Commands waiting for a free HTTP connection are no longer counted as sent and no longer run down their timeout while they wait. Previously a burst of internal commands (for example every environment's health check at once) queued inside the 100-connection pool, expired there, and was reported as an unknown outcome, so healthy environments were marked lost. Requests now wait in Flotilla for one of `opensandbox.max_connections` (new, default 100) connections per pool.
- A create request whose response was lost no longer holds its trial for the whole `timeouts.create` budget. The request now has its own `timeouts.create_request` (default 60 s), after which the sandbox is found by its labels and adopted; previously the request timeout equalled the stage timeout, so this recovery could never run.
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
