# Runtime API

Import public types from `flotilla`. Configuration is loaded locally at construction; `start()` begins remote ownership. The client runs renewal, monitoring, and cleanup on its own event-loop thread, independent of the caller's event loop.

## Environments

| Operation | Behavior |
|---|---|
| `Flotilla(deployment=..., on_event=None)` | Owns a configuration snapshot and one runtime; do not share across processes |
| `fleet.reservation(prepared)` | Local admission budget of one environment: services x 2 |
| `await fleet.start()` | Shared, idempotent initialization |
| `await fleet.open(prepared, options=None)` | Opens a prepared environment; builds and uploads nothing |
| `async with fleet.environment(prepared, options=None)` | Opens and releases on exit |
| `await env.info()` / `await env.check()` | Resource metadata / current health and ownership check |
| `await env.release(timeout=None)` | Idempotent, confirmed environment cleanup |
| `await fleet.drain(timeout=None)` | Waits for pending cleanup; does not release active environments |
| `await fleet.close(timeout=None)` | Releases owned environments and closes the runtime; returns a cleanup report |

A `PreparedEnvironment` comes from a row of the jsonl that `flotilla prepare` writes: `load_prepared(path)` returns `PreparedTask` rows, `row.environment()` is the main environment, and `row.verifier` is the separate verifier environment or `None`. Images are digest-pinned and task files already published, so opening does not need Docker. Harbor verifier selection happens during preparation; the adapter opens the row's environment for its environment directory.

`OpenOptions` accepts `request_id`, `startup_timeout`, `lifetime`, `runtime_params`, per-service `exec_env` and `resources`, and `disable_internet`. Resource overrides support CPU and memory.

`startup_timeout` (default `runtime.startup_timeout_s`) includes initialization, admission queueing, and startup. `lifetime` starts at readiness and cannot exceed `runtime.run_timeout_s`. Runtime parameters are literal substitution values; only explicit network-policy targets are interpreted as network destinations.

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

`CleanupReport.clean` requires no remaining instances, directories, uncertain creates, or preparation residuals. `CleanupError` exposes that report. Context managers preserve a primary application error and attach cleanup failure information. A timed-out waiter does not cancel an owned deletion.

## Diagnostic events

`on_event` receives `RuntimeEvent` on a separate dispatcher thread. Callbacks must be thread-safe. The queue holds 1024 diagnostic events; when full, new events are dropped and counted internally. Events are best-effort observations, never lifecycle acknowledgements. Slow callbacks cannot delay renewal or cleanup. No argv, output, environment values, or credentials are included in event fields.
