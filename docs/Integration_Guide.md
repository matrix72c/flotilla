# Integrating Flotilla into a framework

This guide is for authors of evaluation runners, RL rollout workers, and agent frameworks that want to run their environments on Flotilla. The [Harbor adapter](../src/flotilla/integrations/harbor.py) is a complete example.

## Who owns what

Flotilla owns the native task protocol, preparation and environment resources: fixed images, sandboxes, network policies, shared directories, renewal and cleanup. Your framework selects complete dataset rows, drives the agent, interprets verification outputs, decides episode retries and admits results into training. Harbor source parsing is optional preparation, not an execution dependency.

Two consequences follow:

- One `NativeAttempt` owns its private workspace and roles. Your framework stops the agent before `attempt.verify()`, which stages hidden tests and declared artifact handoffs, then closes the attempt. Host `flotilla prepare` produces the native dataset before consumption; the user publishes its static files.
- A score never makes a cleanup failure acceptable. Check `CleanupReport.clean` separately and treat an unclean report as an infrastructure problem.

## One client per process

Create one `Flotilla` per process and keep it for the process lifetime. It owns an anchor sandbox, a background event loop thread for renewal and cleanup, and the concurrency limits from `deployment.toml`. Do not share one across processes, and do not create one per episode.

```python
from flotilla import Flotilla, RuntimeEvent


def on_event(event: RuntimeEvent) -> None:
    metrics.increment(f"flotilla.{event.kind}")  # called on a separate thread


fleet = Flotilla(deployment="deployment.toml", on_event=on_event)
await fleet.start()
try:
    ...  # run episodes
finally:
    report = await fleet.close()
    if not report.clean:
        log.error("flotilla cleanup incomplete: %r", report)
```

`on_event` receives best-effort diagnostic events: lifecycle kinds such as `queued`, `admitted`, `open_failed`, `operation_started`, `operation_finished`, `cancel_requested`, `service_terminated`, and `cleanup_finished`, plus one timing event per startup stage with `elapsed_s` set. They never contain argv, output, environment values, or credentials. Don't use them as lifecycle acknowledgements; the return values and exceptions are authoritative.

## Selecting native tasks

Read complete dataset rows using explicit deployment bindings. Do not reduce identities to basenames, discard duplicate rows or attach a new release root implicitly when concatenating datasets. Local metadata inspection reads prepared instructions and definitions, not hidden payloads or source Compose.

```python
from flotilla.config import FlotillaConfig
from flotilla.native import NativeAttempt
from flotilla.prepared import PreparedEnvironment

config = FlotillaConfig.load("deployment.toml")
bindings = config.release_bindings()
rows = bindings.read_dataset("dataset.jsonl")
bound = bindings.resolve(rows[0])
main = PreparedEnvironment(bound.task.roles["main"])
# Inspect reservations before allocating attempts.
attempt = await NativeAttempt.create(fleet, bound)
```

See [Native tasks](Native_Tasks.md) for the host preparation and static publication workflow. Both local and platform copies are verified. Execution needs the installed native helper, fixed runtime images and static release only; it must not import Harbor/Compose, build, push, mirror or export seed data from an image. Missing assets and unsupported protocols are explicit failures, not opportunities for fallback. The original source task directory is not an execution input.

Keep published releases immutable. Volume initialization and artifact handoffs are attempt-private. Snapshot release/task/image digests and the complete selected rows. Reusing validation within one launch does not authorize a global cross-run cache.

## Budgeting concurrent work

Before opening many environments, compute what they will reserve. This is local and creates nothing:

```python
per_episode = fleet.reservation(main).instances  # services x 2: one active generation plus one for retries
overhead = fleet.client_reservation().instances  # the anchor
```

Use these numbers when sizing a worker pool against `limits.instance_budget` and `limits.trial_concurrency`, especially when one episode opens a main and a verifier environment. They are upper bounds computed locally, not a remote quota check. Flotilla also enforces the limits itself: excess opens wait in its admission queue for up to `admission_timeout` and then raise `AdmissionTimeout`. Time spent queued does not count against `startup_timeout`.

## Running an episode

```python
from flotilla import ErrorCategory, ExecOutcomeUnknown, FlotillaError, OpenOptions

options = OpenOptions(
    request_id=episode_id,         # reported in events and env.info() for correlation
    admission_timeout=3600,        # waiting for a trial_concurrency / instance_budget slot
    startup_timeout=900,           # from admission until ready
    lifetime=3600,                 # starts when the environment is ready
    exec_env={"main": {"OPENAI_BASE_URL": proxy_url}},  # added to every command in that service
    disable_internet=True,
)
try:
    env = await attempt.open("main", options)
    for action in agent.actions():
        result = await env.agent.shell(action, timeout=120)
        if result.timed_out:
            agent.observe_timeout()  # the remote process was confirmed stopped
        else:
            agent.observe(result.exit_code, result.stdout, result.stderr)
    await agent.stop()  # stop writes before handing artifacts to the verifier
    verification = await attempt.verify()
    reward = score(verification)  # consumer-specific interpretation of declared outputs
except ExecOutcomeUnknown:
    discard(episode_id, reason="command outcome unknown")
except FlotillaError as exc:
    if exc.category is ErrorCategory.SERVICE:
        discard(episode_id, reason=f"task fault in {exc.service}")
    elif exc.retryable:
        requeue(episode_id)
    else:
        fail(episode_id, exc)
finally:
    await attempt.close()
```

How to treat each outcome:

| Outcome | Meaning | Typical handling |
|---|---|---|
| `ExecResult` with `timed_out=False` | The command ran to completion, including signal exits | Normal observation |
| `ExecResult` with `timed_out=True` | The deadline passed and the remote process was confirmed stopped | Normal observation; the environment is still usable |
| `ExecOutcomeUnknown` | The command may or may not have run. The environment has already been invalidated and released | Discard the episode. Never replay the command automatically |
| `FlotillaError`, category `SERVICE` | The task itself failed: a service did not start, failed its healthcheck, or exited | Discard the sample; retrying will usually fail the same way |
| `FlotillaError`, category `LOST` | The infrastructure lost the environment after it was ready | Retry the episode in a new environment |
| Other `FlotillaError` with `retryable=True` | Rate limit, capacity, or a transient platform error | Retry later |
| `CleanupError` | Release could not confirm deletion | Record `exc.report`; run `flotilla gc` later |

`retryable` describes the failed operation. It never means a command with side effects may be sent again.

## Background processes

```python
process = await env.agent.start_process(["python", "-m", "http.server", "8000"])
status = await process.wait(timeout=5)  # stops waiting; the process keeps running
if status.running:
    await process.stop()                # returns only once termination is confirmed
await env.check()                       # confirm the environment is still healthy
```

A wait deadline or cancellation only stops waiting; the process stays owned by its environment until the environment is released. `stop()` on an already-exited process returns without side effects. If termination cannot be confirmed, `stop()` releases the environment and raises `ExecOutcomeUnknown`.

## Owned and borrowed handles

An adapter that opens an environment owns it and must release it. An adapter that receives an existing `Environment` from elsewhere borrows it and must not release it. For native tasks, `NativeAttempt` owns roles and workspace; a consumer adapter wrapping an already-open environment only borrows that handle. Keep this distinction explicit in your own adapter, and make sure exactly one owner calls `release()`.

## After a crash

A process that dies without closing its client leaves its sandboxes and directories behind until their TTL expires. Run `flotilla gc --deployment deployment.toml` from any machine with the deployment config to collect them. GC finds launches both from `launches/` on shared storage and from sandbox labels, so a client that died before creating its directory is still found. It waits for two observations `gc_window` seconds apart before collecting a launch, so it never collects a live client. Use `--launch ID` only after confirming that client has exited. If GC cannot start its own anchor (for example, the anchor image cannot be pulled), it still deletes sandboxes by label, leaves directories in place, and exits 1.

## Testing your integration

Install the `testing` extra and use the public helpers in `flotilla.testing`. They run the low-level preparation, client, and runtime against an in-memory platform and fake image tooling, without a deployment, Docker, or credentials:

```python
import pytest

from flotilla import Flotilla
from flotilla.config import FlotillaConfig
from flotilla.testing.build import prepared_environment
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as deployment  # pytest fixture


@pytest.mark.asyncio
async def test_episode_releases_its_environment(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree], tmp_path
) -> None:
    cfg, platform, _ = deployment
    task = tmp_path / "task"
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text('[environment]\ndocker_image = "r.io/ns/main:1"\n')
    async with Flotilla(deployment=cfg) as fleet:
        async with fleet.environment(prepared_environment(task, cfg)) as env:
            assert (await env.agent.exec(["true"], timeout=10)).exit_code == 0
    assert fleet.cleanup_report is not None and fleet.cleanup_report.clean
    assert platform.instances() == []
```

The synthetic `prepared_environment` fixture above is for unit contracts, not a production task input or platform-acceptance result.

`FakePlatform` lets tests inject faults (`inject_fault`, `inject_create_then_fail`), add latency, replace the command handler (`set_exec_handler`), and lose background processes, so you can test your handling of the outcomes above. It is a test double, not a production backend or an isolation boundary, and it deliberately fails on operations it does not model.
