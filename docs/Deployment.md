# Deployment guide

This page is for whoever operates the sandbox platform: what a deployment must provide, how to build the images and measure isolation, and how each optional feature is enabled and verified. Field details are in [Configuration](Configuration.md).

A deployment provides a sandbox endpoint, an image registry, shared storage and the native Anchor/shared tools. Users receive the deployment values and credentials through a separate channel and construct a `FlotillaConfig` from them. Isolation is measured by running `flotilla probe` against the deployment, which writes nothing.

## What the deployment has to provide

Flotilla resolves a task where it runs it: it reads the published task directory and each image's config from the registry, over HTTP and by digest. A client needs registry **read** access and nothing else — no builder, no Compose executable, no Docker. The Anchor is a scratch image with one pinned BusyBox: no Python, Docker/BuildKit or registry-push credentials. Never expose a host Docker socket or unauthenticated build endpoint to task/verifier sandboxes.

Required on every deployment:

- **Lifecycle API.** Server root with `/v1`; create, get, list by label, delete, and TTL renewal for single-container instances. Create must accept the common top-level `networkPolicy` field and the managed create fields (image digest, entrypoint, metadata, timeout, resource limits, volumes, env). A lost create response is reconciled by labels, so `list(labels)` must find an instance by them.
- **Exec daemon.** One exec daemon per sandbox, reachable through the platform's sandbox proxy route (declare an explicit port in `opensandbox.create_fields` when required). It runs as root to initialize the sandbox and apply each command's numeric identity. Exactly one protocol is selected in `[opensandbox.execd]` — `current` or `legacy` — with no fallback. See [OpenSandbox backend](backends/opensandbox.md).
- **Shared storage.** One shared root visible consistently to every sandbox worker and the Anchor: a host path backed by the same storage on each worker, or an existing RWX PVC with a subpath root. Instances receive only their declared mounts; undeclared volumes must not be auto-mounted.
- **Image registry.** Reachable from the sandbox workers, with digest-pinned pulls and linux/amd64 selection. Flotilla reads registries and never writes to them, so client credentials need pull access only.
- **Share release and anchor.** Flotilla publishes an immutable BusyBox share under the shared root and mounts it read-only at `/.flotilla`, so the binary is at `/.flotilla/bin/busybox`. The digest-pinned anchor image runs BusyBox for directory management, numeric metadata, renewal, and cleanup audits.
- **Quotas and labels.** Bounded-length sandbox labels for ownership and reconciliation; instance/TTL quotas at least matching the client's `[limits]` and `[reaper]` budgets, including the anchor instance and startup replacement capacity.

## 1. Build the Anchor image

Run the builder from this repository's fixed commit or a controlled wheel in a Python 3.12 Linux x86_64 environment. Do not use the unrelated public PyPI `flotilla` package. The image is a scratch filesystem with one hash-verified static BusyBox plus its license and identity files; it contains no Python, package index access, or source checkout.

```python
from pathlib import Path

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

BusyBox is downloaded from a fixed source and verified against pinned hashes, and a fresh container exercises the applets without network or source mounts. Publishing uses configurable Docker/Skopeo commands and manifest format (`v2s2` by default for the current internal registry); no `latest` tag is changed. `anchor_pin` is the **remote manifest digest** to put in `reaper.anchor_image`, and is `None` when nothing was published — a local Docker image ID is not something the platform can pull.

## 2. Provision shared tools and storage

Copy the [deployment template](../deployments/examples/opensandbox.py) into your project, replacing endpoint, image, network and storage values; keep credentials outside Git. The variable named by `opensandbox.credential_env` supplies runtime API credentials.

Use one shared root visible consistently to all workers and the Anchor. Without `[share]`, the deployment uses the built-in release `busybox-1.36.1-r31`: probe and runtime start publish it into the work root from the Anchor image's own pinned BusyBox when missing, with no network access. A custom release is optional; assemble a directory with the controlled `bin/busybox`, retain its license/source information, and publish it:

```python
from pathlib import Path

from flotilla.deployment import Deployment
from flotilla.share.release import publish_release

async with Deployment(PLATFORM).management_anchor() as anchor:
    result = await publish_release(anchor, "initial", Path("share-root"), components={"busybox": "1.36.1"})
print(result.release, result.release_sha256)
```

Put the release name and the returned `release_sha256` in `[share]`. Shared-tools releases are immutable and are separate from datasets and any agent runtime.

## 3. Declare `[platform]` and measure isolation

Most platform behavior is discovered by request: a platform that cannot host a device, a TTL or a tmpfs refuses the create, and that refusal is a better answer than a stored report. Two kinds of fact cannot work that way, because getting one wrong refuses nothing and misbehaves later, so they are declared in `[platform]`: how long a listing may lag (`list_visibility_s`), any TTL cap (`max_ttl_s`), the label and link limits, and the implicit egress with the resolver IPs behind it. `reaper.grace + platform.list_visibility_s < reaper.gc_window` and `reaper.anchor_ttl <= platform.max_ttl_s` are checked when the config loads.

Isolation is the exception — a deployment that ignores an egress policy, mounts storage a sandbox never asked for, or serves the exec channel without checking the token returns success anyway — so `flotilla probe` measures isolation, and only isolation. Configure an ordinary probe image with the required shell, Python and socket tools, pinned by digest, plus the controlled TCP/UDP targets and DNS inputs (see [Isolation measurement](#isolation-measurement)). A privileged image is optional and explicitly configured, never inferred from a runtime name.

```sh
flotilla probe --deployment myproject.deployment:PLATFORM --env sandbox.env
```

The probe writes nothing. It prints each isolation property with what it observed and exits nonzero when one did not hold and `security.accept_insecure` does not name it, or when cleanup was not confirmed. The observations exist only for the length of the run: a client in another process cannot re-derive them, and no later startup re-checks them, so the exit status is the gate. Measure again after the platform itself changes. A waiver records that you run without a property; it neither implements nor claims it.

## 4. Publish and bind datasets

Every service image must already be published where the platform can pull from, and named by digest. Users publish task directories with existing storage tools (see [Running tasks](Native_Tasks.md)). Bind each dataset's local and platform locations explicitly:

```python
datasets={"example": DatasetBinding(local_root="/srv/datasets/example", platform_root="/shared/datasets/example")}
```

For host storage, the platform root is confined under the dataset's root (the `inputs[<input>]` entry the binding names, otherwise `storage`). PVC deployments require an explicit `pvc:<claim>/<configured-root-subpath>/<key>` binding; bare relative roots are rejected. `local_root` must be absolute: a spec outlives the directory it was written in. A dataset must stay immutable while any running job reads it.

To consume datasets published in another owner's storage, keep `storage` on storage this deployment owns and declare the owner's read-only shares as named input roots:

```python
storage=Storage(host_path="@user", root_subpath="flotilla-work"),  # this team's writable root
inputs={
    "datasets": Storage(host_path="@user:owner", root_subpath="datasets"),
    "runtimes": Storage(host_path="@user:owner", root_subpath="runtimes"),
},
datasets={
    "example": DatasetBinding(input="datasets", local_root="/data/datasets/example", platform_root="example/v1"),
},
```

Read-only enforcement for other consumers is the platform's sharing permission; Flotilla only ever mounts input roots read-only. Once a dataset is bound, check it before anything is created:

```sh
flotilla inspect --deployment myproject.deployment:PLATFORM          # every declared dataset
flotilla inspect example --deployment myproject.deployment:PLATFORM  # one of them
```

Each task is planned exactly as `open` would plan it, so a task that passes here is one that opens there. A nonzero exit means at least one task would not open. Making that a submit gate is the submitter's policy — Flotilla provides the check and the exit code. Configure instance budgets, concurrency, creation/exec rates and timeouts from measured quotas; admission includes replacement capacity and Anchor overhead.

## Feature enablement

A feature below is available once the deployment provides it and the configuration selects it. Nothing measures the functional ones first — a platform that cannot host them refuses the create.

| Feature | Platform must provide | Selected in |
|---|---|---|
| Service peer links | Exact pairwise allow between linked services, via raw peer-IP/CIDR or `sandbox:<id>` identity targets, behaviorally enforced | `opensandbox.network.link = "cidr"` (needs `sandbox_cidrs`) or `"sandbox"` |
| Co-scheduling groups | Place all instances of one environment in one scheduling group | `opensandbox.group_extension` (only if the deployment implements it) |
| Inbound isolation | Platform-level default-deny between tenants, or identity-based sandbox ingress rules | `opensandbox.network.ingress = "deployment"` or `"sandbox"` (the latter requires `link = "sandbox"`) |
| Offline (`no-network`) tasks | Egress `defaultAction=deny` with no external rules; only cluster DNS on TCP/UDP 53 tolerated | `[network]` external `none`; `[probe]` targets and DNS name |
| External egress allowlist / any | Egress compilation to none, an allowlist, or `any` | `[network] external`; `platform_cidrs` required for `any` with `link = "cidr"` |
| Read-only rootfs / native tmpfs / `shm_size` | The `sandbox-system-v1` lifecycle dialect: create-time `readOnlyRootFilesystem`, `tmpfsMounts`, shm extension, effective-configuration checks | `opensandbox.filesystem_protocol = "sandbox-system-v1"` (default `"upstream"` rejects such tasks) |
| UDP between services | UDP policy enforcement between linked peers, in addition to TCP | Same selectors as peer links (`link_udp` capability) |
| Host devices (`/dev/fuse`, `/dev/kvm`, `/dev/net/tun`) | A privileged runtime that exposes the device node when the create carries the device extension; TUN also needs `CAP_NET_ADMIN` in the sandbox netns | `opensandbox.privileged_extensions` selects the runtime; Flotilla adds the device key per instance |

A task exceeding `platform.link_max_members` is rejected before any create. A task declaring a device or `privileged: true` is planned onto the privileged runtime; whether that runtime exists is the platform's answer to the create. Isolation features (inbound isolation, offline, egress) are measured by `flotilla probe`, not announced by configuration. The filesystem and device features are verified by opening a task that uses them on the real deployment and observing from inside the sandbox; `flotilla probe` does not check them.

## Isolation measurement

`flotilla probe` creates real sandboxes and measures seven properties: `external_none`, `network_acceptance`, `inbound_isolation`, `read_only`, `subdir_isolation`, `no_auto_mount` and `exec_auth`. Each is measured by **allowing** traffic, **denying** it, and **allowing** it again — a single blocked exchange proves nothing, because it could be a broken target. Missing positive controls and failed restores are reported as not-shown, never as isolation. Confirmed cleanup is required unconditionally and cannot be waived. Only `inbound_isolation`, `exec_auth` and `no_auto_mount` can be waived in `security.accept_insecure`.

A restriction holding for an ordinary sandbox says nothing about one on the privileged runtime, so the storage, egress, inbound and exec-channel checks run a second time there when `probe.privileged_image` is set. That privileged evidence is what admits a `privileged: true` service; it does not establish arbitrary Linux privileges.

**External targets.** `[probe].external_targets` selects a service for each explicit `host`, `port` and `protocol`. Supply both TCP and UDP controls; you need not operate raw echo servers:

```python
Probe(
    # Keep your other probe inputs, including the pinned image.
    dns_name="pypi.org",
    external_targets=[
        {"host": "pypi.org", "port": 443, "protocol": "tcp", "service": "https"},
        {"host": "time.cloudflare.com", "port": 123, "protocol": "udp", "service": "ntp"},
    ],
)
```

- `service = "echo"` (default): TCP/UDP endpoint returning the exact probe token.
- `service = "https"`: probe sends `HEAD /`, requires a valid final HTTP status and complete headers (≤16 KiB), and verifies the certificate against the original hostname using the image's CA store. A valid error status also proves reachability; no redirects are followed.
- `service = "ntp"`: a version-4 client request whose response must have server mode, version 3/4, synchronized leap status, stratum 1–15, a matching originate timestamp and a nonzero transmit timestamp.

For HTTPS and NTP, probe resolves the hostname once under the allowed policy, pins that one IP for all three phases, and narrows the allowlist to it; HTTPS retains the original SNI/verification name. There is no address fallback or phase retry. These targets apply only to probe instances and never expand workload policy; the probe never guesses targets from `[network].external`. The probe image needs Python's standard-library TLS support and a valid CA store, and never installs packages remotely.

**Peer links and inbound isolation.** The probe creates a two-member group, verifies the directed pair on TCP and UDP, disconnects a member through the real topology API, and checks that reconnecting restores exchange. Peer checks take one TCP and a fixed three UDP samples with distinct tokens in both phases, always completing the set rather than retrying until success. An allowed UDP pair needs at least one matching response with no errors — bounded reachability, **not** lossless delivery. Deny requires every sample blocked; any received data is a failure. For inbound isolation the helper sets source egress and destination ingress independently, so an outbound block is never mistaken for inbound isolation. When the backend cannot express independent controls, inbound evidence is `skipped`; `security.accept_insecure = ["inbound_isolation"]` acknowledges that gap but does not make network-none isolation supported.

**DNS and implicit egress.** Probe uses `[probe].dns_name` to issue actual DNS packets on TCP and UDP, comparing allowed and none policies. `external = "none"` denies ordinary external access while tolerating measured cluster DNS on TCP/UDP 53; it is **not** complete disconnection, and DNS remaining reachable is an expected exception, not a failure. `network_acceptance` requires the external allow/deny/restore controls and a completed DNS measurement. Non-53 controls on resolver IPs are an optional diagnostic (`resolver_non_dns`); missing ones produce `skipped` without blocking external isolation. Known non-DNS implicit egress is still rejected.

`scripts/check_network.py` is a standalone diagnostic and cannot generate acceptance by declaration. Offline unit tests in this repository are not sandbox acceptance; only a probe against the target deployment is.

## Acceptance and maintenance

Use real native tasks covering multi-service networking, separate verifier roles, non-root applications, seeded private state, real tmpfs/readonly rootfs, hidden tests, artifact handoff and failure cleanup. Both `current` and `legacy` must independently pass foreground/background execution, timeout and repeated cancellation, binary transfer, service networking, and cleanup, verified with an independent observer that confirms instances and owned directories disappear. Agent evaluation and GPU training are separate checks, not consequences of an image or probe success.

`release()` and `close()` wait for confirmation; inspect `CleanupError.report`, since TTL is only a fallback. Independently confirm acceptance runs leave no owned instances or launch directories.

```sh
flotilla gc --deployment myproject.deployment:PLATFORM
```

GC is explicit, not a background schedule. Use `gc --launch ID` only after confirming that owner has exited. Releases under `share/releases/` that no deployment pins can be removed through `Deployment.management_anchor()` like any other shared-storage work. Native releases and historical runs are not automatically migrated or deleted; old jobs retain their original code, images and evidence. See [RELEASING.md](../RELEASING.md) for the release gates.
