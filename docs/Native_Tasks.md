# Portable native task releases

Flotilla owns the execution protocol. Harbor is an optional **host-side importer** and an optional evaluation consumer; it is not an execution dependency. Voyager uses the same reader and attempt lifecycle.

## Install and prepare tools

Use Linux x86_64 and Python 3.12. Install a wheel from this repository or a full Git commit from `https://github.com/matrix72c/flotilla.git`. The distribution, import and executable are `flotilla`. The public PyPI project with that name is unrelated: do not use bare `pip install flotilla`. Do not install `flotilla-compose` alongside the renamed package. Use a new environment rather than modifying an environment used by running jobs.

Install the `harbor` extra when importing Harbor tasks or using Harbor as a consumer. The normal native reader and execution path need neither Harbor nor Docker Compose. Host import uses the official pinned Compose normalizer; image preparation needs Docker and registry tools. Those tools do not belong in the execution Anchor. A CPU preparation environment does not require Torch, CUDA, XTuner or LMDeploy.

## Four stages

```sh
flotilla probe --deployment deployment.toml --env sandbox.env
flotilla select --host-root /data/harbor/tasks --deployment deployment.toml \
  --out selected.jsonl --report selection-report.json
flotilla prepare selected.jsonl --deployment deployment.toml --env prepare.env \
  --image-repo registry.example.org/team/task-runtime \
  --out /data/releases/example --report prepare-report.json
```

`probe` writes to the capability path declared in the deployment. An explicit `--out` must resolve to that same path. Probe applies the deployment's required capabilities and security policy and requires confirmed instance, Anchor and storage cleanup. Choosing a filesystem protocol is not proof of its behavior; measured evidence is bound to the deployment and includes missing mount targets.

`select` discovers and normalizes tasks on the preparation host. It does not create sandboxes, export image contents, build, publish, tokenize or split train/eval data. It writes importer inputs, not runnable native tasks. Multi-step Harbor tasks are rejected by the native protocol itself — the consume-only lifecycle is one agent run plus one verification — not by any consumer option. `voyager select` additionally requires published application images.

`prepare` resolves application execution identity, commands, environment, healthchecks and image digests **before** adaptation. Root-compatible images are reused. Non-root default images receive a minimal `FROM <source digest>` / `USER 0:0` image, while the original application UID/GID remains in the task. This does not enable privileged execution. tmpfs and readonly rootfs remain real runtime features; directory creation alone is not an image build reason. Unsupported image paths and identities are rejected, not approximated.

**Executing prepare may build and publish necessary images.** `--image-repo` is a repository without tag/digest and overrides `build.target`. It does not mean mirror every source image. Mirroring is explicit, no `latest` tag is moved, and final references use the remote manifest digest, not a local Docker image ID. Missing publication targets fail before the necessary build. Registry authentication remains outside the release and is not required by consume-only execution.

A failed selected row fails the release: no partial successful dataset is silently published. The output directory must not exist. Reports and resume work are separate from the release; use explicit `--resume` after correcting a failed attempt. Previously published images are recorded, not automatically deleted. Never mutate a release used by running jobs.

The prepare report includes invocation-local `metrics`: total `elapsed_s`, per-phase `calls`/`failed`/`seconds`, and cache-hit/resumed-adaptation counts. `import_normalize` combines importing and official Compose normalization; `user0_build_push` and `application_build_push` each measure the builder's combined build/publication call. Phase times are inclusive and may overlap, so do not sum them. Tool calls exclude cache hits, failures remain counted, and resume starts fresh measurements rather than adding old durations. Metrics do not change release identity.

## Static publication belongs to the user

Publish the complete output with your existing rsync, shared storage or object-storage tools. Flotilla does not upload a task dataset for you. Preserve bytes, modes and symlinks, and apply the numeric UID/GID recorded in each asset inventory at the destination. The preparation host need not have those owners locally: blindly copying its actual owners can fail platform integrity validation. Existing tar tools can carry inventory ownership for extraction by a trusted storage administrator. Validate the delivered copy with `ReleaseReader(root, ownership=True)` and read the selected tasks before using it. A gateway PUT acknowledgement may precede visibility through a separate NFS client; wait for visibility before consuming.

Publish shared-tools and the Agent runtime separately, and grant the platform read access to the fixed runtime images. Do not place credentials, source checkout paths or image archives inside the task release.

```text
release/
  manifest.json
  dataset.jsonl
  tasks/<task-variant>/task.json
  tasks/<task-variant>/instruction.md
  assets/<content-digest>/...
```

`task.json` is authoritative and carries role manifests, the verification interface, explicit artifact handoffs, resolved application identities and fixed images. Original Harbor and Compose files are not consulted by consumers. Static bind content and immutable volume seeds live in `assets/`. Writable volumes and verification outputs belong to each attempt.

Task/release identities include normalized definitions and static content/metadata. Machine paths and report timestamps do not participate. `dataset.jsonl` is a selection view: preserve or reorder rows, retain duplicates, and filter it without rebuilding the immutable release. Each row has `release_id`, `task_ref`, `task_digest`, `annotations` and non-secret `parameters`. Do not reduce identity to a task basename. Compiling the same source with different parameters produces different variants.

## Bind local and platform roots

```toml
[task_releases.example]
local_root = "./releases/example"
platform_root = "/shared/releases/example"
```

Local relative paths resolve against the deployment TOML, not the caller's working directory. For host storage, `platform_root` must be confined beneath the configured host root. For PVC storage use the explicit `pvc:<claim>/<configured-root-subpath>/<release-key>` location; bare relative roots are rejected. An arbitrary absolute host path is not treated as a PVC key by stripping its prefix.

Loading configuration for probe/select/prepare does not open future release paths. Consumption opens the selected releases, verifies local task and asset contents, verifies the platform copies, and refuses missing files, mismatched identities, partial uploads and unsupported protocol versions. Selection across several releases requires every release to be explicitly bound. Large immutable assets are verified once per launch, not once per attempt. Releases must be maintained by a trusted publisher and read-only to workloads; checksums are not a promise of TOCTOU protection for an adversarially writable directory.

An optional typed `[agent_runtime]` section binds a consumer's runtime layers (arbitrary names, each with local/platform roots); Flotilla validates shape and roots but not which layers exist — that contract belongs to the consumer. Flotilla does not interpret Agent types, GPU resources, reward or training configuration.

## Public native consumption API

```python
from flotilla import Flotilla
from flotilla.config import FlotillaConfig
from flotilla.native import NativeAttempt

config = FlotillaConfig.load("deployment.toml")
bindings = config.release_bindings()
rows = bindings.read_dataset("selected-native.jsonl")

async with Flotilla(deployment=config) as fleet:
    for row in rows:
        bound = bindings.resolve(row)
        async with await NativeAttempt.create(fleet, bound) as attempt:
            environment = await attempt.open("main")
            # Run the agent through the existing environment/service API.
            # Verification stages hidden tests and explicit role handoffs.
            result = await attempt.verify()
```

Import models/readers from `flotilla.native`; they do not load the importer or GPU libraries. `ReleaseWriter` is the library interface for creating native releases without Harbor. `ReleaseReader` reads one release; `ReleaseBindings` joins explicit deployment locations to dataset rows. The native lifecycle reuses the existing scheduler and prepared-environment executor, rather than implementing another scheduler.

Consumers may pull the fixed runtime images normally. They may not parse Harbor/Compose, resolve source tags, build, push, mirror, or export seed contents from images. Missing assets and incompatible packages fail explicitly; they never trigger preparation fallback. Infrastructure/cleanup failures are errors, not task scores. Reward interpretation belongs to the consumer.

## Installed Anchor builder

```sh
flotilla anchor build --image registry.example.org/team/anchor:VERSION \
  --python-image registry.example.org/python@sha256:FULL_DIGEST
# Add --push and optionally --authfile to publish explicitly.
```

The builder uses packaged assets and the exact installed non-editable Flotilla distribution plus its runtime dependencies. It validates installed RECORD contents and copies no source checkout, credentials or optional Harbor/compiler stack. It requires a pinned Python 3.12 linux/amd64 base and a verified pinned BusyBox asset. The base must support the installed runtime's ABI. Build context and downloaded archives are isolated temporary directories.

A new container verifies helpers and tools without network or source/credential mounts. Publication uses configurable Docker/Skopeo argv and manifest format (`v2s2` by default for the current internal registry). A deployment snippet is printed only after obtaining the remote digest. Local image IDs are not deployment pins. Platform behavior is still measured by probe; self-containment does not replace platform or GPU acceptance.

## Migration

- Old task-key/Harbor lists and prepared `env`/`task_dir` rows must be reselected and prepared. They are not interpreted as native releases.
- Old `flotilla prepare` meant platform warmup. Its library behavior is available only through the explicitly named `legacy_warmup` entry; the new command requires host selected inputs, an output release and a report.
- Old capability evidence must be regenerated for the native execution profile and cleanup requirement.
- Old jobs stay pinned to their old code and image. Do not rewrite snapshots, running worktrees, registry history or deployment evidence to change product names.
- Explicit legacy application-build APIs remain separate from native consumption. They do not authorize a consumer fallback.

Installation, fake-platform tests, real platform behavior and GPU training are different verification scopes. Consult the release's recorded acceptance results rather than assuming one proves the others.
