# Deployment guide

This guide is for whoever operates the sandbox platform. It is done once per deployment; afterwards every Flotilla user needs the resulting `deployment.toml`, generated `capabilities.json`, and the platform credential.

To try the whole procedure on one machine first, follow the [local quick start](Local_Quick_Start.md).

You need a running [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) server (see its getting-started guide), an image registry that both your build machine and the platform can reach, and shared storage. Field-by-field documentation is in the [configuration reference](Configuration.md).

Provision a dedicated shared-storage root reachable by every sandbox worker and the anchor. Stage complete original Harbor tasks under versioned relative keys there. The trusted management image needs Python 3.12, this Flotilla version with its `harbor` extra, fixed Compose (default 2.39.4), and Docker/BuildKit tooling. Use an isolated management sandbox with a local Docker Engine (for example, a supported Kata DinD deployment), or explicitly configure a remote build service. Configure management-only network targets and read-only credential mounts; never expose an unauthenticated Docker TCP endpoint or a host Docker socket to tasks. Runtime platform credentials come from the environment variable named by `opensandbox.credential_env`.

`images/management/build.sh` builds the self-contained management compiler from this checkout and the frozen dependency lock. Supply digest-pinned Python 3.12, Docker Engine DinD and historical anchor images; it includes the Engine binaries, the pinned base's Buildx CLI plugin (required even to resolve prebuilt image digests), and runtime tools, verifies the fixed Compose binary checksum, and tests imports plus Compose/Buildx binary execution without network or workspace mounts. The build check does not start a daemon. For example:

```sh
bash images/management/build.sh registry.example.com/team/management:candidate \
  'python@sha256:<python-3.12-digest>' \
  'docker@sha256:<docker-dind-digest>' \
  'registry.example.com/team/anchor@sha256:<anchor-digest>'
```

For Kata-capable OpenSandbox deployments, opt in only the workspace anchor with `prepare.management_profile = "management"` and `opensandbox.management_extensions.runtimeClassName = "kata"`. Set `prepare.management_resources` for builds (for example 2 CPU / 4Gi); ordinary task, probe and GC sandboxes keep their existing runtime. Verify automatic daemon startup with the actual anchor entrypoint, `docker info`, a build/run/export cycle and storage operations. Engine installation alone is not this evidence. Keep credentials and the Docker socket outside all task contexts and mounts.

The script builds only; it does not publish or promote a deployment. A working-tree candidate is marked `.dirty` in its image version. Publish a clean committed build and record its remote manifest digest only after acceptance. The historical anchor alone supplies file-operation tools, not the complete compiler. Neither a successful image build nor replacing its digest proves build-service connectivity, credential access or real platform acceptance.

Copy [the deployment template](../deployments/examples/opensandbox.toml) as `deployment.toml`, the sole handwritten deployment input. Replace endpoint, storage, registry, image, and network values. Keep real deployment files and capability reports out of version control; `deployments/local/` is ignored by Git for this purpose.

## Provision once

1. Build and push the anchor with `images/anchor/build.sh registry.example.com/team/anchor --push`. Record the digest. Its controlled BusyBox build must include `stat`, shell, file-management, and networking applets. Pin that digest in `reaper.anchor_image`.
2. Assemble a share directory containing `bin/busybox`. Use the [share layout and scripts](../deployments/examples/README.md); record component versions and their corresponding source/license material.
3. Publish the share before the first probe; this bounded management operation needs no existing report:

```sh
flotilla share publish share-root --deployment deployment.toml \
  --release initial --component busybox=1.36.1
```

Paste the resulting release name and `RELEASE.json` SHA256 into `[share]`. Releases are immutable. Changing bytes requires a new release name.

4. Provision an ordinary probe image with the configured execd interface and shell/Python socket tools; pin its digest in `[probe].image`. Configure `privileged_image` and `inner_image` for the actual privileged path, if used. A DinD image and an ordinary code-interpreter image serve different purposes; neither image name proves capability. Configure controlled TCP/UDP echo targets and DNS input as described in [network validation](Network_Validation.md).

```sh
flotilla probe --deployment deployment.toml --out capabilities.candidate.json
```

Inspect the complete `passed`/`failed`/`skipped`/`error` summary and cleanup result. After acceptance, promote the candidate to the configured `capabilities.json` path. Then verify the immutable share:

```sh
flotilla share verify --deployment deployment.toml
```

No declaration file, manual success flags, old-report merging, automatic package installation or protocol/runtime fallback is supported. A missing image tool is an incomplete check, not proof that the platform lacks the feature. Ordinary and privileged evidence are separate. Unsupported filesystem dialects and unconfigured optional runtime/device checks remain skipped. KVM is not inferred from Kata; FUSE needs the actual runtime extension plus successful mount/read/write/unmount behavior.

The report binds operational configuration, storage mapping, shared-tool release and runtime protocols. Relevant changes require a new report even if the endpoint is unchanged. Legacy schema-1 reports are rejected rather than converted into success. Preserve explicit security waivers, but never add a waiver merely to make a migration green. No offline test or historical deployment result supplies current platform acceptance.

## Preparation and budgets

```toml
[prepare]
concurrency = 4
# docker_command = ["sudo", "-n", "docker"]

[build]
pullable_registries = ["registry.example.com"]
target = "registry.example.com/team/tasks"
```

`HarborAttempt.create` and optional `flotilla prepare <task_key>` warmup use `[prepare]` and `[build]` inside the trusted platform management environment. Warmup writes diagnostics, not prepared JSONL training input. `prepare.management_network` selects explicit remote build/registry destinations, `management_mounts` supplies read-only credential/config files, and `management_env` names nonsecret endpoints and paths. Probe, GC and share bootstrap do not inherit these extra mounts or egress. No credentials are embedded in images, no DinD is started automatically, and no unrestricted egress is inferred. Separately set `limits.trial_concurrency`, `instance_budget`, create/exec rates, and timeouts according to measured platform quotas. Startup reserves replacement capacity; the anchor also consumes an instance.

For a private registry, explicitly mount an operator-owned Docker credential directory
read-only into management sandboxes and point `prepare.management_env.DOCKER_CONFIG`
at it. Keep it outside task sources and build contexts; never add it to task or verifier
mounts. Buildx also writes local state under `DOCKER_CONFIG` by default, so set
`prepare.management_env.BUILDX_CONFIG` to a separate writable management-local directory
(for example `/tmp/flotilla-buildx`). Do not make the credential mount writable to satisfy
Buildx. Allow the registry's authentication service and required blob endpoints in the
management network policy as well as the registry hostname itself.

## Maintain

Public `release()` and `close()` wait for cleanup confirmation up to their deadline. Inspect `CleanupError.report` when cleanup is incomplete; closing a caller does not prove remote deletion. TTL is a final fallback. Use an independent platform client to verify real acceptance runs leave no labeled instances or owned launch directories.

The bounded workspace contract can test a candidate management image before full fleet admission:

```sh
python tests/contract/workspace.py --deployment deployment.toml \
  --image 'registry.example.com/team/management@sha256:<candidate-digest>' \
  --out workspace-contract.json
```

It checks duplicate task keys/basenames, attempt isolation, modes, hardlinks and relative symlinks, native instruction parsing, private test staging, and cleanup on real storage. Add `--runtime --runtime-image 'registry.example.com/team/python@sha256:<digest>'` on an admitted deployment to also exercise actual shared and separate verifier roles, read-only artifact handoff, reward files, and cleanup. The fixture image needs Python and a POSIX shell. The separate role inherits the main environment so its tests are staged externally; an explicit native verifier image instead declares bundled tests. These checks do **not** replace the capability probe, Docker/runtime differential testing, real Agent evaluation, or an end-to-end training run.

```sh
flotilla gc --deployment deployment.toml
flotilla share gc --deployment deployment.toml --keep previous --dry-run
```

GC is an explicit operator command, not a background schedule. It also removes `tasks/.staging`, `tasks/.trash` and `share/staging` entries older than `--staging-age` (default 24 h), judged by the timestamp in their names; entries without one are listed, not removed. When its anchor cannot start, GC deletes sandboxes by label only, reports the directories it left, and exits 1. `gc --launch ID` skips the two-observation wait and is appropriate only after confirming that owner has exited. Shared task publications and previous storage formats are never automatically migrated or deleted. Review share GC's dry-run before running it without `--dry-run`.
