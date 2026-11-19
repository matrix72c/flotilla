# Configuration reference

The only deployment file is `deployment.toml`. `flotilla probe` writes nothing: it measures isolation and reports it through its output and exit status. Templates are in [`deployments/examples/`](../deployments/examples/). Validation errors show field paths without echoing input values.

## deployment.toml

Durations are in seconds. Relative paths are resolved against the directory containing `deployment.toml`.

### `backend` and `[backends.<name>]`

| Key | Default | Meaning |
|---|---|---|
| `backend` | `"opensandbox"` | Sandbox backend. The built-in `opensandbox` backend reads `[opensandbox]`; any other name must be an installed backend plugin |
| `[backends.<name>]` | | The selected third-party backend's settings, validated by that backend. Every backend table has `endpoint` and `credential_env` with the same meaning as in `[opensandbox]` |

A configuration contains exactly one backend table: `[opensandbox]` for the built-in backend, or `[backends.<name>]` matching `backend`. See [Writing a backend](Writing_a_Backend.md).

### `[opensandbox]` (required for the built-in backend)

| Key | Default | Meaning |
|---|---|---|
| `endpoint` | required | OpenSandbox server root (`http(s)://...`); the backend appends `/v1` |
| `credential_env` | required | Name of the environment variable holding the API credential. Never put the credential itself here |
| `filesystem_protocol` | `"upstream"` | Lifecycle filesystem mapping: `"upstream"` omits extension fields and rejects tasks needing read-only rootfs, tmpfs or `shm_size`; `"sandbox-system-v1"` enables the internal wire format and those capabilities |
| `extensions` | `{}` | Extra `extensions` sent with every create request |
| `privileged_extensions` | `{}` | Extra `extensions` sent only for privileged services, including services declaring `/dev/fuse`, `/dev/kvm` or `/dev/net/tun`. The device keys `fuse`, `kvm` and `tun` are written per instance and cannot be set in any extensions table |
| `management_extensions` | `{}` | Overrides of `extensions` sent only by workspace anchors explicitly selecting `prepare.management_profile = "management"`; never merged with `privileged_extensions` |
| `create_fields` | `{}` | Extra top-level create-request fields. Fields managed by Flotilla (`image`, `entrypoint`, `metadata`, `timeout`, `resourceLimits`, `resourceRequests`, `volumes`, `networkPolicy`, `extensions`, `env`, `readOnlyRootFilesystem`, `tmpfsMounts`) are rejected |
| `group_extension` | unset | Extension key used to place all sandboxes of one environment in one scheduling group. Set only if the deployment implements it |
| `max_connections` | `100` | HTTP connections per pool. There are three pools: control plane, internal commands (health probes, address and hosts setup), and user commands. Requests beyond this wait in Flotilla; a command's timeout starts when it is sent, not while it waits |

`[opensandbox.network]`:

| Key | Default | Meaning |
|---|---|---|
| `link` | `"cidr"` | How services reach each other: `"cidr"` (policies use sandbox IP ranges) or `"sandbox"` (policies use `sandbox:<id>` targets) |
| `ingress` | `"deployment"` | Use deployment-provided inbound isolation, or opt into `"sandbox"` identity-based ingress rules on platforms supporting that extension. Requires `link = "sandbox"`; never sent by default to upstream OpenSandbox |
| `sandbox_cidrs` | `[]` | Sandbox address ranges. Required when `link = "cidr"` |
| `platform_cidrs` | `[]` | Platform infrastructure ranges excluded from `external = "any"`. Required for `external = "any"` with `link = "cidr"` |
| `max_rules` | `4096` | Maximum rules in one network policy |

`[opensandbox.execd]`:

| Key | Default | Meaning |
|---|---|---|
| `protocol` | `"current"` | execd protocol of the deployment: `"current"` or `"legacy"`. There is no automatic fallback |
| `wrapper` | `"/.flotilla/bin/busybox"` | BusyBox binary from the share release used to wrap commands |

See the [OpenSandbox backend](backends/opensandbox.md) for the differences.

Filesystem checks run when the explicitly selected backend protocol can express them. An unsupported dialect is `skipped`; behavior that violates the request is `failed`; unavailable tools or an incomplete test are `error`. There is no protocol or runtime fallback. Configuration selects a path but never grants a capability, including `ingress = "sandbox"`.

### `[storage]` (required)

| Key | Default | Meaning |
|---|---|---|
| `volumes` | `"host"` | `"host"`: a host path backed by the same shared storage on every worker. `"pvc"`: an existing volume every worker can mount read-write |
| `host_path` | `""` | Shared root for `volumes = "host"`: an absolute host path or a platform-defined identifier, passed to the platform verbatim |
| `claim_name` | `""` | Volume claim for `volumes = "pvc"` |
| `root_subpath` | `""` | Subdirectory of the host path or claim used as the shared root; every mount, including the Anchor's, stays inside it |

`[storage]` is the writable work root: launch and attempt state, the shared-tools release (`[share]`), Anchor operations and GC.

### `[inputs.<name>]` (optional)

Same keys as `[storage]`. Each section declares a named read-only root for published task releases or external read-only payloads such as an Agent runtime, for example an owner's storage shared read-only with this deployment. Names match `[a-z][a-z0-9_-]{0,62}`, at most 63 characters; `work` is reserved for `[storage]`. A release selects its root with `input`; other consumers name the root on their mounts. Task instances mount files from an input root read-only; the Anchor mounts every input root read-only at `/flotilla-inputs/<name>` and only reads from it (release verification, copy sources, hidden tests). Nothing is written, cleaned or garbage-collected there, and Flotilla checks at startup that each input root exists.

Publishing tools (`share publish`) always write the work root of the deployment they are given; release and runtime upload is outside Flotilla. Changing these sections invalidates capability evidence.

### `[datasets.<name>]`

Where one published dataset is, on both sides. Loading a deployment opens nothing, so a config may name a dataset that does not exist yet; resolving it checks the root.

| Key | Meaning |
|---|---|
| `local_root` | Local dataset directory, a directory of task directories; relative to deployment.toml |
| `input` | Optional `[inputs.<name>]` holding the dataset; omitted means the work root `[storage]` |
| `platform_root` | Dataset location on its root: a relative storage key, an absolute host root confined beneath an absolute `host_path`/`root_subpath`, or explicit `pvc:<claim>/<root_subpath>/<key>` for PVC. Platform-identifier host paths require relative keys |

The binding name is a configuration label, not a guessed task identity. `launches/` and `share/` are refused: those are Flotilla's own state, not task data. A dataset is read-only to Flotilla, which never writes to one; do not mutate one running jobs are reading. See [Native tasks](Native_Tasks.md).

### `[agent_runtime]` (optional)

Typed, layer-agnostic bindings for a consumer's agent runtime. Flotilla validates the shape, layer names and roots, and mounts each layer read-only; **which layers must exist is the consumer's contract** — Flotilla does not count layers, interpret their names, or learn anything about the agent. Layer names match `[a-z][a-z0-9_-]{0,62}`, at most 63 characters.

| Key | Meaning |
|---|---|
| `input` | Optional `[inputs.<name>]` holding the layers; omitted means the work root `[storage]` |
| `layers.<name>.local_root` | Optional local directory of the layer (resolved against the deployment TOML); Flotilla only records it |
| `layers.<name>.platform_root` | Layer location on its root, same forms as a release's `platform_root` |

Agent identity, runtime mount paths and timeouts belong to the consumer's recipe, not to this section. The former opaque `[voyager]` namespace was removed; loading a deployment that still declares it fails with a migration error. Secrets must not be embedded here or in task definitions.

### `[share]` (optional)

Omitted means the built-in release `busybox-1.36.1-r31`, published from the Anchor image's pinned BusyBox into the work root at probe/runtime start when missing. Set it only for a custom release.

| Key | Meaning |
|---|---|
| `release` | Share release name (letters, digits, `.`, `_`, `-`) |
| `release_sha256` | SHA-256 of the release's `RELEASE.json`, printed by `flotilla share publish` |

### `[network]`

| Key | Default | Meaning |
|---|---|---|
| `external` | `[]` | Outbound access for services on non-internal networks: `[]` (none), a list of IPs, CIDRs, or host names (`*.example.com` allowed), or the string `"any"`. `link = "sandbox"` does not support `"any"` |

### `[platform]`

Platform properties a request cannot discover. Everything else a platform either does or refuses, and a refusal at create is a better answer than a stored report; what cannot work that way is a number a request has to be built around, where a wrong value refuses nothing and misbehaves later.

| Key | Default | Meaning |
|---|---|---|
| `list_visibility_s` | `30` | Worst-case delay before a created instance appears in listings. GC waits this out, so a value below the truth can collect a live launch. Subject to `reaper.grace + list_visibility_s < reaper.gc_window` |
| `max_ttl_s` | unset | Largest TTL the platform grants an instance, if it caps them. A trial's TTL is clamped to it; a value above the truth lets a sandbox expire mid-trial. `reaper.anchor_ttl` must not exceed it |
| `max_label_value_len` | `63` | Longest label value the platform stores; longer values are truncated to fit |
| `link_max_members` | `9` | Largest group the platform links; a task with more services is rejected before any create |
| `implicit_egress` | `[]` | Destinations a sandbox reaches regardless of policy, as `host:port/proto` (for example `10.96.0.10:53/udp,tcp`). An entry outside `cluster_dns` on port 53 forbids offline tasks |
| `cluster_dns` | `[]` | Exact resolver IPs behind the port-53 entries of `implicit_egress`. Only concrete unicast addresses; a CIDR or a name is rejected |

### `[security]`

| Key | Default | Meaning |
|---|---|---|
| `accept_insecure` | `[]` | Isolation properties you accept running without: any of `"inbound_isolation"`, `"exec_auth"`, `"no_auto_mount"`. `flotilla probe` exits nonzero on a gap not named here; it reports a waiver that matches no gap as unused. Confirmed cleanup is not waivable |

### `[reaper]`

| Key | Default | Meaning |
|---|---|---|
| `anchor_image` | required for use | Anchor image by remote digest (`repo@sha256:...`), built with installed `flotilla anchor build` |
| `anchor_ttl` | `600` | Anchor TTL requested per instance. Must not exceed `platform.max_ttl_s` when that is declared |
| `renew_interval` | `30` | Anchor renewal interval |
| `grace` | `90` | After this long without a successful renewal, the launch pauses new creates and directory operations until a renewal succeeds; running trials continue. The launch is lost only 60s before `anchor_ttl` without renewal. Must be at least `2 * renew_interval` and less than `anchor_ttl - 60` |
| `gc_window` | `180` | Minimum age before GC may collect another launch. Must exceed `grace + platform.list_visibility_s` |
| `trial_ttl` | `1800` | Platform TTL of each service sandbox; renewed while the environment is alive. Sandboxes are not renewed during startup, so each is created with at least the open's startup budget plus `renew_before_s` |
| `renew_before_s` | `600` | Renew a service sandbox when this much TTL remains (capped at `trial_ttl / 2`) |
| `renew_margin_s` | `120` | Safety margin for renewal (capped at half of `renew_before_s`) |

### `[runtime]`

| Key | Default | Meaning |
|---|---|---|
| `startup_timeout_s` | `1800` | Default `OpenOptions.startup_timeout`: from admission until the environment is ready. Queueing and client start are bounded separately |
| `run_timeout_s` | `21600` | Upper bound for `OpenOptions.lifetime` |
| `exec_default_timeout_s` | `300` | Command timeout when none is given |
| `cancel_confirm_timeout_s` | `15` | How long to wait for confirmation that an interrupted command stopped |
| `check_timeout_s` | `60` | Timeout for health and ownership checks |
| `exec_output_limit_bytes` | `8388608` | Maximum captured stdout + stderr per command |

### `[limits]`

Set these from measured platform quotas.

| Key | Default | Meaning |
|---|---|---|
| `trial_concurrency` | `512` | Maximum concurrent environments per client |
| `instance_budget` | `4096` | Maximum sandboxes per client, including startup replacement reservations and the anchor |
| `create_rate` | `5.0` | Sandbox create requests per second |
| `create_concurrency` | `64` | Concurrent create requests |
| `reap_concurrency` | `16` | Concurrent deletions |
| `exec_rate` | `500.0` | Command requests per second |
| `exec_concurrency` | `64` | Concurrent commands in task environments |
| `anchor_concurrency` | `16` | Concurrent requests to an anchor's command service (directory operations, verification staging, uploads and downloads). The anchor shares a small sandbox among them; excess requests queue in the client. Size it with `prepare.management_resources` |
| `exec_timeout` | `21600` | Maximum timeout accepted for a single command |
| `admission_timeout` | `3600` | Default `OpenOptions.admission_timeout`: how long an open waits for a `trial_concurrency` / `instance_budget` slot. On expiry it raises `AdmissionTimeout` (a `TimeoutError`) with the current counts; nothing has been created |

### `[timeouts]`

Per-stage startup deadlines. The whole startup, from admission to ready, is also bounded by `OpenOptions.startup_timeout`.

| Key | Default | Stage |
|---|---|---|
| `prepare` | `600` | Preparing trial directories on shared storage |
| `create` | `600` | Creating all sandboxes |
| `create_request` | `60` | One create request. When no response arrives in time (for example, the gateway drops it under load), the sandbox is looked up by its labels and adopted. Must be below `create` |
| `pending` | `300` | One sandbox staying pending. It is deleted and created again once; if the replacement also stays pending, the open fails. An explicit numeric value must be below `create`; the default is off when `create` is not above it. Disabled pending replacement is serialized as JSON `null`, preserving the effective behavior when a workspace compiler reloads the full configuration snapshot |
| `address` | `30` | Resolving sandbox addresses |
| `wire` | `60` | Applying peer network links |
| `hosts` | `30` | Writing `/etc/hosts` entries |
| `start_max` | `1800` | Upper bound for starting services and waiting for health gates |
| `close` | `60` | Default timeout for `fleet.drain()` and `fleet.close()` |
| `client_start` | `300` | Starting the client's anchor and checking the pinned share release, once per client before its first open. The check downloads `RELEASE.json`, compares its sha256 and confirms `bin/busybox`; `flotilla share verify` checks every file |

`[process].poll_interval` (default `2`) sets how often background process status is polled.

### `[resources]`

```toml
[resources.default]           # applies to every service without an explicit setting
cpu = "1"
memory = "2Gi"

[resources.services.main]     # by service name, for every task
cpu = "2"
memory = "4Gi"

[resources.tasks.my-task.services.db]   # one service of one task
cpu = "4"
memory = "8Gi"
```

Precedence, lowest first: `[resources.default]`, resources declared in the Compose file, `[resources.services.*]`, `[resources.tasks.*]`, and finally `OpenOptions.resources` at runtime.

### `[prepare]`

The attempt's own management sandbox: the launch anchor that compiles attempt-owned storage and reads task metadata, which every client creates.

| Key | Default | Meaning |
|---|---|---|
| `concurrency` | `4` | Tasks a launch anchor works on concurrently |
| `workspace_command` | `["python3", "-I", "-m", "flotilla.integrations.workspace"]` | Helper in the trusted management image; the matching Flotilla install is required and never installed automatically |
| `workspace_timeout` | `1800` | Total remote budget for that helper |
| `management_profile` | `"default"` | `"management"` selects the trusted backend-specific management overrides for workspace anchors only; OpenSandbox requires nonempty `management_extensions` |
| `management_resources` | unset (`0.5` CPU / `512Mi`) | Optional `{cpu, memory}` allocation for workspace anchors only |
| `management_network` | external none | Explicit management-only destinations; no automatic unrestricted egress |
| `management_mounts` | `[]` | Explicit read-only shared-volume mounts for credential/config files, never baked into images or passed to task sandboxes |
| `management_env` | `{}` | Nonsecret connection settings; credential contents belong in mounted files |

The launch anchor that reads task metadata can be sized and placed separately from the ordinary task runtime:

```toml
[prepare]
management_profile = "management"
management_resources = { cpu = "2", memory = "4Gi" }

[opensandbox.management_extensions]
nodeSelector = { "example.com/pool" = "metadata" }
```

These are operator-owned deployment settings, not Compose fields or task runtime parameters. The internal profile is closed (`default` or `management`); selecting it does not grant task privileged permission, and combining management with `privileged=True` is rejected. Ordinary task, probe, bootstrap and GC requests retain their existing extensions and resource allocations. Management extensions may override global deployment extensions but cannot set Flotilla-managed placement-group or shared-memory keys.

The management image still comes from `reaper.anchor_image`, and the anchor retains its explicit entrypoint. Metadata and resource inspection only read the task's Compose files in-process; they run no tool and need no Docker daemon in the anchor.

Management-only extensions/profile/resources are excluded from the task capability fingerprint because probes do not use them. Record separate management diagnostics against the exact management image digest, extensions and resources when changing them.

### `runtime_params` and `[agent_overlay]`

| Key | Meaning |
|---|---|
| `runtime_params` | Deployment-wide interpolation values for task Compose files, applied when a task is prepared or inspected; a per-call value for the same name wins |
| `agent_overlay.env` | Environment variables added to the agent service. `${VAR}` refers to the service's original value, e.g. `PATH = "/opt/tools/bin:${PATH}"` |
| `agent_overlay.services` | Services that receive the overlay instead of only the main service |

### `[probe]`

All probe inputs live in the deployment file. Images must be pinned `repo@sha256:<64 lowercase hex>` references; empty optional images skip the corresponding phase.

| Key | Default | Meaning |
|---|---|---|
| `deployment` | `"deployment"` | Human-readable name for this deployment in the probe's output |
| `image` | `""` | Required ordinary probe image |
| `privileged_image` | `""` | Image for the configured privileged create path. Enables the privileged phase, which measures the same storage, egress, inbound and exec-channel restrictions again inside the privileged runtime |
| `link_members` | `9` | Group size the probe builds for its network checks, bounded to 2–32; `platform.link_max_members` is the deployment's own limit |
| `hosts_recheck_s` | `600` | Delay before verifying that `/etc/hosts` was not rewritten |
| `total_timeout_s` | `3600` | Total check budget; cleanup has its own budget |
| `cleanup_timeout_s` | `180` | Bounded reconciliation and deletion confirmation |
| `external_targets` | `[]` | Explicit `{host, port, protocol, service}` controls: `service = "echo"` (default, TCP/UDP), `"https"` (TCP), or `"ntp"` (UDP); `protocol` defaults to `"tcp"`; never inferred from workload egress |
| `dns_name` | `""` | DNS query name for explicit TCP/UDP resolver checks |

For external isolation, provide TCP and UDP service controls (for example HTTPS and NTP, without operating echo servers) and a DNS query name. HTTPS/NTP resolve once under an allowing policy and use the same pinned IP for allow, deny and allow again; HTTPS retains certificate hostname verification and SNI. Missing positives, invalid responses, certificate errors and restore failures never prove isolation. The `platform.cluster_dns` resolvers are allowed on TCP/UDP 53; that is not complete network disconnection. See [Network validation](Network_Validation.md). Probe targets do not extend `[network].external` for tasks.

The probe image needs the configured execd interface, shell and file utilities, and Python 3's standard socket/TLS libraries with a CA trust store for HTTPS.

## Isolation evidence

`flotilla probe` prints one line per isolation property with what was observed, and exits nonzero if any of them did not hold without being named in `security.accept_insecure`, or if cleanup was not confirmed. It writes no file: the observations exist only for the length of the run, and a client in another process cannot re-derive them, so running the probe and reading its exit status is the gate. See [Platform requirements](Platform_Requirements.md).
