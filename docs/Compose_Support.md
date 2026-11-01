# Compose support

Flotilla reads a Compose project and maps each service to its own sandbox. Every key is classified as **supported**, **equivalent** (accepted, but has no effect or a documented approximation), **warning**, or **rejected**. A single rejection rejects the task before any image is built or any sandbox is created. Run `flotilla inspect` to see the findings for a task, each with its field path.

## Task layout

```text
task/
  task.toml
  environment/
    Dockerfile             # optional
    docker-compose.yaml    # optional; the only Compose file name that is read
    .env                   # optional; with CONTEXT_DIR, the only source of interpolation variables
```

- The main (agent) service is always named `main`. A task without a `main` service is rejected.
- Without a Compose file, the task has a single `main` service built from `environment/Dockerfile`.
- With a Compose file, `main` gets `command: ["sh", "-c", "sleep infinity"]` and, if `environment/Dockerfile` exists, `build: .` unless the Compose file overrides them. The task's Compose file is merged on top, as with `docker compose -f base -f task`.
- Service names must match `[A-Za-z0-9][A-Za-z0-9_.-]*`.

### `task.toml`

| Key | Effect |
|---|---|
| `[task].name` | Task name (defaults to the directory name); used for `[resources.tasks.*]` |
| `[environment].docker_image` | Image for `main`; replaces any `build` |
| `[environment].workdir` | `working_dir` of `main` |
| `[environment].cpus` | CPU of `main` |
| `[environment].memory` / `memory_mb` | Memory of `main` (set only one) |
| `[environment].network_mode` | `"public"` or `"no-network"`; the latter removes external access from all services while preserving their Compose networks |
| `[environment].allow_internet` | `false` selects `no-network`, `true` selects `public`. Explicit `network_mode` takes precedence; otherwise ignored when `allowed_hosts` is present |

`OpenOptions(disable_internet=True)` and Harbor's `no-network` policy also remove external access. A task-level restriction survives preparation and cannot be relaxed by caller or deployment defaults. Cluster DNS may remain reachable as described below.

## Top-level keys

| Key | Status |
|---|---|
| `services` | Supported; must be a non-empty mapping |
| `name`, `version` | Equivalent (ignored) |
| `networks` | Supported with restrictions, see below |
| `volumes` | Named volumes without options only (`data: {}` or `data:`) |
| `secrets`, `configs`, `include`, `x-*`, others | Rejected |

Top-level networks support `internal: true`. `driver` must be `bridge`, `ipam` must be empty or `{driver: default}` (the platform assigns addresses), and only the bridge name, MTU, and `gateway_mode_ipv4/ipv6` driver options are accepted. `external` networks and volumes are rejected because trials never share resources. Unused networks are dropped; a `default` network is created implicitly.

## Service keys

| Key | Status |
|---|---|
| `image` | Supported |
| `build` | `context`, `dockerfile`, and `args` only; paths must stay inside the task directory. `target`, `ssh`, `cache_from`, `secrets`, `platforms`, and others are rejected |
| `command`, `entrypoint` | Supported (string form is split like a shell word list, without a shell). Docker semantics: `entrypoint` drops the image `CMD` |
| `environment` | Supported, including names Docker accepts that are not shell identifiers (`discovery.type`). A key without a value (inherit from host) is rejected: there is no host environment |
| `env_file` | A path or list of paths inside `environment/`, strict `KEY=VALUE` lines. The long form (`path`/`required`) and escaped values are rejected. `environment` entries win |
| `working_dir` | Supported |
| `user` | Supported; resolved to numeric uid/gid from the image |
| `depends_on` | List or map. Conditions `service_started`, `service_healthy`, `service_completed_successfully`, and `required` are supported |
| `healthcheck` | `test`, `interval`, `timeout`, `retries`, `start_period`, `start_interval`, `disable`. `test` must be given (inheriting the image's test is rejected); `["NONE"]` or `disable: true` turns the image's healthcheck off |
| `volumes` | See [Volumes and bind mounts](#volumes-and-bind-mounts) |
| `tmpfs`, `type: tmpfs` | Native memory mounts; requires verified `native_tmpfs`. Short syntax supports size/mode/uid/gid; long syntax supports size/mode. No directory emulation |
| `shm_size` | `/dev/shm` capacity; requires verified `shm_size` (internal filesystem dialect only, rejected on upstream OpenSandbox). Counts against the same memory budget as tmpfs. Without it the platform default applies. See [shared memory](Readonly_Rootfs_and_Tmpfs.md#shared-memory-shm_size) |
| `networks` | List or map with `aliases`. Static `ipv4_address`/`ipv6_address` are rejected |
| `network_mode` | Only `none`. `host`, `bridge`, `service:*`, and `container:*` are rejected |
| `ports`, `expose` | Equivalent: nothing is published, and services on a shared network reach every port. Port ranges and protocols other than TCP/UDP are rejected; UDP requires the deployment's `link_udp` capability |
| `restart` | `no` is supported. `always` and `unless-stopped` are equivalent: services are not restarted, and the run is marked void if a service exits in a way Docker would have restarted. `on-failure` is rejected |
| `read_only` | Service root filesystem protection; `true` requires verified `readonly_rootfs`. Independent of volume read-only flags |
| `privileged` | Requires the deployment's `privileged_runtime` capability (warning when supported) |
| `hostname`, `container_name` | Added as names other services can resolve. `hostname` is a warning: `gethostname()` inside the sandbox still returns the platform's name |
| `cpus`, `mem_limit`, `deploy.resources.limits.{cpus,memory}` | Supported; if both forms are given they must agree. Memory units are binary (`g` = GiB) |
| `init`, `labels`, `logging`, `stop_signal`, `stop_grace_period`, `pull_policy` | Equivalent (recorded only) |
| `ulimits`, `tty`, `stdin_open`, `group_add`, `cap_drop`, `security_opt`, `pid`, `ipc`, `userns_mode`, `runtime` | Rejected: not available in a sandbox |
| `cap_add`, `devices`, `sysctls`, `extra_hosts`, `domainname`, `links`, `profiles`, `secrets`, `configs`, `extends`, `volumes_from`, `scale` | Rejected: not implemented |
| Any other key, including `deploy` keys other than resource limits and `x-*` | Rejected |

## Volumes and bind mounts

Short syntax `source:target[:ro|rw]` is a bind mount when the source starts with `.`, `/`, or `~`, and a named volume otherwise. Long syntax supports `type` (`volume`, `bind`, `tmpfs`), `source`, `target`, `read_only`, `volume.nocopy`, and `bind.create_host_path`. Anonymous volumes are rejected, the target must be absolute (and not `/` or under `/.flotilla`), and bind sources must be relative paths inside `environment/`.

Flotilla decides how to provide each volume from how it is used:

| Usage | Provided as |
|---|---|
| Only read-only references | Published with the task files and mounted read-only |
| One writable reference | Copied into that service's image at build time |
| Several references, at least one writable | A per-environment shared directory, seeded from the image or the bind source. Requires the deployment's `shared_volume` capability |

A missing bind source is rejected: the platform would mount an empty directory over the target, so a task whose files were never committed would run with empty tools and grade as unsolvable. `/tmp` must stay writable (framework file transfers and the platform exec daemon write there), so a read-only mount on it is rejected. Mounting a single file that is not copied into an image requires the `volume_file` capability.

For read-only root filesystems, even single-service writable volumes/binds use real environment-owned mounts. Framework scratch space is mounted explicitly at `/.flotilla-runtime`; this path and `/.flotilla` are reserved. See [read-only rootfs and tmpfs](Readonly_Rootfs_and_Tmpfs.md) for deployment selection, supported syntax, limits, defaults and migration from directory emulation.

## Networking

Services that share a Compose network can reach each other by service name, aliases, `hostname`, and `container_name`. Services on at least one non-`internal` network get the outbound policy from the deployment's `[network].external`; services only on internal networks, or with `network_mode: none`, get no outbound access. `OpenOptions.disable_internet=True` removes outbound access for every service. Multi-service tasks need the deployment's `link` capability and must stay within `link_max_members`.

Offline policies tolerate only explicitly declared cluster DNS resolvers on TCP/UDP port 53. The capability report must list their concrete IPs in `network.cluster_dns` and retain every exception in `network.implicit_egress`. Other implicit destinations, ports, and unrecognized declarations reject offline services during inspection and at runtime. This applies to task-level `no-network`, internal networks, Compose `none`, and the runtime option.

Compose `network_mode: none` also excludes the service from peer links and requires inbound isolation: either the deployment's recorded capability or explicit sandbox ingress enforcement. It isolates service traffic while preserving declared cluster DNS and platform management access; it does not remove the sandbox's network interface. See [OpenSandbox network configuration](backends/opensandbox.md).

## Variable interpolation

Only values under `services` are interpolated, and variables come only from `environment/.env`; the host environment is never read. As in Harbor, `CONTEXT_DIR` is always defined as the environment directory (`.`) and takes precedence over `.env`.

| Syntax | Status |
|---|---|
| `${VAR}`, `${VAR:-default}`, `${VAR-default}`, `${VAR:+alt}`, `${VAR+alt}`, `$$` | Supported; defaults and alternatives may contain nested references, e.g. `${NO_PROXY:+${NO_PROXY},}` |
| `$VAR`, `${VAR:?err}`, `${VAR?err}` | Rejected |

A variable referenced in a service's `environment` (or `env_file`) that `.env` does not define becomes a **runtime parameter**. Its value is supplied when the environment is opened, from `OpenOptions.runtime_params` or the deployment's `runtime_params` table (the request wins), or from the Compose default. Opening fails if a parameter has neither a value nor a default. An undefined variable without a default elsewhere in a service rejects the task.
