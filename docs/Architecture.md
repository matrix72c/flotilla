# Architecture

## Terminology

| Term | Meaning |
|---|---|
| Task | A published directory: `task.toml`, an `environment/` Compose project, `instruction.md`, and `tests/` |
| Trial plan | What a task resolves to: one unit per service, with its image, resources, mounts, network and dependencies |
| Environment | One running instance of a task: a group of sandboxes, one per service |
| Service | One Compose service; the main service is `env.agent`, others are sidecars |
| Trial | Flotilla's internal unit for one environment's sandboxes, labels, and lifecycle |
| Launch | One running Flotilla client process; every resource it creates carries its launch ID |
| Anchor | A small, renewable sandbox per launch that mounts the shared-storage root and performs file operations on it |
| Share release | An immutable directory of helper binaries (BusyBox) mounted into every sandbox at `/.flotilla` |
| Dataset | A directory of task directories, declared by name and bound to a local and a platform root |
| Task source | Where one task's files are, on the client and on the platform; both, because neither side can infer the other |
| Attempt workspace | Launch-owned private role state, verification staging and artifact handoff directories |
| Agent phase | The part of an attempt the agent acts in: from the first `open` until `end_agent_phase` |
| Grading | The phase after it: `stage_verification` makes the tests visible, `open_verification_role` runs the command that scores the agent's artifacts |
| Isolation evidence | What `flotilla probe` measured, in memory for the length of the run: seven properties a deployment can lack silently, plus what was observed for each |
| Waiver | An isolation property the operator accepts running without, named in `security.accept_insecure`; the probe exits nonzero on any gap not named there |
| execd | The OpenSandbox daemon inside each sandbox that runs commands and transfers files |
| GC | Explicit collection of resources left by crashed launches (`flotilla gc`) |

## Layers

Bottom up; each layer may import the layers below it, and the import contracts in
`pyproject.toml` (CI runs `lint-imports`) refuse the rest:

| Layer | What it owns | Enforced by |
|---|---|---|
| `models` | The vocabulary every layer passes around: plans, sources, verification shapes, errors | `models is the bottom of the graph` |
| `platform` | The sandbox protocol (`platform.base`) and the backends behind it | `platform does not depend on core` |
| `core` | Trial lifecycle: labels, renewal, reconciliation, cleanup, capacity | `core depends only on platform.base` |
| `compose` | Task resolution: Compose project in, `TrialPlan` out; offline and stateless | `compose resolves tasks and nothing else`, `offline path (compose/scan) does not call the platform` |
| `runtime` | Admission, environment handles, monitoring, the maintenance loop | `runtime does not plan or grade tasks`, `runtime does not import adapters`, `runtime does not inspect or scan` |
| `native` | Graded attempts: planning per role plus the two-phase lifecycle over the runtime | `native drives the runtime, not the platform` |
| `config` | The deployment spec; resolves datasets eagerly, plans tasks lazily through `plan_for` | the `compose` and `runtime` contracts above, read from below |
| `deployment` | The consumer entry point; wraps `config` in `plan`, `plan_environment`, clients, validation | `integrations, probe, cli, and testing are outermost` |
| integrations, probe, cli, testing | Consumers of the whole stack; nothing internal imports them | same contract |

Two edges are deliberately lazy rather than forbidden: `config` imports `compose` and
`deployment` imports `compose.task` only inside the functions that need them, so importing a
spec (for `flotilla gc`, a probe, or metadata use) does not pull the Compose parser in.

## Components

```mermaid
flowchart LR
    Dataset[Published dataset] --> Source[TaskSource: client path + platform key]
    Source --> Plan[FlotillaConfig.plan_for: TrialPlan]
    Registry[(Registry image configs)] --> Plan
    Plan --> Attempt[NativeAttempt]
    Attempt --> Workspace[Private platform workspace]
    Workspace --> API[Role environments]
    API --> Manager[Runtime manager]
    Manager --> Core[Lifecycle core]
    Core --> Platform[Platform protocol]
    Platform --> Backend[OpenSandbox current / legacy]
    Platform --> Plugin[Backend plugins]
    Harbor[Harbor adapter] --> API
```

`compose` reads a task directory and resolves it into a `TrialPlan`: it loads the Compose project itself, normalizes it into Flotilla's supported subset with a finding per rejected field, and reads each image's config from the registry by digest. `FlotillaConfig.plan_for` is the single planning entry point — it binds a task to the deployment's shape and resources — and `Deployment.plan` wraps it for consumers. This is a local computation over immutable inputs, so it writes nothing and can be run over a whole dataset before any sandbox exists. `NativeAttempt` resolves a task into one plan per role plus how it is graded, and opens them. See [Running published tasks](Native_Tasks.md) for the immutable-publication trust boundary.

`runtime` owns admission, environment handles, monitoring, and cleanup. Each client has one maintenance loop in a separate thread. Diagnostic callbacks use a bounded dispatcher thread; local file reads use owned thread work that must settle before their directory is removed.

`backends` defines the settings table and the `Backend` protocol; `BACKENDS` in `flotilla/config.py` selects the platform: the built-in OpenSandbox backend or another in-tree backend, and every caller (runtime, probe, GC, share) constructs it through `FlotillaConfig.build_platform`. See [Writing a backend](Writing_a_Backend.md).

`core` depends on the Platform protocol and injected Clock. Its anchor is a renewable sandbox mounting the shared root. Launch and trial labels allow reconciliation after uncertain creates; a create response timeout does not imply that creation failed. Startup reserves replacement capacity and uses actual dependency gates. Cleanup releases capacity only after deletion is confirmed.

A ready environment has a finite lifetime. Operations verify ownership and service health. Cancellation tracks remote execution attempts, interrupts identified commands, and confirms their status. If stopping cannot be proved, the environment becomes unusable and deletion takes over. Background-result delivery has the same responsibility. Commands are never replayed automatically.

Shared storage separates the immutable shared-tools release at `share/`, published datasets on whatever roots the deployment declares, and launch-owned workspaces at `launches/<launch>/attempts/<attempt>`. A plan may name task files anywhere on declared storage but never under `launches/` or `share/`, which are Flotilla's own. Task files are mounted read-only where they are published, so nothing is copied to run them; only a writable bind group or a named volume becomes trial-owned storage. Hidden tests remain private to the trusted Anchor; agent roles receive only selected mounts, public instruction and writable state. Verification stages tests after the agent stops. A separately declared verifier role receives declared handoff artifacts read-only; otherwise verification uses the main role. Role instances must be deleted before their workspace.

The Anchor is a scratch image with one pinned BusyBox, not a Compose executable, a builder or push credentials: Flotilla reads images, it does not write them. No automatic DinD, protocol fallback or credential baking is performed. Offline tests do not establish real platform acceptance or a compatible published image.

The anchor pauses new creates and directory operations once renewal is overdue by its grace period and resumes when a renewal succeeds; the launch and its trials are lost only when the anchor's TTL could have expired. GC uses two observations and list-visibility bounds to avoid collecting a live owner. Explicit GC handles abandoned resources after crashes; it is not a scheduler inside the runtime.

Reading entry points: `runtime/client.py`, `runtime/workspace.py`, `native/runtime.py`, `native/dataset.py`, `compose/plan_task.py`, `core/trial.py`, `core/reaper.py`, and `platform/base.py`. The public `flotilla.testing` package provides clocks, fake registries, a deliberately limited fake platform, and an attempt harness that drives a real `NativeAttempt` through both grading phases, for downstream contract tests; it ships in the wheel, and runtime packages never import it. See [the SDK surface](SDK.md) for what may be imported and the stability commitments, [the API](Runtime_API.md) for user contracts, [OpenSandbox](backends/opensandbox.md) for protocol details, and [integration guide](Integration_Guide.md) for the testing surface.
