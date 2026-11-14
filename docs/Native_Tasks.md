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
  --out /data/releases/example --name example --report prepare-report.json
```

`probe` writes to the capability path declared in the deployment. An explicit `--out` must resolve to that same path. Probe applies the deployment's required capabilities and security policy and requires confirmed instance, Anchor and storage cleanup. Choosing a filesystem protocol is not proof of its behavior; measured evidence is bound to the deployment and includes missing mount targets.

`select` discovers and normalizes tasks on the preparation host. It does not create sandboxes, export image contents, build, publish, tokenize or split train/eval data. It writes importer inputs, not runnable native tasks. Multi-step Harbor tasks are rejected by the native protocol itself — the consume-only lifecycle is one agent run plus one verification — not by any consumer option. The selection report lists each service's image source — `image`, `build`, or `config-only` (a Dockerfile that only sets configuration) — and importer warnings. `--require-prebuilt-images` rejects tasks that need builds, for hosts without Docker.

`prepare` resolves application execution identity, commands, environment, healthchecks and image digests **before** adaptation. Root-compatible images are reused. Non-root default images receive a minimal `FROM <source digest>` / `USER 0:0` image, while the original application UID/GID remains in the task. This does not enable privileged execution. tmpfs and readonly rootfs remain real runtime features; directory creation alone is not an image build reason. Unsupported image paths and identities are rejected, not approximated.

A service with `build` gets its image in one of three ways, recorded per service in the prepare report as `dispositions` (`link`, `mirror`, `config-only`, `cache-hit`, `built`) and `adapted` (USER0):

- **config-only**: a single-stage Dockerfile using only `FROM`, `ARG`, `ENV`, `WORKDIR`, `USER`, `CMD`, `ENTRYPOINT`, `LABEL`, `EXPOSE`, `MAINTAINER` and `STOPSIGNAL` on a linkable or mirrored base. The base digest is used directly and the configuration Docker would produce is written into the task. It is built instead when the result depends on the build: base `ONBUILD` triggers, a `WORKDIR` other than `/` or the base working directory (a build creates it), variables the Dockerfile and base do not define, proxy arguments, a custom base `SHELL` for shell-form commands, or substitution forms other than `$VAR`, `${VAR}`, `${VAR:-word}` and `${VAR:+word}`.
- **cache-hit**: other Dockerfiles are keyed by content — Dockerfile, pinned base digests, build arguments other than proxies, base substitutions and the complete context tree (paths, bytes, modes and link targets). The image is published as `<target>:bk-<key>`; an existing tag is reused without building, across tasks and runs. A first non-reproducible build (unpinned package installs) is what later runs reuse. A base that cannot be pinned (`FROM $ARG`, unreachable registry) disables reuse for that build.
- **built**: built and pushed under the cache tag.

USER0 adaptations are tagged by source digest and target and likewise reused when the tag exists.

**Executing prepare may build and publish necessary images.** `--image-repo` is a repository without tag/digest and overrides `build.target`. It does not mean mirror every source image. Mirroring is explicit, no `latest` tag is moved, and final references use the remote manifest digest, not a local Docker image ID. Missing publication targets fail before the necessary build. Registry authentication remains outside the release and is not required by consume-only execution.

A failed selected row fails the release: no partial successful dataset is silently published. The output directory must not exist. Work files live in a temporary sibling directory that is removed afterwards; after correcting a failure, run prepare again. Previously published images are recorded, not automatically deleted. Never mutate a release used by running jobs.

`--name` names the release. Every dataset row carries it, and the consumer's deployment binds it as `[task_releases.<name>]`. A task selected several times is packaged once; its rows repeat.

The prepare report includes invocation-local `metrics`: total `elapsed_s`, per-phase `calls`/`failed`/`seconds` (`registry_lookup` counts cache-tag lookups), and counts of registry/seed cache hits, `build_cache_hits` and `config_only` services. `import_normalize` combines importing and official Compose normalization; `user0_build_push` and `application_build_push` each measure the builder's combined build/publication call. Phase times are inclusive and may overlap, so do not sum them. Tool calls exclude cache hits and failures remain counted. The Compose executable's version is checked once per process.

## Static publication belongs to the user

Publish the complete output with your existing rsync, shared storage or object-storage tools. Flotilla does not upload a task dataset for you. Preserve bytes and modes. File owners are not part of the release: task inputs are mounted read-only, and volume seeds are tar archives that carry their own numeric owners. A gateway PUT acknowledgement may precede visibility through a separate NFS client; wait for visibility before consuming.

Publish shared-tools and the Agent runtime separately, and grant the platform read access to the fixed runtime images. Do not place credentials, source checkout paths or image archives inside the task release.

```text
release/
  dataset.jsonl
  tasks/<task-key>/task.json
  tasks/<task-key>/instruction.md
  tasks/<task-key>/files/...      # bind content and volume seed tars
  tasks/<task-key>/tests/...      # hidden tests, staged only for verification
```

There is no central manifest and nothing is content-addressed: a release is this directory tree. `task.json` (protocol version 2) is authoritative and carries role manifests, the verification interface, explicit artifact handoffs, resolved application identities and fixed images; its file references are relative to the release root. Original Harbor and Compose files are not consulted by consumers. Writable volumes and verification outputs belong to each attempt.

`dataset.jsonl` is a selection view: preserve or reorder rows, retain duplicates, filter it, or concatenate rows of several releases without rebuilding anything. Each row has `release` (the release name), `task` (the task key, which is its directory under `tasks/`), `annotations` and non-secret `parameters`. Version 1 releases (`manifest.json`, `release_id`/`task_digest` rows) are not readable; prepare them again.

## Bind local and platform roots

```toml
[task_releases.example]
local_root = "./releases/example"
platform_root = "/shared/releases/example"
```

Local relative paths resolve against the deployment TOML, not the caller's working directory. For host storage, `platform_root` must be confined beneath the configured host root. For PVC storage use the explicit `pvc:<claim>/<configured-root-subpath>/<release-key>` location; bare relative roots are rejected. An arbitrary absolute host path is not treated as a PVC key by stripping its prefix.

Loading configuration for probe/select/prepare does not open release paths. Consumption reads only `task.json` and `instruction.md` of each selected task from the local root; nothing is hashed or re-verified locally or on the platform. A row's `release` must name a binding. Releases must be maintained by a trusted publisher and read-only to workloads.

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
