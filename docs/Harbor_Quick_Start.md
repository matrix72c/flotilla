# Harbor quick start

Harbor is a **host-side import source** and an optional evaluation consumer. Flotilla's execution authority is a versioned [native task release](Native_Tasks.md), not an original Harbor directory or a platform task key. Attempts do not parse Compose, build/push images, or export image seeds.

Install the controlled Flotilla wheel with its `harbor` extra, or use the full Git SHA in your delivery manifest:

```sh
python -m pip install 'flotilla[harbor] @ git+https://github.com/matrix72c/flotilla.git@<FULL_FLOTILLA_COMMIT>'
```

The extra pins [Harbor d78eb9c262e7074944106d16bff0ba6426bb2d34](https://github.com/matrix72c/harbor/commit/d78eb9c262e7074944106d16bff0ba6426bb2d34), which supplies `task_source`, trial `runtime`, and `Task.from_metadata`. Stock Harbor 0.24.0 lacks these extension interfaces. The public PyPI package named `flotilla` is unrelated; do not install it by bare name.

## Prepare once, publish static files

Use [Quick start](Quick_Start.md) to probe the deployment and run host-side `select` then `prepare`. The preparation environment needs the pinned official Compose CLI and configured image tooling. Registry credentials belong only there. Execution requires a compatible installed Anchor and shared tools, **not** Harbor/Compose/build tooling in the Anchor.

The output is a portable directory containing `manifest.json`, `dataset.jsonl`, `tasks/`, and `assets/`. Publish the complete directory through your existing storage tools, preserving its metadata. Bind both its local readable copy and its platform copy:

```toml
[task_releases.tb2]
local_root = "/srv/eval/releases/tb2"
platform_root = "pvc:YOUR-CLAIM/YOUR-ROOT/releases/tb2"
```

This example uses PVC storage; the claim and configured root subpath must match `[storage]`. For host-path storage, use its absolute host path (or a relative key under that configured root). A PVC binding must use the explicit `pvc:` form. Bindings do not upload or synchronize files. Keep the release immutable while jobs or restored trials reference it.

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
    dataset: /srv/eval/releases/tb2/dataset.jsonl
    deployment: /srv/eval/deployment.toml
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

`nop` is a lifecycle smoke agent, not a solver. Supply your intended Harbor agent and its credentials separately for evaluation. Do **not** set Harbor `tasks`/`datasets`, `verifier.import_path`, or `verifier.env`: the native dataset owns selection and the release owns verification. To filter or repeat tasks, write a selection JSONL retaining the complete original rows (`release_id`, `task_ref`, `task_digest`, annotations and parameters). Order and duplicate rows remain separate trials.

Relative `task_source.kwargs.dataset` and `deployment` resolve against the config that defines them. Snapshots freeze absolute external paths. Remote configs require absolute input paths. A standalone restored Trial revalidates its selection rather than depending on the old Job's in-memory cache.

The importer stores Harbor presentation metadata in `task.provenance.harbor.config`. The adapter reconstructs a metadata-backed Harbor Task from that field and the verified public instruction; it never scans original source tasks at evaluation time. Missing importer provenance, remote task downloads, multi-step and non-Linux tasks fail explicitly.

## Execution and ownership

- `task_source` owns local selection and metadata; `runtime` owns the Flotilla client, `NativeAttempt`, verification, artifacts and cleanup; `environment` is the Harbor I/O adapter. Configure all three together.
- Each attempt has private mutable volumes and verification output. Immutable assets may be shared read-only after launch-local integrity verification.
- Hidden tests are not exposed during the agent phase. A prepared task with an explicit separate verifier releases main before opening the verifier; artifact handoff remains platform-side. Without an explicit Harbor `[verifier.environment]`, preparation uses main for verification, even if an old source requested inherited `environment_mode="separate"`. Inspect the prepared roles rather than assuming that old hint created another sandbox.
- Original application and healthcheck UID/GID remain authoritative after host-side USER0 adaptation. Flotilla does not recursively rewrite published asset ownership or create application users.
- Task and job/trial result-artifact downloads use Harbor's ordinary trial output and manifest. Agent failures and disabled verification still collect available results before cleanup. They are distinct from release preparation and verifier handoff.
- A test script's nonzero exit with a valid reward may be a graded failure. Missing/malformed/nonfinite rewards and transport/cleanup failures are infrastructure errors, not reward zero.
- `environment.delete: false` and `force_build` are rejected. `runtime.kwargs.run_timeout` must be positive and within the deployment lifetime.

For non-Harbor applications, use the public `ReleaseBindings` and `NativeAttempt` API in [Native tasks](Native_Tasks.md). There is no task-key or old prepared-row fallback. The old source-workspace compiler is a legacy API, not this execution path.

## Acceptance

Check known positive/negative outcomes, same-role and explicit separate verification, nonempty handoff, original nonroot identities, hidden-test isolation, cancellation and failed cleanup. Require independent confirmation that platform resources were released. Simulated transport tests and successful CLI parsing are not real platform acceptance; see [Deployment](Deployment.md) for the evidence contract.
