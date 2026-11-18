# Harbor quick start

Harbor supplies the task format and is an optional evaluation consumer. Flotilla runs a [published task directory](Native_Tasks.md), not a platform task key: it reads the directory and each image's config, and resolves the trial in the process that runs it.

Install the controlled Flotilla wheel, or use the full Git SHA in your delivery manifest:

```sh
python -m pip install 'flotilla @ git+https://github.com/matrix72c/flotilla.git@bd28834c0685d9002ab85b1903d3c6f291d61d7b'
```

Flotilla pins [Harbor d78eb9c262e7074944106d16bff0ba6426bb2d34](https://github.com/matrix72c/harbor/commit/d78eb9c262e7074944106d16bff0ba6426bb2d34), which supplies `task_source`, trial `runtime`, and `Task.from_metadata`. Stock Harbor 0.24.0 lacks these extension interfaces. The public PyPI package named `flotilla` is unrelated; do not install it by bare name.

## Publish the tasks, declare the dataset

Use [Quick start](Quick_Start.md) to probe the deployment. Publish your task directories through your existing storage tools, preserving file modes, then declare where they are on both sides:

```toml
[datasets.tb2]
local_root = "/srv/eval/datasets/tb2"
platform_root = "pvc:YOUR-CLAIM/YOUR-ROOT/datasets/tb2"
```

This example uses PVC storage; the claim and configured root subpath must match `[storage]`. For host-path storage, use its absolute host path (or a relative key under that configured root). A PVC binding must use the explicit `pvc:` form. Declaring a dataset uploads and synchronizes nothing. Keep it immutable while jobs or restored trials reference it.

Execution requires a compatible installed Anchor and shared tools, plus registry **read** access for image configs. No Compose executable, builder or Docker is involved.

## Native `harbor run`

Save `/srv/eval/harbor-job.yaml`:

```yaml
job_name: flotilla-native-smoke
n_concurrent_trials: 1
agents:
  - name: nop
task_source:
  import_path: flotilla.integrations.harbor_native:FlotillaTaskSource
  kwargs:
    dataset: tb2                      # a [datasets.<name>] the deployment declares
    deployment: /srv/eval/deployment.toml
    # selection: /srv/eval/tb2-subset.txt    # optional: task keys, one per line
    metadata_workers: 8
runtime:
  import_path: flotilla.integrations.harbor_native:NativeFlotillaRuntime
  kwargs:
    deployment: /srv/eval/deployment.toml
environment:
  import_path: flotilla.integrations.harbor_native:NativeFlotillaEnvironment
  delete: true
```

```sh
harbor run --config /srv/eval/harbor-job.yaml
```

`nop` is a lifecycle smoke agent, not a solver. Supply your intended Harbor agent and its credentials separately for evaluation. Do **not** set Harbor `tasks`/`datasets`, `verifier.import_path`, or `verifier.env`: the dataset owns selection and each task owns its verification. Without `selection`, every task in the dataset is selected, in a stable order; with it, the listed keys in that file's order, so repeating a key repeats the trial.

A relative `deployment` or `selection` path resolves against the config that defines it. Snapshots freeze absolute external paths. Remote configs require absolute input paths. A standalone restored Trial revalidates its selection rather than depending on the old Job's in-memory cache.

The adapter builds a metadata-backed Harbor Task from the task's own `task.toml` and `instruction.md`. Remote task downloads, multi-step and non-Linux tasks fail explicitly.

## Execution and ownership

- `task_source` owns local selection and metadata; `runtime` owns the Flotilla client, `NativeAttempt`, verification, artifacts and cleanup; `environment` is the Harbor I/O adapter. Configure all three together.
- Each attempt has private mutable volumes and verification output. Immutable task files are shared read-only.
- Hidden tests are not exposed during the agent phase. A task with an explicit separate verifier environment releases main before opening the verifier; artifact handoff remains platform-side. A task without one is verified in main, in the sandbox the agent just worked in.
- The application and healthcheck UID/GID a task declares remain authoritative. Flotilla does not rewrite images, recursively rewrite published asset ownership, or create application users.
- Task and job/trial result-artifact downloads use Harbor's ordinary trial output and manifest. Agent failures and disabled verification still collect available results before cleanup. They are distinct from verifier handoff.
- A test script's nonzero exit with a valid reward may be a graded failure. Missing/malformed/nonfinite rewards and transport/cleanup failures are infrastructure errors, not reward zero.
- `environment.delete: false` and `force_build` are rejected. `runtime.kwargs.run_timeout` must be positive and within the deployment lifetime.

For non-Harbor applications, use the public `Dataset` and `NativeAttempt` API in [Native tasks](Native_Tasks.md).

## Acceptance

Check known positive/negative outcomes, same-role and explicit separate verification, nonempty handoff, declared nonroot identities, hidden-test isolation, cancellation and failed cleanup. Require independent confirmation that platform resources were released. Simulated transport tests and successful CLI parsing are not real platform acceptance; see [Deployment](Deployment.md) for the evidence contract.
