# Runtime API

Import public types from `flotilla`. Configuration is loaded locally at construction; `start()` begins remote ownership. The client runs renewal, monitoring, and cleanup on its own event-loop thread, independent of the caller's event loop.

## Environments

| Operation | Behavior |
|---|---|
| `Flotilla(deployment=..., on_event=None)` | Owns a configuration snapshot and one runtime; do not share across processes |
| `fleet.plan(source)` | Resolves a task directory into the trial `open` would create, without creating it |
| `fleet.reservation(plan)` | Local admission budget of one environment: services x 2 |
| `await fleet.start()` | Shared, idempotent initialization |
| `await fleet.open(source, options=None)` | Opens a task directory, or a plan already resolved from one; builds and uploads nothing |
| `async with fleet.environment(source, options=None)` | Opens and releases on exit |
| `await env.info()` / `await env.check()` | Resource metadata / current health and ownership check |
| `await env.release(timeout=None)` | Idempotent, confirmed environment cleanup |
| `await fleet.drain(timeout=None)` | Waits for pending cleanup; does not release active environments |
| `await fleet.close(timeout=None)` | Releases owned environments and closes the runtime; returns a cleanup report |

For production tasks use `NativeAttempt.create(fleet, source)` from `flotilla.native`, where `source = config.resolve_datasets()[name].source(key)`. The task is resolved into one plan per role, plus how it is graded, before any role is opened. Call `await attempt.open("main", options)`, stop the agent, then `await attempt.verify()`. Verification stages hidden tests and declared artifact handoffs; a separate verifier is used only when the task defines one. Always `await attempt.close()` in `finally`; it closes roles before deleting their private workspace.

`TrialPlan` is the in-memory boundary for `fleet.open`: everything one trial runs, resolved from the task directory in this process. `open` also takes a `TaskSource` and resolves it for you; resolve it yourself when you want to check a dataset first, or run one plan several times. [Native tasks](Native_Tasks.md) documents datasets and path binding.

The native Anchor requires installed Flotilla and filesystem helpers, not Harbor, Compose, Docker or push credentials. Actual platform acceptance remains separate from package/unit checks.

`OpenOptions` accepts `request_id`, `admission_timeout`, `startup_timeout`, `lifetime`, per-service `exec_env` and `resources`, `disable_internet`, `external_mounts`, `workspace_id` and `workspace_mounts`. Resource overrides support CPU and memory. Compose variables are resolved at preparation, so there is nothing to substitute here.

Each phase of an open has its own budget. Client start (once per client) is bounded by `timeouts.client_start`. Waiting for a `trial_concurrency` / `instance_budget` slot is bounded by `admission_timeout` (default `limits.admission_timeout`) and raises `AdmissionTimeout` with the current counts. `startup_timeout` (default `runtime.startup_timeout_s`) starts at admission and ends when the environment is ready. `lifetime` starts at readiness and cannot exceed `runtime.run_timeout_s`. Runtime parameters are literal substitution values; only explicit network-policy targets are interpreted as network destinations.

## Anchor stat read recovery

The client's **read-only `Anchor.stat`** retries transient/rate-limited transport errors and unknown command responses (for example an HTTP 504 after sending `stat`): at most five attempts with 0.5/1/2/4 second delays and a 30-second retry-admission window. Command exit failures and malformed stat output are not retried. Cancellation or anchor deactivation interrupts recovery. OpenSandbox result downloads already use the HTTP layer's bounded idempotent GET retries; no outer download or batch retry multiplies them. Generic execution, writes, copies, builds and workloads remain non-replayed even when their response is unknown.

These limits bound retry admission and added waiting, not kernel-blocked reads on a hard NFS mount. Existing command timeouts, settled cancellation and cleanup remain in force. This is transient-read recovery, not a repair of the underlying storage service.

## Services and processes

`env.agent` is the main service; `env.service(name)` selects another service.

- `await service.exec(argv, timeout=None, cwd=None, env=None, uid=None, gid=None)` returns `ExecResult(exit_code, stdout, stderr, timed_out)`. Output is bytes. `argv` is an argument sequence without implicit shell interpretation. `timeout` defaults to `runtime.exec_default_timeout_s` and cannot exceed `limits.exec_timeout`.
- `await service.shell(command, shell="/bin/sh", ...)` explicitly invokes a shell.
- `await service.start_process(argv, ...)` returns a `Process`; `await process.status()` reports its state. Release the environment or terminate its service to end its owned background processes.
- `read_file` / `write_file` transfer bytes. `upload_file`, `download_file`, `upload_dir`, and `download_dir` transfer local paths. Transfers enforce path and size constraints; unsupported links/special entries are rejected.
- `await service.terminate()` confirms deletion of that service while allowing surviving sidecars to remain usable.

## Cancellation and errors

Commands are non-idempotent and are never automatically replayed. Execution tracks whether a request was sent and its remote command ID, including background starts. Cancellation waits for termination confirmation or for invalidation and transfer of cleanup responsibility. Cancellation during result delivery is covered by the same ownership rule.

`timed_out=True` means a client deadline expired and remote stopping was confirmed. A normal signal exit is an ordinary execution result. Missing IDs, missing status records, or unconfirmed termination produce `ExecOutcomeUnknown`; the environment is invalidated and released. Do not retry unknown execution automatically. A timeout waiting for admission before sending has no remote execution effect.

`CommandBusy` means the backend explicitly rejected the command before accepting it. Foreground and background calls raise this error without releasing the environment or automatically replaying the command. A busy healthcheck waits for its next interval; an unknown healthcheck outcome fails startup and triggers cleanup.

```python
from flotilla import CleanupError, ExecOutcomeUnknown

try:
    result = await env.agent.exec(["make", "test"], timeout=600)
except ExecOutcomeUnknown:
    # The command may or may not have run. The environment is already invalidated;
    # open a new one instead of retrying blindly.
    raise
if result.timed_out:
    ...  # the deadline expired and the remote process was confirmed stopped

try:
    await env.release()
except CleanupError as exc:
    print(exc.report)  # what is still left on the platform
```

`FlotillaError` carries `stage`, `category`, and `retryable`. These describe an operation's failure, not permission to replay a command. HTTP exceptions exclude raw remote response bodies. Do not add command output, credentials, or complete configuration objects to diagnostics.

`CleanupReport.clean` requires no remaining instances, directories, or uncertain creates. Each unclean `CleanupRecord` in `report.remaining` has a `reason` (`create_in_flight`, `create_outcome_unknown`, `delete_unconfirmed`, `directory_remove_failed`, `anchor_deactivated`, or `in_progress`) and, when one was seen, the last related `error`. `CleanupError` exposes that report. Context managers preserve a primary application error and attach cleanup failure information. A timed-out waiter does not cancel an owned deletion.

Before retiring its Anchor, `Flotilla.close()` retries previously failed owned cleanup once, within the same close timeout. This allows a directory removal that failed while NFS file handles were still open to recover after those handles close. The retry uses retained ownership records, not a scan of unrelated resources. Persistent failures and exhausted deadlines still produce an unclean report; successful final cleanup does not retroactively accept an episode whose release failed.

## Diagnostic events

`on_event` receives `RuntimeEvent` on a separate dispatcher thread. Callbacks must be thread-safe. The queue holds 1024 diagnostic events; when full, new events are dropped and counted internally. Events are best-effort observations, never lifecycle acknowledgements. Slow callbacks cannot delay renewal or cleanup. No argv, output, environment values, or credentials are included in event fields.
