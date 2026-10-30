# Configuration reference

A deployment is described by two operator-owned files: `deployment.toml`, read by every Flotilla client, and `declared.json`, which `flotilla probe` combines with its own measurements into `capabilities.json`. Templates are in [`deployments/examples/`](../deployments/examples/). Unknown keys are rejected in both files, and validation errors show field paths without echoing values.

## deployment.toml

Durations are in seconds. Relative paths are resolved against the directory containing `deployment.toml`.

### `backend` and `[backends.<name>]`

| Key | Default | Meaning |
|---|---|---|
| `backend` | `"opensandbox"` | Sandbox backend. The built-in `opensandbox` backend reads `[opensandbox]`; any other name must be an installed backend plugin |
| `[backends.<name>]` | | The selected third-party backend's settings, validated by that backend. Every backend table has `endpoint`, `credential_env`, and `capabilities` with the same meaning as in `[opensandbox]` |

A configuration contains exactly one backend table: `[opensandbox]` for the built-in backend, or `[backends.<name>]` matching `backend`. See [Writing a backend](Writing_a_Backend.md).

### `[opensandbox]` (required for the built-in backend)

| Key | Default | Meaning |
|---|---|---|
| `endpoint` | required | OpenSandbox server root (`http(s)://...`); the backend appends `/v1` |
| `credential_env` | required | Name of the environment variable holding the API credential. Never put the credential itself here |
| `capabilities` | required | Path to the capability report written by `flotilla probe` |
| `filesystem_protocol` | `"upstream"` | Lifecycle filesystem mapping: `"upstream"` omits extension fields; `"sandbox-system-v1"` enables the internal wire format, subject to matching probe evidence |
| `extensions` | `{}` | Extra `extensions` sent with every create request |
| `privileged_extensions` | `{}` | Extra `extensions` sent only for `privileged: true` services |
| `create_fields` | `{}` | Extra top-level create-request fields. Fields managed by Flotilla (`image`, `entrypoint`, `metadata`, `timeout`, `resourceLimits`, `resourceRequests`, `volumes`, `networkPolicy`, `extensions`, `env`, `readOnlyRootFilesystem`, `tmpfsMounts`) are rejected |
| `group_extension` | unset | Extension key used to place all sandboxes of one environment in one scheduling group. Set only if the deployment implements it |

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

The optional `probe_readonly_rootfs`, `probe_native_tmpfs`, and `probe_shm_size` booleans in `declared.json` request behavioral measurements and default to false. They do not assert support. New filesystem capabilities require matching `probed` evidence and a runtime configuration fingerprint. See [filesystem configuration and migration](Readonly_Rootfs_and_Tmpfs.md).

### `[storage]` (required)

| Key | Default | Meaning |
|---|---|---|
| `volumes` | `"host"` | `"host"`: a host path backed by the same shared storage on every worker. `"pvc"`: an existing volume every worker can mount read-write |
| `host_path` | `""` | Shared root for `volumes = "host"` |
| `claim_name` | `""` | Volume claim for `volumes = "pvc"` |
| `root_subpath` | `""` | Subdirectory of the claim used as the shared root |

### `[share]` (required)

| Key | Meaning |
|---|---|
| `release` | Share release name (letters, digits, `.`, `_`, `-`) |
| `release_sha256` | SHA-256 of the release's `RELEASE.json`, printed by `flotilla share publish` |

### `[network]`

| Key | Default | Meaning |
|---|---|---|
| `external` | `[]` | Outbound access for services on non-internal networks: `[]` (none), a list of IPs, CIDRs, or host names (`*.example.com` allowed), or the string `"any"`. `link = "sandbox"` does not support `"any"` |

### `[security]`

| Key | Default | Meaning |
|---|---|---|
| `accept_insecure` | `[]` | Security gaps you explicitly accept: any of `"inbound_isolation"`, `"exec_auth"`, `"no_auto_mount"`. Startup refuses if the capability report shows an unaccepted gap. Waivers that no longer match a gap produce a warning |

### `[reaper]`

| Key | Default | Meaning |
|---|---|---|
| `anchor_image` | required for use | Anchor image by digest (`repo@sha256:...`), built with `images/anchor/build.sh` |
| `anchor_ttl` | `600` | Anchor sandbox TTL; must not exceed the deployment's maximum TTL |
| `renew_interval` | `30` | Anchor renewal interval |
| `grace` | `90` | After this long without a successful renewal, the launch stops admitting work. Must be at least `2 * renew_interval` and less than `anchor_ttl` |
| `gc_window` | `180` | Minimum age before GC may collect another launch. Must exceed `grace + list_visibility_s` |
| `trial_ttl` | `1800` | Platform TTL of each service sandbox; renewed while the environment is alive |
| `renew_before_s` | `600` | Renew a service sandbox when this much TTL remains (capped at `trial_ttl / 2`) |
| `renew_margin_s` | `120` | Safety margin for renewal (capped at half of `renew_before_s`) |

### `[runtime]`

| Key | Default | Meaning |
|---|---|---|
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
| `exec_concurrency` | `64` | Concurrent commands |
| `exec_timeout` | `21600` | Maximum timeout accepted for a single command |

### `[timeouts]`

Per-stage startup deadlines. The whole startup is also bounded by `OpenOptions.startup_timeout`.

| Key | Default | Stage |
|---|---|---|
| `prepare` | `600` | Preparing trial directories on shared storage |
| `create` | `600` | Creating all sandboxes |
| `address` | `30` | Resolving sandbox addresses |
| `wire` | `60` | Applying peer network links |
| `hosts` | `30` | Writing `/etc/hosts` entries |
| `start_max` | `1800` | Upper bound for starting services and waiting for health gates |
| `close` | `60` | Default timeout for `fleet.drain()` and `fleet.close()` |

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

### `[preparation]` and `[build]`

| Key | Default | Meaning |
|---|---|---|
| `preparation.concurrency` | `4` | Concurrent task preparations per client |
| `preparation.timeout_s` | `3600` | Cap on a single preparation |
| `preparation.docker_command` | `["docker"]` | Docker argv prefix, e.g. `["sudo", "-n", "docker"]` |
| `build.target` | `""` | Repository that built and mirrored images are pushed to, e.g. `registry.example.com/team/tasks` |
| `build.pullable_registries` | `[]` | Registries or prefixes the platform can pull from directly; other images are mirrored to `build.target` |
| `build.mirror_images` | `false` | Mirror every `image:` service, even from pullable registries |
| `build.image_replacements` | `{}` | Map of image references to substitutes, e.g. to use an internal mirror |

### `runtime_params` and `[agent_overlay]`

| Key | Meaning |
|---|---|
| `runtime_params` | Deployment-wide values for a task's runtime parameters; `OpenOptions.runtime_params` takes precedence |
| `agent_overlay.env` | Environment variables added to the agent service. `${VAR}` refers to the service's original value, e.g. `PATH = "/opt/tools/bin:${PATH}"` |
| `agent_overlay.services` | Services that receive the overlay instead of only the main service |

## declared.json

`flotilla probe` measures what it can and takes everything else from this file. A declared item is an operator assertion, not a test result: set `ok` to `true` only with real evidence and describe it in `evidence`.

| Field | Meaning |
|---|---|
| `deployment` | Human-readable deployment name |
| `max_ttl_seconds` | Maximum sandbox TTL the platform allows, or `null` |
| `list_visibility_s` | Upper bound for a new sandbox to appear in label listings. The probe checks it |
| `link_max_members` | Maximum number of sandboxes in one peer-linked group |
| `external_forms` | Supported outbound policy forms: `"none"`, `"allowlist"`, `"any"` |
| `implicit_egress` | Destinations the platform allows regardless of policy, as `target:port/protocol` strings |
| `cluster_dns` | Optional concrete resolver IPs (default `[]`). Offline policies tolerate only matching `IP:53/udp`, `IP:53/tcp`, or comma-separated `tcp,udp` entries in `implicit_egress`; bracket IPv6 addresses, e.g. `[fd00::a]:53/udp`. Keep prose in evidence, not in these entries. No subnet, hostname, wildcard, or port-wide exception is accepted |
| `devices` | Devices available to privileged services: `"fuse"`, `"kvm"` |
| `notes` | Free-form notes copied into the report |

Each of the following is an object `{"ok": bool, "source": "declared", "evidence": "..."}`:

| Item | Kind | Asserts |
|---|---|---|
| `link` | Feature | Sandboxes can be linked so that services reach each other |
| `link_udp` | Feature | Peer links carry UDP |
| `inbound_isolation` | **Security** | A sandbox cannot be reached by sandboxes outside its group |
| `implicit_egress_declared` | **Required** | `implicit_egress` lists every implicitly allowed destination |
| `shared_volume` | Feature | Writable volumes can be shared between services |
| `missing_subpath` | Informational | Platform behavior for a volume subpath that does not exist yet; recorded only |
| `no_auto_mount` | **Security** | The platform mounts nothing beyond the requested volumes |
| `volume_file` | Feature | Single files can be mounted |
| `exec_auth` | **Security** | execd rejects requests without the sandbox's access token |
| `diagnostics` | Informational | Platform diagnostics are available for failed sandboxes; recorded only |
| `privileged_runtime` | Feature | `privileged: true` services are supported |
| `multi_container` | Informational | Recorded only; not used |

Missing **required** items, measured or declared, block startup. **Security** gaps block startup unless accepted in `security.accept_insecure`. Feature items gate tasks that need them; for example, a multi-service task needs `link`. See [Platform requirements](Platform_Requirements.md) for what the probe measures and how to gather evidence.
