# Harbor quick start

Flotilla consumes original [Harbor](https://github.com/harbor-framework/harbor) task directories on platform storage. Each attempt makes a private platform-side copy, preserving the directory basename as its task ID. No prepared JSONL, locally mirrored task tree, or per-rollout upload of tests is required.

Install the pinned Harbor integration with `python -m pip install '.[harbor]'`, then follow the [deployment guide](Deployment.md). The trusted management image needs the same Flotilla/Harbor code, the pinned official Compose CLI and explicitly configured build tooling. Builder access and credentials are operator-configured; Flotilla does not start DinD or bake credentials into images. A new compatible management/runtime image and real platform acceptance are still required before deploying this refactor.

## Inputs and optional warmup

Use your own delivery script to copy the complete task directory (including `task.toml`, `instruction.md`, environment files and tests) into platform storage. Paths below are relative to the deployment's storage root:

```sh
flotilla prepare datasets/terminal-bench/task-a \
  --deployment deployment.toml --out warmup-diagnostics.json
```

This validates and warms image caches, then removes its workspace. Its output is diagnostic information, **not a training dataset**. Each later attempt compiles in a new workspace; image/build caches may be reused. Keep canonical data immutable during a run, and use versioned dataset keys when updating it.

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

This opens an environment; it does not run an evaluation or award a reward. The sandbox_rl integration drives the agent, native Harbor verifier/reward parsing, hidden-test staging and platform-side artifact transfer. See [Integration guide](Integration_Guide.md) for ownership and error handling.

**Native `harbor run --env ... --ek prepared=...` is no longer a supported entrypoint.** Native Harbor's Trial still assumes a local Task and uploads tests; it cannot be switched to remote task keys by passing an environment option. `FlotillaEnvironment.view(handle)` is the borrowed Harbor I/O adapter, not a remote Trial implementation. Direct legacy prepared inputs fail with migration guidance.

## Execution and ownership

- Original data is never mounted directly into an agent/verifier. All task-backed mounts use attempt-local data.
- The task ID is its directory basename. Duplicate IDs and repeated input rows are allowed; unique attempt IDs own resources and cleanup.
- Main and verifier environments have independent lifetimes. Releasing main does not remove the attempt workspace needed by a separate verifier.
- Hidden tests are staged after the agent phase. Bundled tests retain Harbor's bundled-test semantics.
- User identities and permissions remain task/image responsibilities. Flotilla does not recursively chmod/chown canonical data or create task users.
- Cleanup failures remain visible; do not accept an infrastructure failure as a normal zero reward.

## Acceptance

Use tasks with known positive and negative outcomes. Check shared, separate and bundled verification, real nonempty artifact handoff, permission-sensitive tasks, cancellation and failed deletion. Require the expected reward, no infrastructure exception and confirmed cleanup via an independent platform connection. A successful CLI exit alone is insufficient.

Unit and simulated-platform integration tests do not establish real platform equivalence. Official Compose normalization tests also do not replace Docker-versus-platform runtime differential checks of mounts, process lifecycle and networking. Unsupported semantics must fail explicitly; see [Compose compatibility](Compose_Compatibility.md).
