# Harbor quick start

Flotilla runs original [Harbor](https://github.com/harbor-framework/harbor) tasks from platform storage. Each attempt makes a private platform-side copy, preserving the directory basename as its task ID. Native `harbor run` additionally reads metadata from a local original-task source mapped to those platform keys; it does not upload that source or use a prepared dataset. The lower-level attempt API can use platform keys alone.

Install with `python -m pip install '.[harbor]'`. The extra pins the **compatible patched Harbor build** at [`d78eb9c262e7074944106d16bff0ba6426bb2d34`](https://github.com/matrix72c/harbor/commit/d78eb9c262e7074944106d16bff0ba6426bb2d34), providing the explicit `task_source`, trial `runtime`, and metadata-backed `Task.from_metadata` interfaces. Stock Harbor 0.24.0 does not provide this native entrypoint; installing an unpatched Harbor version or changing only the environment class is insufficient. Follow the [deployment guide](Deployment.md) for the trusted management image, which needs matching Flotilla/Harbor code, pinned official Compose and explicitly configured build tooling. Builder access and credentials are operator-configured; Flotilla does not start DinD or bake credentials into images. A compatible released image and real platform acceptance are still required before deploying this integration.

## Inputs and optional warmup

Use your own delivery script to copy the complete task directory (including `task.toml`, `instruction.md`, environment files and tests) into platform storage. Paths below are relative to the deployment's storage root:

```sh
flotilla prepare datasets/terminal-bench/task-a \
  --deployment deployment.toml --out warmup-diagnostics.json
```

This validates and warms image caches, then removes its workspace. Its output is diagnostic information, **not a training dataset**. Each later attempt compiles in a new workspace; image/build caches may be reused. Keep canonical data immutable during a run, and use versioned dataset keys when updating it.

## Native `harbor run`

Keep a local original dataset at `data/terminal-bench/` and deliver the same version to platform storage under `datasets/terminal-bench/`. A local `data/terminal-bench/task-a` then maps to the platform key `datasets/terminal-bench/task-a`. This mapping does not copy, publish, or synchronize data. Keep both sources immutable and accessible for the entire run.

Save the following as `/srv/eval/harbor-job.yaml`, with original tasks in `/srv/eval/data/terminal-bench` and the deployment in `/srv/eval/deployment.toml` (replace these example absolute paths with your own). Relative `sources[].local_root` paths resolve against the config file that defines `sources`, not the process working directory. Other Harbor paths retain their existing semantics: the example uses absolute dataset and deployment paths so it can run from any directory. The `nop` agent is a lifecycle smoke example, not a solver; replace it with your intended native Harbor agent configuration for evaluation. Provide platform and agent credentials through their documented external credential inputs, never in the task or example config.

```yaml
job_name: flotilla-native-smoke
n_concurrent_trials: 1
agents:
  - name: nop
datasets:
  - path: /srv/eval/data/terminal-bench
    task_names: [task-a]
task_source:
  import_path: flotilla.integrations.harbor_native:FlotillaTaskSource
  kwargs:
    sources:
      - local_root: data/terminal-bench
        platform_prefix: datasets/terminal-bench
    metadata_workers: 8
runtime:
  import_path: flotilla.integrations.harbor_native:NativeFlotillaRuntime
  kwargs:
    deployment: /srv/eval/deployment.toml
    runtime_params: {}
environment:
  import_path: flotilla.integrations.harbor_native:NativeFlotillaEnvironment
  delete: true
verifier:
  import_path: flotilla.integrations.harbor_native:NativeWorkspaceVerifier
```

```sh
harbor run --config /srv/eval/harbor-job.yaml
```

Harbor preserves source-kwarg origins across layered configs and Job/Trial JSON snapshots. Overriding only `metadata_workers` does not rebase inherited `sources`; replacing the `sources` list uses the replacing file's directory. Once loaded, the native source stores absolute external roots in snapshots; it does not copy the task tree. Remote config URLs require absolute `local_root` paths. Direct Python construction without config-origin metadata retains CWD-relative roots; use absolute roots when saving such configs before loading tasks.

For explicit selection, replace `datasets` with `tasks: [{path: /srv/eval/data/terminal-bench/task-a}]`; do not supply both unless you intend to concatenate their selections. Dataset discovery examines immediate child directories containing `task.toml`, applies Harbor's name/exclusion/count filters, and reads only selected configs. Instructions are loaded when their individual Trial is created. Git/package downloads, multi-step tasks and non-Linux environments are not supported by this adapter.

`task_source` owns local selection and metadata; `runtime` owns the Flotilla client, attempt and role transitions; `environment` is the native Harbor I/O adapter. Configure all three together. The runtime selects the platform-aware verifier above even when its `import_path` is omitted; unrelated verifier overrides and `environment.delete: false` are rejected. `runtime.kwargs.run_timeout`, if supplied, must be positive and no greater than the deployment's runtime lifetime.

Source mappings use component boundaries, reject overlapping roots/prefixes and keep metadata reads confined to their source/task roots. `metadata_workers` defaults to 8: spawn workers process batches of 64, with at most twice the effective worker count submitted; small selections reduce worker count, and 1 uses no subprocess. No persistent metadata cache or payload-tree hash is created. Before any task environment opens, the runtime compares local and platform **parsed config plus canary-stripped instruction**; a mismatch fails and closes the workspace. This is not a full-tree consistency check, so immutable, matching source versions remain required. Run-specific extra instructions do not change this original-task identity.

## Attempt API

```python
import asyncio
from flotilla import Flotilla
from flotilla.integrations.harbor_attempt import HarborAttempt

async def main():
    async with Flotilla(deployment="deployment.toml") as fleet:
        attempt = await HarborAttempt.create(fleet, "datasets/terminal-bench/task-a")
        try:
            env = await attempt.open("main")
            result = await env.agent.exec(["pwd"], timeout=30)
            assert result.exit_code == 0
        finally:
            await attempt.close()

asyncio.run(main())
```

This opens an environment; it does not run an evaluation or award a reward. Native Harbor uses the configured trial runtime above; sandbox_rl drives its own agent loop. Both use shared Flotilla verification lifecycle and platform-side artifact handoff. See [Integration guide](Integration_Guide.md) for ownership and error handling.

**`harbor run --env ... --ek prepared=...` remains unsupported.** Use the explicit source/runtime/environment configuration, not prepared JSONL, a global factory patch, or an environment option pretending to be a remote task. `FlotillaEnvironment.view(handle)` remains a borrowed I/O adapter for integrations that already own their attempt.

## Execution and ownership

- Original data is never mounted directly into an agent/verifier. All task-backed mounts use attempt-local data.
- The task ID is its directory basename. Duplicate IDs and repeated input rows are allowed; unique attempt IDs own resources and cleanup.
- With Harbor's `verifier.environment_mode="shared"`, verification uses the existing main environment after the agent phase. With `"separate"`, artifact collection finishes first, main and its remaining sidecars are released, hidden tests are staged, and only then is the verifier opened. The attempt workspace survives this transition.
- Separate verifier definitions may inherit the main environment or declare an explicit verifier environment, following Harbor's native rules. Bundled tests stay image-owned rather than being replaced by a host test mount.
- Artifact handoff stays in per-service platform scratch and honors native destinations, exclusions and required-artifact failures. Native Harbor saves result artifacts and their manifest to its ordinary trial output, including job/trial `artifacts` in addition to task-declared artifacts. Disabled verification and agent failure still trigger result-artifact collection before close. These result downloads are separate from platform verifier handoff; they are not task-source copies or a metadata cache.
- User identities and permissions remain task/image responsibilities. Flotilla does not recursively chmod/chown canonical data or create task users.
- Cleanup failures remain visible; do not accept an infrastructure failure as a normal zero reward.

## Acceptance

Use tasks with known positive and negative outcomes. Check shared, separate and bundled verification, real nonempty artifact handoff, permission-sensitive tasks, cancellation and failed deletion. Require the expected reward, no infrastructure exception and confirmed cleanup via an independent platform connection. A successful CLI exit alone is insufficient.

Unit and simulated-platform integration tests do not establish real platform equivalence. Official Compose normalization tests also do not replace Docker-versus-platform runtime differential checks of mounts, process lifecycle and networking. Unsupported semantics must fail explicitly; see [Compose compatibility](Compose_Compatibility.md).
