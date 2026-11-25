# Integrating Flotilla into a framework

This guide is for authors of evaluation runners, RL rollout workers, and agent frameworks that want to run their environments on Flotilla. The [Harbor adapter](../src/flotilla/integrations/harbor.py) is a complete example; if your framework is Harbor itself, see [running harbor trials](#running-harbor-trials) -- the loop already exists.

## Who owns what

Flotilla owns resolving a task into a trial and the environment resources that trial needs: sandboxes,
network policies, shared directories, renewal and cleanup. Your framework decides which tasks to run,
drives the agent, interprets verification outputs, decides episode retries and admits results into
training.

Two consequences follow:

- One `NativeAttempt` owns its private workspace and roles. Your framework stops the agent before `attempt.verify()`, which stages hidden tests and declared artifact handoffs, then closes the attempt.
- A score never makes a cleanup failure acceptable. Check `CleanupReport.clean` separately and treat an unclean report as an infrastructure problem.

## Running harbor trials

`FlotillaTaskEnvironment` is a harbor environment that runs one trial on a `NativeAttempt`. Nothing is
written between the two sides: harbor drives the trial, the attempt owns the workspace, and both
grading modes of a harbor task map onto the attempt's phases.

```sh
harbor run \
  --env flotilla.integrations.harbor:FlotillaTaskEnvironment \
  --ek deployment=myproject.deployment:PLATFORM \
  --path /srv/tasks/example/ds/demo
```

`deployment` names the deployment the way every flotilla command does, as `module:attribute`;
credentials stay in that spec and never pass through `--ek`. `--path` must land inside a directory the
deployment declares as a dataset -- published tasks are mounted in place, so a task harbor copied into
its own workspace (hub and git tasks) is rejected at startup, with that reason named. Preflight loads
the deployment and resolves its datasets before the trial is queued, so a mistyped option or a bad
import fails fast.

What the environment does with one trial:

- The agent environment resolves the task, creates the attempt, and opens the main role. CPU and
  memory overrides fold into the trial; there is no storage, GPU, or TPU knob to fold, and those flags
  are rejected with the reason. A baseline network policy of NO_NETWORK disables internet; phase
  policies broader than the baseline are refused by harbor itself before the trial starts.
- Grading follows the task's own declaration. Tests with their own Compose, or
  `[verifier].environment.docker_image`, grade in a separate verifier environment; anything else
  grades inside the agent environment. Harbor's tests upload is the handoff into grading -- staging
  supersedes the copy, so harbor's `file_filter` on the tests is not replayed -- and published test
  scripts must carry their own execute bits, since chmod on the read-only mount is best-effort.
- Whichever environment ends the trial closes the attempt. If harbor never grades (verifier disabled,
  an agent failure), the agent environment's stop closes it; in separate mode the workspace outlives
  the agent environment for the verifier that follows, and is released at process exit if none does.

One process runs one fleet per deployment, closed at interpreter exit; a process embedding harbor
should call `flotilla.integrations.harbor.shutdown()` instead of relying on exit. Regrades and grading
in another process are rejected with the reason named: verification runs on a trial the same process
opened, because the attempt's state lives in that process.

## Loading a deployment

Wrap the deployment spec once per process in a `Deployment` and ask it for everything platform-side. Do not read backend tables, timeout names or storage settings from the spec yourself.

```python
from flotilla import Deployment

from myproject.deployment import PLATFORM

deployment = Deployment(PLATFORM)
deployment.bind_credentials("private.env")  # binds only credential_env, nothing else in the file
fleet = deployment.client(on_event=on_event)
datasets = deployment.datasets()            # declared [datasets.<name>] by name
sources = [datasets["demo"].source(key) for key in datasets["demo"].tasks()]
plan = deployment.plan(sources[0])          # resolve one task locally; creates nothing
budget = deployment.setup_budget()          # worst case from create to started services, seconds:
                                            # the per-phase timeouts' sum, capped by runtime.startup_timeout_s
peak = deployment.dataset_peak(sources)     # local reservation maxima; creates nothing
layers = deployment.runtime_layers()        # [agent_runtime] as storage keys; names are your vocabulary
options = deployment.runtime_options(plan, layers, {"node": "/opt/runtime/node"}, request_id="episode-1")
report = await deployment.validate(sources, sandboxes=True, options=lambda role, plan: ...)
```

`runtime_options` mounts each layer read-only into the plan's agent service and rejects a layer set whose storage root has moved. `validate` records each task's failure instead of raising, never opens a separate verifier role and closes its client cleanly.

## One client per process

Create one `Flotilla` per process and keep it for the process lifetime. It owns an anchor sandbox, a background event loop thread for renewal and cleanup, and the spec's concurrency limits. Do not share one across processes, and do not create one per episode.

```python
from flotilla import Flotilla, RuntimeEvent


def on_event(event: RuntimeEvent) -> None:
    metrics.increment(f"flotilla.{event.kind}")  # called on a separate thread


fleet = Flotilla(deployment=PLATFORM, on_event=on_event)
await fleet.start()
try:
    ...  # run episodes
finally:
    report = await fleet.close()
    if not report.clean:
        log.error("flotilla cleanup incomplete: %r", report)
```

`on_event` receives best-effort diagnostic events: lifecycle kinds such as `queued`, `admitted`, `open_failed`, `operation_started`, `operation_finished`, `cancel_requested`, `service_terminated`, and `cleanup_finished`, plus one timing event per startup stage with `elapsed_s` set. They never contain argv, output, environment values, or credentials. Don't use them as lifecycle acknowledgements; the return values and exceptions are authoritative.

## Selecting tasks

Name a declared dataset and the task keys inside it. Do not reduce identities to basenames or attach a
different dataset root implicitly when combining selections.

```python
from flotilla.native import NativeAttempt

from myproject.deployment import PLATFORM

dataset = PLATFORM.resolve_datasets()["demo"]
source = dataset.source("vulhub/cve-2017-7529")
# Resolving is local and synchronous: check a whole dataset before spending a sandbox on any of it.
plan = NativeAttempt.resolve(fleet, source).plans["main"]
attempt = await NativeAttempt.create(fleet, source)
```

Resolving reads the task's own files and each image's config from the registry, and nothing else. An
image config is cached by digest, so a repeated image costs no network. Keep published datasets
immutable: an attempt mounts the task directory read-only, in place, so changing it mid-run changes
what running attempts see.

See [Running published tasks](Native_Tasks.md) for path binding. Volume initialization and
artifact handoffs are attempt-private. Snapshot the dataset name, the task keys and the image digests
the task declares.

## Budgeting concurrent work

Before opening many environments, compute what they will reserve. This is local and creates nothing:

```python
per_episode = fleet.reservation(plan).instances  # services x 2: one active generation plus one for retries
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

Each outcome's meaning and retry policy is in the [SDK error table](SDK.md#errors-whose-fault-and-what-to-do). In short: `timed_out=True` is a normal observation (the remote process was confirmed stopped); `ExecOutcomeUnknown` discards the episode and is never replayed; `SERVICE` is a task fault; `LOST` and other `retryable` errors are infrastructure and may be retried in a new environment; `CleanupError` is recorded and left for `flotilla gc`.

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

A process that dies without closing its client leaves its sandboxes and directories behind until their TTL expires. Run `flotilla gc --deployment myproject.deployment:PLATFORM` from any machine that can import the spec to collect them. GC finds launches both from `launches/` on shared storage and from sandbox labels, so a client that died before creating its directory is still found. It waits for two observations `gc_window` seconds apart before collecting a launch, so it never collects a live client. Use `--launch ID` only after confirming that client has exited. If GC cannot start its own anchor (for example, the anchor image cannot be pulled), it still deletes sandboxes by label, leaves directories in place, and exits 1.

## Testing your integration

Install the `testing` extra and use the public helpers in `flotilla.testing`. They run planning, the client, the runtime, and the attempt lifecycle against an in-memory platform and a fake registry, without a deployment, Docker, or credentials:

```python
import pytest

from flotilla import Flotilla
from flotilla.config import FlotillaConfig
from flotilla.testing.planning import APP, planned_environment
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as deployment  # pytest fixture


@pytest.mark.asyncio
async def test_episode_releases_its_environment(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree], tmp_path
) -> None:
    cfg, platform, _ = deployment
    task = tmp_path / "task"
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text(f'[environment]\ndocker_image = "{APP}"\n')
    async with Flotilla(deployment=cfg) as fleet:
        async with fleet.environment(planned_environment(task, cfg)) as env:
            assert (await env.agent.exec(["true"], timeout=10)).exit_code == 0
    assert fleet.cleanup_report is not None and fleet.cleanup_report.clean
    assert platform.instances() == []
```

`planned_environment` resolves a task directory with a fake registry, so it needs no network. It is for
unit contracts, not a production task input or a platform-acceptance result.

`FakePlatform` lets tests inject faults (`inject_fault`, `inject_create_then_fail`), add latency, replace the command handler (`set_exec_handler`), and lose background processes, so you can test your handling of the outcomes above. It is a test double, not a production backend or an isolation boundary, and it deliberately fails on operations it does not model.

`flotilla.testing.attempt` drives a real `NativeAttempt` on the fake platform: `prepared_source` writes a task with hidden tests and publishes it on the fake shared storage, and `AttemptHarness` emulates the anchor's staging commands and the grading command. An integration's episode loop — including `verify`, or the three phase primitives called by hand — runs end to end with no platform, registry, or network. What a [checked](SDK.md) surface import is and which parts may move between versions is the SDK surface doc's business; this guide's snippets use it.
