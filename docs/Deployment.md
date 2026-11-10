# Deployment guide

A deployment provides a sandbox endpoint, an image registry, shared storage, native Anchor/shared tools and measured capability evidence. Users receive `deployment.toml`, its generated capability report and credentials through a separate channel. Field details are in [Configuration](Configuration.md).

## Separate preparation from execution

Host preparation needs the optional Harbor importer, pinned official Compose and Docker/registry tools. These tools run in an operator-controlled CPU environment, not in business sandboxes. They resolve source images, capture original application identity, publish required USER0 adaptations and produce immutable native releases.

The platform Anchor needs the matching installed Flotilla runtime and filesystem tools. It does **not** need Harbor, Compose, Docker/BuildKit, registry-push credentials or source task directories. Never expose a host Docker socket or unauthenticated build endpoint to task/verifier sandboxes. Normal pulls of the final fixed images remain a platform responsibility.

The old `images/management` compiler and workspace contracts describe the explicit legacy source-task path; they are not requirements or fallbacks for native consumption.

## 1. Build the installed Anchor

Install this repository's fixed commit or controlled wheel in a clean Python 3.12 Linux x86_64 environment. Do not use the unrelated public PyPI `flotilla` package. The builder snapshots the exact installed non-editable distribution and runtime dependency closure, validates RECORD hashes and uses packaged resources rather than a checkout.

```sh
flotilla anchor build \
  --image registry.example.org/team/anchor:VERSION \
  --python-image python@sha256:FULL_PYTHON_312_DIGEST \
  --push --authfile /private/registry-auth.json
```

The Python base must be pinned, compatible with the installed binary wheels and provide Python 3.12/venv. BusyBox is downloaded from a fixed source and verified. A fresh container checks the resulting helpers without network or source mounts. Publishing uses configurable Docker/Skopeo commands and a configurable manifest format (`v2s2` by default). No `latest` tag is changed. Record the reported **remote manifest digest**, not the local Docker image ID, in `[reaper].anchor_image`.

A successful build proves image self-containment, not platform behavior. Do not promote an image as platform-tested until the following checks pass.

## 2. Provision shared tools and storage

Copy the [deployment template](../deployments/examples/opensandbox.toml), replacing endpoint, image, network and storage values. Keep actual credentials and deployment evidence outside Git. The environment variable named by `opensandbox.credential_env` supplies runtime API credentials.

Use one shared root visible consistently to all workers and the Anchor. Assemble a share directory containing the controlled `bin/busybox` and retain its corresponding license/source information. Bootstrap does not require an existing capability report:

```sh
flotilla share publish share-root --deployment deployment.toml \
  --release initial --component busybox=1.36.1
```

Put its release name and `RELEASE.json` digest in `[share]`. Shared-tools releases are immutable. They are separate from native task releases and any Agent runtime.

## 3. Probe the actual deployment

Configure an ordinary probe image with the required shell/Python/socket tools and pin it by digest. Configure controlled TCP/UDP targets and DNS inputs as described in [Network validation](Network_Validation.md). Privileged images/device checks are optional, explicitly configured paths, not inferred from runtime names.

```sh
flotilla probe --deployment deployment.toml --env sandbox.env
flotilla share verify --deployment deployment.toml
```

Probe writes to the capability path declared in this deployment. `--out`, if supplied, must resolve to that same path; use a separate deployment file with a candidate capability path when evaluating candidates. The command applies required capability and security admission and fails if cleanup is not confirmed. It persists the measured report rather than replacing failures with manual success flags.

Inspect `passed`, `failed`, `skipped` and `error` independently. Selecting a filesystem protocol is not evidence of behavior. The native profile includes readonly-rootfs/tmpfs and missing/nested mount targets where supported, in addition to actual ownership/mode, non-root writes, capacity, file transfer and isolation checks. Symlink path support is not inferred from success on missing ordinary directories.

Relevant configuration changes invalidate evidence. Do not merge old reports, waive security gaps merely to pass migration, or treat unit mocks/historical runs as current platform acceptance. Probe, Anchor cleanup, platform client cleanup and owned-directory deletion all matter.

## 4. Prepare and publish task releases

Example host preparation settings:

```toml
[prepare]
concurrency = 4
# docker_command = ["sudo", "-n", "docker"]

[build]
pullable_registries = ["registry.example.org"]
target = "registry.example.org/team/task-runtime"
```

`--image-repo` overrides `build.target`. A target does not request mirroring all images: mirroring remains explicit. Keep registry credentials outside build contexts/releases. A read-only Docker auth directory may need a separate writable `BUILDX_CONFIG` directory for tool state; do not make the credential mount writable to accommodate it.

Use [Native tasks](Native_Tasks.md) for `select` and `prepare`. Users publish the resulting complete release with existing storage tools. Bind its local and platform locations explicitly:

```toml
[task_releases.example]
local_root = "./releases/example"
platform_root = "/shared/releases/example"
```

For host storage, the platform root is confined under `storage.host_path`. PVC deployments require an explicit `pvc:<claim>/<configured-root-subpath>/<release-key>` binding; bare relative roots are rejected. Preserve required metadata and symlinks during publication; both local and platform consumed files are verified. Releases must remain immutable while selected by any running job. Task upload is not a Flotilla backend feature.

Configure instance budgets, concurrency, creation/exec rates and timeouts from measured quotas. Admission includes replacement capacity and Anchor overhead. Private writable task state is allocated per attempt; immutable assets may be shared. Compose/build inputs cannot be changed through runtime overrides.

## Acceptance and maintenance

Use real native tasks covering multi-service networking, separate verifier roles, non-root applications, seeded private state, real tmpfs/readonly rootfs, hidden tests, artifact handoff and failure cleanup. Verify the native consumer works without source Harbor directories or build tools, and performs no build/push/mirror/image-seed-export during attempts. Agent evaluation and GPU training are separate checks, not consequences of an image or probe success.

`release()` and `close()` wait for confirmation. Inspect `CleanupError.report`; TTL is only a fallback. Independently confirm acceptance runs leave no owned instances or launch directories.

```sh
flotilla gc --deployment deployment.toml
flotilla share gc --deployment deployment.toml --keep previous --dry-run
```

GC is explicit, not a background schedule. Use `gc --launch ID` only after confirming that owner has exited. Review share GC's dry-run before deleting. Native releases and historical runs are not automatically migrated or deleted; old jobs retain their original code, images and evidence.
