# SDK surface

What a consumer, and an extension package built on Flotilla, may import -- and what each side
may rely on. The surface is `import flotilla` for the runtime, `flotilla.native` for graded
attempts, `flotilla.integrations.harbor` for running harbor trials, and `flotilla.testing` for
contract tests. Modules below those are internal: the import contracts in `pyproject.toml` pin
the layer graph, not module layouts, so deep imports move without notice.

## Imported from `flotilla`

| Symbol | What it is |
|---|---|
| `Deployment` | Consumer entry point: planning, datasets, clients, validation, capacity, runtime layers |
| `Flotilla` | The runtime client: one per process, owns renewal, monitoring, cleanup |
| `OpenOptions` | What one run decides: timeouts, resources, mounts, exec environment |
| `Environment` | One running task: a group of sandboxes, one per service |
| `Service` | One service of an environment: exec, file transfer, health |
| `Process` | A background process started on a service |
| `TaskSource` | Where one task's files are, on the client and on the platform |
| `TrialPlan` | What a task resolves to: everything one trial runs, in memory |
| `DatasetPeak` | Per-role maxima over a dataset, for sizing concurrency |
| `TaskValidation` | One task's validation outcome; failures reported, not raised |
| `RuntimeLayers` | The deployment's runtime layers as storage keys |
| `EnvironmentInfo` | Resource metadata of an opened environment |
| `ServiceInfo` | One service's metadata inside `EnvironmentInfo` |
| `ExecResult` | A foreground exec's outcome: exit code, output, timeout flag |
| `ProcessStatus` | A background process's status, by process ID |
| `ErrorCategory` | Why a platform call failed; the core's retry decisions use it |
| `FlotillaError` | Platform-side failure, carrying `stage`, `category`, `retryable` |
| `CommandBusy` | The exec channel rejected a command before accepting it |
| `ExecOutcomeUnknown` | An exec may have taken effect; never a result, never replayed |
| `AdmissionTimeout` | The admission queue's budget ran out, with the current counts |
| `CleanupError` | Cleanup left resources behind; carries the `CleanupReport` |
| `CleanupReport` | What a close released and what it could not |
| `ResourceReservation` | The admission budget of one environment, locally computed |
| `RuntimeEvent` | One diagnostic event, delivered best-effort to `on_event` |

## Imported from `flotilla.native`

| Symbol | What it is |
|---|---|
| `NativeAttempt` | One private attempt at a graded task, in two phases: the agent's, then grading |
| `VerificationResult` | The grading outcome: exit code, output, collected outputs |
| `Dataset` | A directory of task directories, as the client and the platform see it |
| `DatasetError` | A dataset root is unusable: not a directory, not on declared storage, no tasks |
| `datasets` | Resolve the deployment's declared datasets against their storage roots |
| `platform_key` | Map a host path or PVC identity to a storage key; never guess a prefix |
| `Artifact` | An output directory the agent writes, handed to grading afterwards |
| `Verification` | How an attempt is graded: what runs, where, which outputs are collected |

`NativeAttempt` and `VerificationResult` import lazily, so metadata-only use (datasets,
artifacts, verification shapes) does not load the scheduler.

## Imported from `flotilla.integrations.harbor`

| Symbol | What it is |
|---|---|
| `FlotillaTaskEnvironment` | The environment a harbor trial runs on: owns the `NativeAttempt`, both grading modes, and the per-process fleet |
| `FlotillaTaskOptions` | Its `--ek` options: `deployment` as `module:attribute` |
| `FlotillaEnvironment` | A borrowed view over one live handle: exec, file transfer, service stops |
| `shutdown` | Close every attempt and fleet this process opened; harbor's CLI may rely on exit instead |

The module is not re-exported at the top level -- `import flotilla` stays harbor-free -- and
its names import harbor itself, which is a required dependency of the distribution.

## `flotilla.testing`

Contract-test doubles for downstreams, shipped in the wheel behind the `testing` extra:
fake clocks, a fake registry (`FakeConfigs`), task-tree planning fixtures, a fake platform
(`FakePlatform`, `SharedTree`), and an attempt harness (`AttemptHarness`, `prepared_source`)
that drives a real `NativeAttempt` -- both grading phases included -- on that platform. The
runtime never imports it. See [the integration guide](Integration_Guide.md).

## Stability

Before 1.0, minor versions may contain breaking changes; every one of them lands in the
[CHANGELOG](../CHANGELOG.md) with a breaking marker. Within that, the commitments below are what
an extension can plan around:

- The tables above. Names are added without notice; a removal or a signature change is a
  changelog entry, and every table is checked against its package's `__all__` by a unit
  test, so the doc and the surface cannot drift apart silently.
- The grading phase contract: `open` during the agent phase, then `end_agent_phase`,
  `stage_verification`, `open_verification_role` in that order, each once, with `verify` the
  default composition. An extension that drives the steps itself today keeps working when
  the internals change.
- The layer graph: `models` at the bottom, `compose` offline, the runtime free of compose and
  native, native above both. The import contracts enforce it in CI, so an extension importing
  from a layer finds the same layer below it in every version.

## Errors: whose fault, and what to do

Grading returns an outcome; infrastructure fails by raising. A `VerificationResult.exit_code`
is the task's own result -- including nonzero -- and never an exception. Everything that
raises instead is about running the attempt, and an extension mapping retries onto its own
protocol has two attributes to read: `FlotillaError.category` and `FlotillaError.retryable`.

| Situation | Raised type | Category | Retry |
|---|---|---|---|
| Sandbox create rejected: rate limit, capacity, transient transport | `FlotillaError` | `RATE_LIMITED` / `CAPACITY` / `TRANSIENT` | The core reconciles uncertain creates; the caller may reopen |
| The task's own services fail: start, health check, one-shot exit | `FlotillaError` | `SERVICE` | No -- the sample is discarded, the task is at fault |
| An environment is lost after readiness | `FlotillaError` | `LOST` | Yes -- infrastructure, after readiness |
| A command's outcome is unknown | `ExecOutcomeUnknown` | `TRANSIENT` | Never replay; never turn it into a result |
| The admission queue's budget runs out | `AdmissionTimeout` | -- | Yes, when capacity frees; queueing does not eat `startup_timeout` |
| Cleanup cannot confirm deletion | `CleanupError` | -- | Close again or `flotilla gc`; the report says what is left |
| Grading exceeds its timeout | `TimeoutError` | -- | Policy choice; a longer `verifier.timeout_sec` or a discard |
| The task's data is malformed | `PlanError`, `TaskError`, `DatasetError`, `ValueError` at planning time | -- | Fix the task; the same task fails the same way every time |

`retryable` means the caller may try again, not that a command with side effects may be
replayed: `ExecOutcomeUnknown` is deliberately `retryable=False`, and nothing in the runtime
replays an exec. `COMMAND_BUSY` (`CommandBusy`) is the one transient a caller may re-issue,
because the platform rejected it before accepting it.
