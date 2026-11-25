# OpenSandbox backend

Set `opensandbox.endpoint` to the server root; the backend appends `/v1`. Credentials are read from `opensandbox.credential_env`. Each runtime owns its HTTP clients and closes them explicitly. Error messages retain operation stage, HTTP status, and constrained error codes/request IDs, excluding raw response bodies.

Authentication and other non-retryable failures stop readiness/reconciliation polling. A rejected cleanup request stays unconfirmed and never counts as a successful deletion. A definitively rejected create needs no reconciliation; a lost create response or a deadline during creation still does.

## execd protocols

| Setting | `current` | `legacy` |
|---|---|---|
| Command representation | argv and numeric identity fields | Quoted command string; wrapper applies identity/environment |
| File routes | `/v1/filesystem/{uid}/{gid}/...` | `/files/...` |
| Upload ownership | Route selects writing identity | Root writes, then chown and chmod restore requested metadata |
| Literal upload paths | Identity route contract | Absolute path without `$`; remote expansion is rejected |
| execd version | 1.1.1 or later (argv requests and identity file routes) | Older execd releases |

Choose the deployment's actual protocol in `[opensandbox.execd]`; there is no speculative retry using a different protocol. Both paths use the configured BusyBox wrapper (default `/.flotilla/bin/busybox`). Task images must provide the shell/tools their own commands need; Flotilla does not inject a missing `/bin/sh` into arbitrary images.

The exec daemon runs as root so it can initialize the sandbox and apply each command's numeric identity, so every task image must declare a root `USER`. Preparation rejects one that does not, rather than deriving a `USER 0:0` image from it: publish the image with `USER 0:0` and declare the application identity with Compose `user`. The deployment must permit the daemon to run as root.

Commands are streamed and tracked using the `init` command ID. A client deadline triggers interruption and bounded status confirmation; a remote fallback deadline protects against a lost client without classifying ordinary signal exits as timeouts. Non-idempotent requests are never replayed. See [Runtime API](../Runtime_API.md).

An environment health check retries a confirmed timeout of its internal `true` probe within `runtime.check_timeout_s`, as it does transient transport failures. A nonzero probe result, unknown execution outcome, or exhaustion of the overall check budget still fails closed.

For the command stream only, HTTP 409 with error code `CONFLICT` (flat or inside `detail`) is the deployment's explicit busy rejection, raised as `CommandBusy` (transient, retryable). It is not converted to `ExecOutcomeUnknown`, and neither foreground nor background commands are automatically replayed. Healthchecks skip the round without consuming a failure and probe again on their next interval. Bare 409s, other error codes, and conflicts on other routes keep their existing classification.

To investigate command conflicts, enable DEBUG on the Python logger `flotilla.platform.opensandbox.http`. Each stream logs send, response headers, `init` when observed, and request end with a shared local `trace_id`, plus the sandbox proxy path, stage, background flag, monotonic timestamps, HTTP status, constrained platform IDs, and total duration. Compare send/end intervals for the same sandbox within one client process to find overlapping requests; timestamps from different processes are not a shared timeline. Payloads, environment values, output, credentials, and raw error bodies are excluded.

## Storage and network

Storage uses deployment-approved host paths or an existing shared PVC, with confined relative subpaths. `host_path` is sent verbatim as `host.path`, so a platform storage identifier (for example a user-scoped `@user` or a shared `@user:<owner>` directory) works without Flotilla interpreting it; `root_subpath` prefixes every `subPath`. Each volume compiles against its own root: a volume on a named input root uses `[inputs.<name>]`, everything else `[storage]`; an unconfigured name is an error. The anchor mounts the work root read-write and each input root read-only; task instances receive only their specified mounts. The anchor image includes BusyBox `stat` for numeric metadata, including setuid/setgid/sticky bits.

`opensandbox.network.link="cidr"` requires accurate sandbox CIDRs. `link="sandbox"` uses explicit `sandbox:<id>` peer targets and supports none/allowlist external policies. The optional `group_extension` is used only when the deployment implements it; it groups a trial rather than pinning a worker node. Support for sandbox-ID policies does not establish support for raw peer-IP/CIDR policies; record the mode and any group extension with the probe run that measured it.

Create uses the common top-level `networkPolicy` field; replacements use `PUT /v1/sandboxes/{id}/networkpolicy` on upstream OpenSandbox and platforms implementing its compatibility route. Flotilla does not fall back to a provider-specific route or the sidecar API. Upstream's policy schema contains `defaultAction` and `egress`; provider extensions are explicitly selected.

On an internal platform supporting sandbox ingress, set both `opensandbox.network.link="sandbox"` and `opensandbox.network.ingress="sandbox"`. Every instance starts with `ingress={"defaultAction":"allow","rules":[{"action":"deny","from":"sandbox:any","protocol":"ANY"}]}`. Linking replaces both egress and ingress, adding only that service's exact peers to the ingress rule's `except` list. Services with Compose `network_mode: none` stay unlinked and keep the initial deny rule. With the default `ingress="deployment"`, no ingress extension is sent; Compose `none` requires the recorded inbound isolation capability, even when the deployment otherwise accepts an inbound-isolation waiver.

No-network policies use `defaultAction="deny"` with no external allow rules. The only tolerated implicit egress is TCP/UDP 53 to the explicit `[platform].cluster_dns` IPs; an `[platform].implicit_egress` entry outside them forbids offline tasks. `external="any"` does not make sandbox subnets reachable: unlinked sandbox and platform ranges remain excluded, so a failed sandbox-to-sandbox request cannot establish inbound isolation.

## Read-only root filesystems, tmpfs and shared memory

Lifecycle filesystem extensions are selected with `opensandbox.filesystem_protocol`, independent of `[opensandbox.execd].protocol`. The default `"upstream"` sends no extension fields and rejects any task requiring these capabilities. `"sandbox-system-v1"` maps Compose `read_only: true`, `tmpfs` and `shm_size` to the internal lifecycle API, checks effective configuration before startup, and treats unsupported requests as non-retryable. These are three independent capabilities; a task requiring any of them is rejected at planning unless the protocol can express it. Flotilla never retries a create after removing filesystem restrictions, and never substitutes a regular directory for tmpfs.

Nothing measures these capabilities first, and `flotilla probe` does not check them: a platform that cannot honor the fields refuses the create, and the probe measures only isolation. Selecting the protocol is not evidence a server implements it, so before relying on it, open a task declaring `read_only` and `tmpfs` on the actual deployment and observe from inside the sandbox — writes to `/` fail with EROFS, each tmpfs mount has type `tmpfs` with the declared size, mode and owner, and declared writable volumes stay writable.

```yaml
services:
  main:
    image: registry.example/app:version
    user: "1000:1000"
    read_only: true
    mem_limit: 512m
    tmpfs:
      - /tmp:size=64m,mode=1777
      - /cache:size=32m,mode=0750,uid=1000,gid=1000
    volumes:
      - results:/results
volumes:
  results: {}
```

Service `read_only` protects the root filesystem, including against ordinary root writes; a volume's `read_only` protects only that mount. Programs writing to undeclared image paths fail — Flotilla does not automatically open `/tmp`.

**tmpfs.** Short syntax accepts an absolute directory, optionally followed by `size`, `mode`, `uid`, and `gid`. Long volume syntax accepts `type: tmpfs`, `target`, and `tmpfs: {size, mode}`; use short syntax for UID/GID. Unsupported options (`noexec`, `nosuid`, read-only tmpfs) are rejected rather than dropped. Capacity converts from Compose byte quantities to a whole byte count (`64m` → 67108864). A mount without `size` gets **half the service's requested memory, at least 64 MiB**, after deployment resource defaults are resolved; admission counts it at that size. The dialect enforces: at most eight mounts; total declared tmpfs (plus any `shm_size`) no greater than requested sandbox memory; canonical absolute paths with no duplicate, parent/child or volume overlap; none at or overlapping `/run`, `/var/run`, `/opt/opensandbox`, `/dev`, `/proc`, `/sys`, `/sandbox-data`, `/data`, `/var/lib/docker`, or Flotilla's `/.flotilla` and `/.flotilla-runtime`; and no combination with `privileged: true`. tmpfs consumption counts against sandbox memory (a process can OOM before filling the mount), lasts the sandbox lifetime, and is never copied from the image or shared between instances.

**Shared memory.** Compose `shm_size` sets the service's `/dev/shm` capacity (`2gb` → 2147483648). A service without it keeps the platform default: `min(8Gi, requested memory)` on the internal platform, the runtime's default (usually 64 MiB) on upstream. An explicit size is sent per instance as `extensions["sandbox-system.io/shm-size"]` in bytes and counts against the same requested-memory budget as tmpfs. `opensandbox.extensions` and `privileged_extensions` may not set this key; privileged services with `shm_size` are rejected, as with tmpfs. Before business startup, an instance that requested `shm_size` must echo exactly that size (after normalization) or readiness fails like a tmpfs mismatch.

**Storage for read-only services.** Single-service writable named volumes and binds become real environment-owned storage mounts, not writable image directories; a named volume starts empty either way. A read-only service also receives a framework mount at `/.flotilla-runtime`, backed by shared storage with a private per-attempt subdirectory, owned by the service UID/GID, holding service logs and transfer archives, and removed after the environment's instances are deleted. It does not consume a tmpfs slot or memory budget. The `/.flotilla` tools mount stays read-only. A target that is a symlink in the image is resolved inside the container's root filesystem by the runtime, as Docker does; a target that is a regular file fails at sandbox creation.

**Wire contract.** Each create sends explicit `readOnlyRootFilesystem` and `tmpfsMounts` (including false/empty for auxiliary instances); these are managed fields and cannot be overridden through `create_fields`. Before business startup, a Running instance's effective configuration must match the request after normalization — missing fields, ignored restrictions, or unexpected inherited mounts fail readiness, with the instance registered for cleanup first. HTTP 400 and 501 are non-retryable configuration/capability failures: no filesystem fields are removed and the task cannot proceed with a writable-root approximation. MVM, sysbox, and kata are outside the internal feature contract; probe the exact runtime settings.

## Compatibility verification

Select each protocol explicitly and run the opt-in real contracts; record server/execd image versions and protocol configuration privately with the results. A local mock contract is not evidence for a real gateway's stream, interrupt, upload, or policy behavior.

```sh
python -m tests.contract.foreground --deployment myproject.deployment:PLATFORM \
  --image "$CONTRACT_IMAGE" --out artifacts/foreground-contract.json
```

The default contract requires three mutually reachable services. For a deployment that cannot link peers, use `--services 1` to validate execution, cancellation, timeout, binary transfer and cleanup independently; the result records the skipped peer checks explicitly, and passing this subset does not satisfy multi-service acceptance. For a bounded concurrency and renewal check, add `--soak-seconds 120` with a private test config (`reaper.trial_ttl=120`, `renew_before_s=60`, `renew_margin_s=20`) so service renewal occurs within the window. It is a short acceptance check, not a long-duration stress test.
