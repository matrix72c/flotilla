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

For production native tasks use `NativeAttempt.create(fleet, bound_task)` from `flotilla.native`, where `bound_task = config.release_bindings().resolve(row)`. Local release validation and actual platform file validation precede opening roles. Call `await attempt.open("main", options)`, stop the agent, then `await attempt.verify()`. Verification stages hidden tests and declared artifact handoffs; a separate verifier is used only when the task defines one. Always `await attempt.close()` in `finally`; it closes roles before deleting their private workspace.

`PreparedEnvironment` is the low-level in-memory boundary for `fleet.open`, not the persistent dataset interchange. The native release/dataset is that interchange. Do not use old `prepared.jsonl`, task-key rows or `load_prepared` as native inputs. `flotilla prepare` creates the release on the host. [Native tasks](Native_Tasks.md) documents models, bindings and integrity; [Harbor quick start](Harbor_Quick_Start.md) documents the optional consumer adapter.

The native Anchor requires installed Flotilla and filesystem helpers, not Harbor, Compose, Docker or push credentials. Actual platform acceptance remains separate from package/unit checks. The old `HarborAttempt`, `HarborVerification` and workspace compiler remain explicit legacy source-task APIs; native consumers never fall back to them.

`OpenOptions` accepts `request_id`, `admission_timeout`, `startup_timeout`, `lifetime`, `runtime_params`, per-service `exec_env` and `resources`, and `disable_internet`. Resource overrides support CPU and memory.

Each phase of an open has its own budget. Client start (once per client) is bounded by `timeouts.client_start`. Waiting for a `trial_concurrency` / `instance_budget` slot is bounded by `admission_timeout` (default `limits.admission_timeout`) and raises `AdmissionTimeout` with the current counts. `startup_timeout` (default `runtime.startup_timeout_s`) starts at admission and ends when the environment is ready. `lifetime` starts at readiness and cannot exceed `runtime.run_timeout_s`. Runtime parameters are literal substitution values; only explicit network-policy targets are interpreted as network destinations.

## Legacy source metadata

For native releases, `ReleaseReader` and `ReleaseBindings` own metadata and full dataset identity. The following source-reader API is retained only for explicit legacy integrations and importer tooling; it does not read native releases.

`flotilla.integrations.harbor_source_reader` provides `SourceMapping(local_root=..., platform_prefix=...)` and `NativeTaskSource(sources)`. Prefixes match whole path components; overlapping mappings and metadata files escaping their task/source roots are rejected.

| Operation | Result |
|---|---|
| `resolve_task(key)` / `key_for_path(path)` | Map an original platform key to a contained local task directory, or back |
| `read_config(key)` | Harbor `TaskConfig` with native defaults; does not read the instruction |
| `read_metadata(key)` | `task_id` (directory basename), canary-stripped `instruction`, JSON-mode `task_config` |
| `await read_configs(keys, workers=8)` | Full-key-to-`TaskConfig` mapping, config only |
| `await read_many(keys, workers=8)` | Full-key-to-metadata mapping |
| `metadata_identity(description)` | In-memory digest of canonical config and stripped instruction, not a full-tree hash |

Single-task calls are synchronous. Bulk calls keep filesystem work off the event loop, deduplicate only full keys and preserve first-occurrence order. Spawn workers process batches of 64 with at most `2 × workers` in flight; small requests cap the effective worker count, and a single worker uses no subprocess. Cancellation stops new submissions, cancels queued work and joins active reads before propagating. Neither API constructs a filesystem Harbor `Task`, scans tests/solution/environment payloads, initializes a platform client or writes a persistent cache. Kernel-blocked filesystem reads still have to return before cleanup completes.

Native dataset selection uses config-only reads; individual Trials load instructions when selected for execution. Dataset discovery and lifetime of any in-memory results belong to the caller. Keep original sources immutable during a run; the identity check does not compare hidden tests or other payload files.

## Platform metadata read recovery

`await fleet.describe_tasks(task_keys)` reads instruction/config metadata from native tasks without creating attempts. `await fleet.describe_task_resources(task_keys, runtime_params=...)` inspects declared resources without building images. The management image must contain the matching workspace helper. Inputs remain immutable throughout a run; metadata reuse does not deduplicate dataset rows or rename task IDs.

The helper recovers actual `OSError(ESTALE)` in native description and explicit source-path/resource-discovery reads. Each logical read gets at most five attempts, with 0.5, 1, 2 and 4 second delays. No new retry is admitted after 30 seconds for that read, and one metadata request (at most 128 unique keys) shares 15 seconds of added sleep. A failed description rereads and reparses metadata directly, without constructing a filesystem Harbor `Task`; previously successful tasks are not reread. Metadata-only reads do not validate unrelated payload trees. The private-copy compile/use boundary still validates the task tree and relocatable test inputs, and enumeration errors there propagate instead of accepting an incomplete tree. No partial metadata batch is published.

The same description recovery runs before an attempt publishes its instruction or stages tests. It does **not** retry file copies, Compose commands, builds, Agent/Verifier execution, remote command failures, or unknown execution outcomes. Permissions, missing files, invalid task data and unsafe links are not transient-read retries. Exhaustion preserves the original error with a short diagnostic note; it never becomes missing metadata or a task score. Successful recovery emits one concise helper diagnostic.

Separately, the client's **read-only `Anchor.stat`** retries transient/rate-limited transport errors and unknown command responses (for example an HTTP 504 after sending `stat`): at most five attempts with 0.5/1/2/4 second delays and a 30-second retry-admission window. Command exit failures and malformed stat output are not retried. The metadata helper is not replayed: its result directory stays alive until result retrieval/validation finishes or fails, then normal cleanup runs. Cancellation or anchor deactivation interrupts recovery. OpenSandbox result downloads already use the HTTP layer's bounded idempotent GET retries; no outer download or batch retry multiplies them. Generic execution, writes, copies, builds and workloads remain non-replayed even when their response is unknown.

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

## Diagnostic events

`on_event` receives `RuntimeEvent` on a separate dispatcher thread. Callbacks must be thread-safe. The queue holds 1024 diagnostic events; when full, new events are dropped and counted internally. Events are best-effort observations, never lifecycle acknowledgements. Slow callbacks cannot delay renewal or cleanup. No argv, output, environment values, or credentials are included in event fields.
