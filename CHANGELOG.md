# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/). Before 1.0, minor versions may contain breaking changes.

## [Unreleased]

### Added

- `flotilla.integrations.harbor:FlotillaTaskEnvironment`: `harbor run --env flotilla.integrations.harbor:FlotillaTaskEnvironment --ek deployment=<module:attribute>` runs one harbor trial on a `NativeAttempt`, with both grading modes and one fleet per process (`shutdown()` closes it). Tasks outside every declared dataset, regrades, and cross-process grading are rejected with the reason.
- `flotilla.testing.attempt`: `AttemptHarness`, `prepared_source`, `write_task` and `VERIFY_COMMAND` drive a real `NativeAttempt` through both grading phases on the fake platform.
- `docs/SDK.md`: the import surface of `flotilla`, `flotilla.native`, `flotilla.integrations.harbor` and `flotilla.testing`, stability commitments, and the error classification; a unit test checks each table against `__all__`. `CommandBusy` is exported at the top level.
- `NativeAttempt.end_agent_phase()`, `stage_verification()` and `open_verification_role()`: the three steps of `verify`, public, with the same order and run-once gates.
- `[platform]`: `list_visibility_s`, `max_ttl_s`, `max_label_value_len`, `link_max_members`, `implicit_egress` and `cluster_dns`, declared per deployment. `reaper.grace + platform.list_visibility_s < reaper.gc_window` and `reaper.anchor_ttl <= platform.max_ttl_s` are checked at load.
- `flotilla.compose.plan_task.plan_task` and `FlotillaConfig.plan_for(task, source, configs)`, the single way a task directory becomes a `TrialPlan`; `Deployment.plan(source)` and `Deployment.plan_environment(task, source, options)` for consumers. `TaskSource` names a task's files on the client and the platform.
- `flotilla.native.Dataset` (`tasks()`, `source(key)`), `[datasets.<name>]` and `resolve_datasets()`. `launches/` and `share/` are refused as dataset roots.
- `Flotilla.with_options` / `with_resources` apply one run's overrides to a plan resolved without them.
- `flotilla.registry.ImageConfigs` reads image configs from a registry over HTTP, by digest only, with in-memory and optional on-disk caching. The token realm comes from the registry's `WWW-Authenticate` challenge.
- `flotilla.inspect` (`inspect_source`, `sources_of`) and `flotilla inspect`: check a dataset without creating anything; nonzero exit if any task would not open.
- `Deployment.management_anchor()`: a temporary Anchor for shared-storage work outside a run.
- `flotilla.compose.verification.verification_of` / `artifacts_of`, and `ResourceDefaults.for_services`.
- Compose `extra_hosts` (literal addresses; `host-gateway` rejected), `links`, `volumes_from`, top-level `x-*` fields, and `devices: /dev/fuse`. **Breaking** for deployments setting `fuse` in a static extensions table: that key is now written per instance and rejected there.
- `flotilla.Deployment`: one consumer entry point for planning, datasets, clients, credentials binding (`bind_credentials` reads only `credential_env`), validation, capacity and runtime layers.
- Caller-owned external read-only mounts (`OpenOptions.external_mounts`), excluded from trial volume lifecycle management.
- Backends are selected with `backend = "<name>"` and configured in `[backends.<name>]`; another backend is registered in the in-tree `BACKENDS` table.
- Opt-in read-only root filesystems, native tmpfs and `shm_size` on the `sandbox-system-v1` filesystem dialect, verified per instance against the effective configuration.
- Compose `privileged: true` (warning) and `devices` `/dev/kvm` / `/dev/net/tun` (with `cap_add: [NET_ADMIN]` for TUN), run on the privileged runtime with per-instance device extensions.
- Task-level `network_mode = "no-network"` / `allow_internet = false`, explicit cluster DNS declarations, and opt-in sandbox ingress enforcement; Compose `network_mode: none` checks outbound exceptions and inbound isolation.
- Correlated execd request timing logs, without command contents or credentials.
- Compose `${VAR:+alt}` / `${VAR+alt}`, nested references in defaults, and Harbor's `CONTEXT_DIR`.

### Changed

- User commands (`exec` with an `ExecAttempt`) never reuse an HTTP connection: the workload pool keeps no idle connections, so a connection pinned to a proxy backend that stopped answering cannot wedge later commands in the same process. The execd end log gains `phases=` offsets (`connect`, `connected`, `sent`, `status_wait`, `status`), which locate where a request that ended without a status line stopped.
- `Deployment.setup_budget()` is the per-phase timeouts' sum capped by `runtime.startup_timeout_s`, the bound one open really has.
- The import contracts pin the current layer graph: `models` at the bottom, `compose` offline, the runtime free of `compose` and `native`, `native` above both.
- **Breaking:** the shared vocabulary lives in `flotilla.models`. `flotilla.core.plan` → `flotilla.models.plan`; `flotilla.native.models` → `flotilla.models.verification` and `flotilla.models.paths`; `flotilla.platform.filesystem` → `flotilla.models.filesystem`; the value objects and errors of `flotilla.platform.base` → `flotilla.models.platform` (still re-exported from `platform.base`). The top-level `flotilla` surface is unchanged.
- **Breaking:** `flotilla.integrations.harbor_source.task_environments` is `flotilla.compose.harbor_source.task_environments`.
- **Breaking:** `harbor` is a required dependency (`harbor>=0.26.0,<0.27`, from PyPI); the `harbor` extra is gone.
- **Breaking:** a deployment is a `FlotillaConfig` the caller constructs; `deployment.toml` is gone. Commands, `Deployment.load` and `Flotilla(deployment=...)` take `package.module:attribute`. `deployments/examples/opensandbox.toml` is `opensandbox.py`; `flotilla.testing.config.importable(spec)` publishes a spec for tests.
- **Breaking:** client-side paths in a spec (`datasets.<name>.local_root`, `agent_runtime.layers.<name>.local_root`) must be absolute.
- **Breaking:** `flotilla probe` measures only isolation and writes no report. `probe()` returns an `IsolationEvidence` of seven properties and their privileged counterparts; `--out` is gone.
- **Breaking:** the isolation gate is the probe's exit status. `FlotillaConfig.check_isolation` runs in the probe; clients no longer re-check a report. `EnvironmentInfo.security_gaps` is replaced by `accepted_security_gaps`.
- **Breaking:** flotilla reads Compose itself (`flotilla.compose.parse.load_project`) and no longer runs `docker compose config`. `tests/contract/compose_differential.py` compares the two readers; a bare `$VAR` and a `$` in an env file value are rejected.
- A `root`/`0` image `USER` or Compose `user` resolves to 0:0 without reading the image's `/etc/passwd`.
- Every rejected Compose field carries a category (`unimplemented`, `platform-gap`, `no-model`, `out-of-scope`, `unsafe`, `not-reproducible`, `docker-internal`), checked against the support table. Fields the sandbox API has no counterpart for (`cap_drop`, `security_opt`, `ulimits`, `sysctls`, `group_add`, `pid`, `ipc`, ...) say so, and `pids_limit`, `dns*`, `platform`, `memswap_limit` and `cgroup` are listed.
- `[share]` is optional; the built-in release `busybox-1.36.1-r31` is published from the Anchor image's BusyBox when missing.
- `[agent_runtime].layers.<name>.local_root` is optional.
- Compose fields that only change local Docker behavior (`init`, `logging`, `stop_signal`, `stop_grace_period`, `pull_policy`, `ports`, network `gw_priority`, bridge options, `depends_on.*.restart`) are recorded instead of rejected.
- `flotilla gc` finds launches from sandbox labels as well as `launches/`; without an anchor it deletes sandboxes by label only and exits 1. `--staging-age` bounds staging cleanup.
- `CleanupRecord` gains `reason` and `error`.
- **Breaking:** an open's budgets are split by phase: `OpenOptions.admission_timeout` (default `limits.admission_timeout`) bounds the admission queue and raises `AdmissionTimeout`; `startup_timeout` starts at admission.
- The share check at client start reads only `RELEASE.json` and confirms `bin/busybox`.
- `flotilla probe` and `gc` build their platform through the runtime's factory, so `opensandbox.privileged_extensions` applies to them too.
- **Breaking:** tmpfs requires native support; the empty-directory approximation is gone.

### Removed

- **Breaking:** native `harbor run` (`flotilla.integrations.harbor_native`), superseded by `FlotillaTaskEnvironment`.
- **Breaking:** `FlotillaConfig.base_dir` and `to_mapping()`.
- **Breaking:** the capability report (`capabilities.json`, `CapabilityReport`, its sections and states) and the functional probe checks.
- **Breaking:** the release, the manifest and the prepare stage (`flotilla.manifest`, `flotilla.prepare`); a task directory becomes a trial in the process that runs it.
- **Breaking:** task files are mounted read-only where they are published instead of being copied under a launch tree.
- **Breaking:** the Compose adapter (`compose/adapter.py`, `ComposeConfigAdapter`) and the `compose_adapter` arguments.
- **Breaking:** named volumes start empty instead of emulating Docker's copy-up.
- **Breaking:** flotilla no longer builds, adapts or publishes images. Compose `build:` is rejected (`out-of-scope`), and a non-root image `USER` is rejected instead of adapted.
- **Breaking:** the legacy Harbor source-task API (`HarborAttempt`, `HarborVerification`, `prepare/runner.py`, `share/publish.py`).
- **Breaking:** deferred interpolation (`runtime/values.py`, `RuntimeExpression`) and `OpenOptions.runtime_params`; references are resolved when the task is read.
- **Breaking:** image inspection of tmpfs and read-only mount targets during preparation.
- **Breaking:** the CLI is `inspect`, `probe` and `gc`; the other subcommands are gone.
- Unreachable symbols: the `Backend.profile` / `FlotillaConfig.filesystem_profile` chain, `compile_host_volumes`, `compile_pvc_volumes`, `derive_dockerfile`, `share.release.release_json`.

### Fixed

- A plan resolved beforehand keeps its run's `OpenOptions` (`resources`, `external_mounts`, `workspace_mounts`).
- A verifier reads the agent's handoff at a declared target (`/handoff/agent`).
- A declared named volume no service mounts is dropped, as Compose does.
- An undefined Compose variable without a default is refused instead of substituted with a blank string.
- `flotilla inspect` reads a task exactly as a run does.
- Read-only anchor `stat` recovers transient or unknown transport responses (at most five attempts, 30-second window) without replaying writes.
- Healthchecks propagate non-retryable execution errors, including unknown outcomes.
- A service with both `service_healthy` and `service_completed_successfully` dependents drives both conditions.
- A sandbox no longer expires while its environment is still starting.
- Environment variable names Docker accepts but shells do not (`discovery.type`) are passed through.
- A sandbox the platform never starts is recreated once after `timeouts.pending` (new, default 300 s) instead of holding its trial for `timeouts.create`.
- Commands waiting for a free HTTP connection are not counted as sent and do not run down their timeout.
- A lost create response is recovered after `timeouts.create_request` (new, default 60 s) by label lookup.
- A confirmed timeout of the internal health probe is retried within the check budget.
- execd command-stream `409 CONFLICT` is an explicit `CommandBusy` rejection.
- Network policy replacement sends the sandbox's execd access token (upstream returned 401).
- Command output from execd 1.1.1+ no longer doubles line terminators.

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
