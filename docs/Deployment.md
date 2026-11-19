# Deployment guide

A deployment provides a sandbox endpoint, an image registry, shared storage and the native Anchor/shared tools. Users receive the deployment values and credentials through a separate channel, and construct a `FlotillaConfig` from them; isolation is measured by running `flotilla probe` against the deployment, which writes nothing. Field details are in [Configuration](Configuration.md).

## What the deployment has to provide

Flotilla resolves a task where it runs it: it reads the published task directory and each image's config from the registry, over HTTP and by digest. So a client needs registry **read** access and nothing else — no builder, no Compose executable, no Docker.

The platform Anchor needs the matching installed Flotilla runtime and filesystem tools. It does **not** need Docker/BuildKit or registry-push credentials. Never expose a host Docker socket or unauthenticated build endpoint to task/verifier sandboxes. Normal pulls of the fixed images remain a platform responsibility.

## 1. Build the Anchor image

Run the builder from this repository's fixed commit or controlled wheel in a Python 3.12 Linux x86_64 environment. Do not use the unrelated public PyPI `flotilla` package. The image is a scratch filesystem containing one hash-verified static BusyBox plus its license and identity files; it contains no Python, package index access, or source checkout.

```python
from flotilla.anchor_build import AnchorBuildOptions, build_anchor

result = build_anchor(
    AnchorBuildOptions(
        image="registry.example.org/team/anchor:VERSION",
        push=True,
        authfile=Path("/private/registry-auth.json"),
    )
)
print(result.anchor_pin)   # None unless push succeeded
```

BusyBox is downloaded from a fixed source and verified against pinned hashes. A fresh container exercises the resulting applets without network or source mounts. Publishing uses configurable Docker/Skopeo commands and a configurable manifest format (`v2s2` by default). No `latest` tag is changed. `anchor_pin` is the **remote manifest digest** to put in `reaper.anchor_image`; it is `None` when nothing was published, because a local Docker image ID is not something the platform can pull.

A successful build proves image self-containment, not platform behavior. Do not promote an image as platform-tested until the following checks pass.

## 2. Provision shared tools and storage

Copy the [deployment template](../deployments/examples/opensandbox.py) into your own project, replacing endpoint, image, network and storage values. Keep actual credentials outside Git. The environment variable named by `opensandbox.credential_env` supplies runtime API credentials.

Use one shared root visible consistently to all workers and the Anchor. Without `[share]`, the deployment uses the built-in shared-tools release `busybox-1.36.1-r31`: probe and runtime start publish it into the work root from the Anchor image's own pinned BusyBox when it is missing, with no network access. A custom release is optional; assemble a directory containing the controlled `bin/busybox`, retain its license/source information and publish it:

```python
from flotilla.deployment import Deployment
from flotilla.share.release import publish_release

async with Deployment(PLATFORM).management_anchor() as anchor:
    result = await publish_release(anchor, "initial", Path("share-root"), components={"busybox": "1.36.1"})
print(result.release, result.release_sha256)
```

Then put its release name and `RELEASE.json` digest in `[share]`. Shared-tools releases are immutable. They are separate from datasets and any Agent runtime.

## 3. Declare `[platform]` and measure isolation

Declare the platform numbers a request cannot discover: how long a listing may lag, any TTL cap, the label and link limits, and the implicit egress with the resolver IPs behind it. Getting one wrong refuses nothing and misbehaves later, which is why they are stated rather than assumed. Everything else the platform either does or refuses at create.

Then configure an ordinary probe image with the required shell, Python and socket tools, pinned by digest, and the controlled TCP/UDP targets and DNS inputs described in [Network validation](Network_Validation.md). A privileged image is optional and explicitly configured, never inferred from a runtime name.

```sh
flotilla probe --deployment myproject.deployment:PLATFORM --env sandbox.env
```

The probe writes nothing. It prints each isolation property with what it observed and exits nonzero when one did not hold and `security.accept_insecure` does not name it, or when cleanup was not confirmed. The observations exist only for the length of the run, so read them there: a client in another process cannot re-derive them, and no later startup re-checks them.

Measure again after the platform itself changes. A waiver is not a migration tool — it records that you run without the property — and unit tests or an earlier run against another deployment are not acceptance of this one.

## 4. Publish and bind datasets

Every service image must already be published where the platform can pull from, and named by digest. Flotilla reads registries and never writes to them, so the registry credentials a client uses need pull access only. Keep them out of datasets.

Users publish task directories with existing storage tools; see [Native tasks](Native_Tasks.md). Bind each dataset's local and platform locations explicitly:

```python
datasets={"example": DatasetBinding(local_root="/srv/datasets/example", platform_root="/shared/datasets/example")}
```

For host storage, the platform root is confined under the dataset's root (the `inputs[<input>]` entry the binding names, otherwise `storage`). PVC deployments require an explicit `pvc:<claim>/<configured-root-subpath>/<key>` binding; bare relative roots are rejected. `local_root` must be absolute: a spec outlives the directory it was written in. Preserve required metadata and symlinks during publication. A dataset must stay immutable while any running job reads it. Dataset upload is not a Flotilla backend feature.

To consume datasets published in another owner's storage, keep `storage` on storage this deployment owns and declare the owner's read-only shares as named input roots; the built-in shared-tools release is published into the work root on first start. Different payloads may come from different roots. For example, with platform storage identifiers:

```python
storage=Storage(                   # this team's writable root
    host_path="@user",
    root_subpath="flotilla-work",
),
inputs={
    # The owner's directories, shared read-only.
    "datasets": Storage(host_path="@user:owner", root_subpath="datasets"),
    "runtimes": Storage(host_path="@user:owner", root_subpath="runtimes"),
},
datasets={
    "example": DatasetBinding(
        input="datasets",                            # the input root it lives on
        local_root="/data/datasets/example",
        platform_root="example/v1",
    )
},
```

The owner uploads task directories into those directories with their own storage tools. Read-only enforcement for other consumers is the platform's sharing permission; Flotilla only ever mounts input roots read-only.

Once a dataset is bound, check it before anything is created:

```sh
flotilla inspect --deployment myproject.deployment:PLATFORM          # every declared dataset
flotilla inspect example --deployment myproject.deployment:PLATFORM  # one of them
```

Each task is planned exactly as `open` would plan it, so a task that passes here is one that opens there. Nothing is created and nothing is written unless `--out` names a report file; a nonzero exit means at least one task would not open. Running it on the path that submits your jobs is what turns it into a gate — Flotilla provides the check and the exit code, not the policy.

Configure instance budgets, concurrency, creation/exec rates and timeouts from measured quotas. Admission includes replacement capacity and Anchor overhead. Private writable task state is allocated per attempt; the task's own read-only files are mounted where they are published and shared by every attempt. A task's images and topology cannot be changed through runtime overrides.

## Acceptance and maintenance

Use real native tasks covering multi-service networking, separate verifier roles, non-root applications, seeded private state, real tmpfs/readonly rootfs, hidden tests, artifact handoff and failure cleanup. Verify the native consumer works without source Harbor directories or build tools, and performs no build/push/mirror/image-seed-export during attempts. Agent evaluation and GPU training are separate checks, not consequences of an image or probe success.

`release()` and `close()` wait for confirmation. Inspect `CleanupError.report`; TTL is only a fallback. Independently confirm acceptance runs leave no owned instances or launch directories.

```sh
flotilla gc --deployment myproject.deployment:PLATFORM
```

GC is explicit, not a background schedule. Use `gc --launch ID` only after confirming that owner has exited. Releases under `share/releases/` that no deployment pins can be removed through `Deployment.management_anchor()` like any other shared-storage work. Native releases and historical runs are not automatically migrated or deleted; old jobs retain their original code, images and evidence.
