# Runtime API

Import public types from `flotilla`. Configuration is loaded locally at construction; `start()` begins remote ownership. The client runs renewal, monitoring, and cleanup on its own event-loop thread, independent of the caller's event loop.

## Environments

| Operation | Behavior |
|---|---|
| `Flotilla(deployment=..., on_event=None)` | Owns a configuration snapshot and one runtime; do not share across processes |
| `fleet.inspect(task_path)` | Synchronous local inspection of the main environment |
| `await fleet.start()` | Shared, idempotent initialization |
| `await fleet.open_task(path, options=None)` | Parses and prepares the main environment |
| `await fleet.open_environment(definition, options=None)` | Prepares an explicitly selected effective environment |
| `async with fleet.environment(source, options=None)` | Opens and releases on exit; accepts a path or definition |
| `await env.info()` / `await env.check()` | Resource metadata / current health and ownership check |
| `await env.release(timeout=None)` | Idempotent, confirmed environment cleanup |
| `await fleet.drain(timeout=None)` | Waits for pending cleanup; does not release active environments |
| `await fleet.close(timeout=None)` | Releases owned environments and closes the runtime; returns a cleanup report |

`EnvironmentDefinition(task_root, directory, name, config={})` selects a source directory confined to its task root. `config` is the effective environment configuration. Harbor inheritance and verifier selection belong to the adapter, not to core optional-dependency discovery.

`OpenOptions` accepts `request_id`, `startup_timeout`, `lifetime`, `runtime_params`, per-service `exec_env` and `resources`, `disable_internet`, and `force_build`. Resource overrides support CPU and memory. `force_build` selects source build when a Dockerfile exists; it does not disable Docker's layer cache.

`startup_timeout` includes initialization, source parsing, preparation queueing, builds, publication, and startup. Preparation also has the deployment's `preparation.timeout_s` cap. `lifetime` starts at readiness and cannot exceed `runtime.run_timeout_s`. Default preparation concurrency is 4. Runtime parameters are literal substitution values; only explicit network-policy targets are interpreted as network destinations.

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
