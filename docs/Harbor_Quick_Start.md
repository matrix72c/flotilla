# Harbor quick start

[Harbor](https://github.com/harbor-framework/harbor) is a framework for evaluating agents on containerized tasks, such as Terminal-Bench. Flotilla provides a Harbor environment so that each trial runs on sandboxes instead of a local Docker host.

Install the adapter together with its pinned Harbor version:

```sh
python -m pip install '.[harbor]'
```

Complete the [deployment guide](Deployment.md) first. The machine running Harbor needs Docker/BuildKit and registry access, and the platform credential must be exported in the variable named by `opensandbox.credential_env`.

Start with a task whose expected rewards are known. Harbor's built-in `oracle` agent runs the task's reference solution (it should pass) and `nop` does nothing (it should fail), so together they check the environment and verifier without involving a model:

```sh
export FLOTILLA_DEPLOYMENT=/path/to/deployment.toml
export HARBOR_TASK=/path/to/task
export HARBOR_JOBS=/path/to/private/results
for agent in oracle nop; do
  harbor run --path "$HARBOR_TASK" \
    --env flotilla.integrations.harbor:FlotillaEnvironment \
    --ek deployment="$FLOTILLA_DEPLOYMENT" \
    --agent "$agent" --n-concurrent 1 --max-retries 0 \
    --jobs-dir "$HARBOR_JOBS" --job-name "$agent-smoke"
done
```

Use fresh job names on subsequent runs. `flotilla inspect` checks the task's main environment only; Harbor selects effective main and verifier definitions using its own inheritance and step rules, and the adapter passes those definitions explicitly to Flotilla.

## Supported behavior

- Shared verification uses the existing main environment. Separate verification receives its own selected source/configuration and lifecycle.
- CPU and memory overrides are applied at runtime without mutating the caller's Harbor configuration. Storage/GPU/TPU overrides and additional Compose overlays are rejected.
- A normally constructed adapter owns its fleet. An explicitly injected fleet is borrowed, but the adapter still releases the environment it opened. `FlotillaEnvironment.view(...)` borrows both an existing environment and its fleet; stopping the view does not delete them.
- Agent/verifier users must exist in the image and have suitable permissions. The adapter prepares Harbor's log directories; it does not create task users or recursively change task ownership.
- Main-service termination preserves a local log snapshot for Harbor's later collection while sidecars remain available. Failures to confirm service termination cannot be accepted as a normal verifier result.
- `force_build` selects available source builds while retaining Docker layer caching.

## Running at scale

- Run the Harbor driver on a dedicated machine. One driver process holds an event loop, one stream per in-flight command, and any local BuildKit preparation; sharing it with other heavy workloads stalls the loop and surfaces as false environment-start timeouts across the whole job.
- Prewarm before loading: run one low-concurrency pass (for example `nop`) so every task image is built or derived and pushed once; the loaded run then performs no local builds and starts in seconds.
- Scale out, not up: raise total concurrency by sharding jobs across driver processes instead of one large `--n-concurrent`; each process runs its own preparation and keeps its own limits.
- Judge by per-trial `result.json` and cleanup records (below), never by CLI exit codes or wall-clock impressions.

## Acceptance

Inspect every trial's `result.json`: require no infrastructure exception and the task's expected verifier reward. A successful CLI exit alone does not establish success. Check agent/verifier logs, artifacts, resource overrides, and each `flotilla/<session>/environment.json` / `cleanup.json` record. Session records distinguish separate verifiers.

When validating a new deployment or adapter change, repeat with shared and separate verifier configurations, a borrowed view, cancellation during start/exec/stop, and a deliberately failed cleanup. A failed cleanup must remain visible. Use an independent platform connection to confirm all owned instances and launch directories have disappeared.
