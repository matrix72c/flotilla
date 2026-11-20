# Compose support

Flotilla reads a task's Compose project in-process and maps each service to its own sandbox. It does **not** run Docker Compose containers and promises no full Docker, PID 1, signal-delivery, process-tree, or container-inspection equivalence: it supports a subset of Compose and maintains that subset's agreement with Docker. There is no Compose executable and no Docker daemon, so a host that prepares or inspects a task needs neither.

Every key is classified as **supported**, **equivalent** (accepted, but with no effect or a documented approximation), **warning**, or **rejected**. A single rejection rejects the task, with its field path, before any sandbox is created. Run `flotilla inspect` to see the findings for a task.

## Task layout

```text
task/
  task.toml
  environment/
    docker-compose.yaml    # optional; the only Compose file name that is read
    .env                   # optional; interpolation values, with CONTEXT_DIR and caller values
```

- The main (agent) service is always named `main`. A task without a `main` service is rejected.
- Without a Compose file, the task has a single `main` service running `[environment].docker_image`.
- With a Compose file, `main` gets `command: ["sh", "-c", "sleep infinity"]` unless the Compose file overrides it. The task's Compose file is merged on top, as with `docker compose -f base -f task`.
- Service names must match `[A-Za-z0-9][A-Za-z0-9_.-]*`.

### `task.toml`

| Key | Effect |
|---|---|
| `[task].name` | Task name (defaults to the directory name); used for `[resources.tasks.*]` |
| `[environment].docker_image` | Image for `main`; required, since `build` is rejected |
| `[environment].workdir` | `working_dir` of `main` |
| `[environment].cpus` | CPU of `main` |
| `[environment].memory` / `memory_mb` | Memory of `main` (set only one) |
| `[environment].network_mode` | `"public"` or `"no-network"`; the latter removes external access from all services while preserving their Compose networks |
| `[environment].allow_internet` | `false` selects `no-network`, `true` selects `public`. Explicit `network_mode` takes precedence; otherwise ignored when `allowed_hosts` is present |

`OpenOptions(disable_internet=True)` and Harbor's `no-network` policy also remove external access. A task-level restriction cannot be relaxed by a caller or deployment default. Cluster DNS may remain reachable (see [Networking](#networking)).

## Reading a project

`flotilla.compose.parse.load_project` does YAML load, interpolation, `env_file` expansion, the ordered merge of several files, and Harbor's synthetic `main` service. The caller supplies an environment directory, ordered Compose files, and explicit interpolation values; the loader defaults to that directory's `docker-compose.yaml`. The ambient process environment is never read, so a task resolves to the same project on any host. `CONTEXT_DIR` is the environment directory; other Harbor variables are supplied explicitly by the caller.

Only three kinds of file are opened: the Compose files named in the arguments, a task-local `.env`, and the literal `env_file` paths a service declares. Each is confined to the environment directory, symlinks resolved. A dynamic `$` path in `env_file` is refused. `include` and `extends` name further files and are rejected by name rather than expanded.

Harbor's explicit `docker_image` supersedes the resulting main image, and its explicit working directory wins. Bind sources are rewritten to normalized task-relative paths, because attempt-owned storage is keyed by bind source and `conf`, `./conf` and `./x/../conf` must name one thing. A path outside the environment directory is refused. YAML anchors and `<<` merge keys are resolved while loading; Compose's `!reset` and `!override` tags are not implemented and fail the load.

## Top-level keys

| Key | Status |
|---|---|
| `services` | Supported; must be a non-empty mapping |
| `name`, `version` | Equivalent (ignored) |
| `networks` | Supported with restrictions, see below |
| `volumes` | Named volumes without options only (`data: {}` or `data:`) |
| `x-*` | Equivalent (recorded only). Anchors are resolved first, so a surviving `x-` key is leftover metadata; Compose strips the service-, network- and volume-level extension fields itself |
| `secrets`, `configs`, `include`, others | Rejected |

Top-level networks support `internal: true`. `driver` must be `bridge`, `ipam` must be empty or `{driver: default}` (the platform assigns addresses), and only the bridge name, MTU, and `gateway_mode_ipv4/ipv6` driver options are accepted. `external` networks and volumes are rejected because trials never share resources. Unused networks are dropped; a `default` network is created implicitly.

## Why a field is rejected

Every rejection carries one of seven categories, which say what would have to change for the field to become supported. `normalize.py` holds the same category for every key and a test keeps the two in step.

| Category | Meaning | What would change it |
|---|---|---|
| `unimplemented` | Flotilla's own work; nothing outside it is missing | Implementing it here |
| `out-of-scope` | A different job from orchestration; flotilla turns published images into sandboxes | Doing that job elsewhere, before preparation |
| `platform-gap` | The task semantics are real, but the sandbox API has no request surface for them | The platform growing that surface |
| `no-model` | A sandbox is one container in one isolation domain, so the field has no subject | Nothing; the field would stay rejected on any platform |
| `unsafe` | Honoring it would remove the isolation the sandbox exists to provide | Nothing |
| `not-reproducible` | It shares state across trials, which training requires to stay independent | Nothing |
| `docker-internal` | A Docker mechanism with no meaning here; emulating it would invent a semantics | Nothing |

A field marked `mixed` spans categories: some of its values belong in one, some in another. The whole field is rejected and carries the strictest of them, which is coarse but never wrong.

## Service keys

| Key | Status |
|---|---|
| `image` | Supported |
| `build` | Rejected (out-of-scope): building an image is a different job from orchestrating one. Preparation consumes images that are already published, so it needs no builder, push target or registry write access. Publish the image first and name it in `image` or `[environment].docker_image` |
| `command`, `entrypoint` | Supported (string form is split like a shell word list, without a shell). Docker semantics: `entrypoint` drops the image `CMD` |
| `environment` | Supported, including names Docker accepts that are not shell identifiers (`discovery.type`). A key without a value (inherit from host) is rejected: there is no host environment |
| `env_file` | A path or list of paths inside `environment/`, in short or long (`path`, `required`) form. Paths must be literal and stay inside the task. `environment` entries win |
| `working_dir` | Supported |
| `user` | Supported; resolved to numeric uid/gid from the image |
| `depends_on` | List or map. Conditions `service_started`, `service_healthy`, `service_completed_successfully`, and `required` are supported; `restart` is equivalent (services are never restarted) |
| `healthcheck` | `test`, `interval`, `timeout`, `retries`, `start_period`, `start_interval`, `disable`. `test` must be given (inheriting the image's test is rejected); `["NONE"]` or `disable: true` turns the image's healthcheck off |
| `volumes` | See [Volumes and bind mounts](#volumes-and-bind-mounts) |
| `tmpfs`, `type: tmpfs` | Native memory mounts; requires verified `native_tmpfs`. Short syntax supports size/mode/uid/gid; long syntax supports size/mode. No directory emulation |
| `shm_size` | `/dev/shm` capacity; requires the `sandbox-system-v1` filesystem dialect (rejected on upstream OpenSandbox). Counts against the same memory budget as tmpfs. Without it the platform default applies. See [OpenSandbox backend](backends/opensandbox.md#read-only-root-filesystems-tmpfs-and-shared-memory) |
| `networks` | List or map with `aliases`. `gw_priority`/`priority` are equivalent: egress follows the sandbox policy. Static `ipv4_address`/`ipv6_address` are rejected |
| `network_mode` | Only `none`. `host`, `bridge`, `service:*`, and `container:*` are rejected |
| `ports`, `expose` | Equivalent: nothing is published, and services on a shared network reach every port. Port ranges and protocols other than TCP/UDP are rejected; UDP requires the deployment's `link_udp` capability |
| `restart` | `no` is supported. `always`, `unless-stopped`, `on-failure` and `on-failure:N` are equivalent: services are not restarted, and the run is marked void if a service exits in a way Docker would have restarted |
| `read_only` | Service root filesystem protection; `true` requires the `sandbox-system-v1` filesystem protocol. Independent of volume read-only flags |
| `privileged` | Runs the service on the privileged runtime (`opensandbox.privileged_extensions`, e.g. sysbox): root holds all capabilities inside its own user namespace, which is not Docker privileged mode on the host (warning). A platform without a privileged runtime refuses the create; run the probe with `probe.privileged_image` to measure its isolation. Read-only rootfs, tmpfs and `shm_size` are not available; writable trial volumes are opened as for devices |
| `hostname`, `container_name` | Added as names other services can resolve. `hostname` is a warning: `gethostname()` inside the sandbox still returns the platform's name |
| `extra_hosts` | Literal IPv4/IPv6 addresses only, in mapping or `name:address` list form. They join the `/etc/hosts` block flotilla already writes to publish service names, are not filtered by network membership, and do not go through the platform's address lookup. Reachability is still the egress policy's decision. `host-gateway` is rejected at the value level (`no-model`): a sandbox has no host to reach |
| `links` | `service` or `service:alias`. The alias is published on a network both services are on; when they share none both join `default`. Unlike Docker, the alias is visible to every member of that network, not only the service that declared the link |
| `volumes_from` | `service` or `service:ro`. The donor's declared mounts are added to this service, with `:ro` folded into each one's read-only flag. It does not follow the donor's own `volumes_from`, and a target this service already mounts is a conflict, rejected rather than silently replaced. `container:` is rejected: it names a Docker container outside the project |
| `cpus`, `mem_limit`, `deploy.resources.limits.{cpus,memory}` | Supported; if both forms are given they must agree. Memory units are binary (`g` = GiB) |
| `init`, `labels`, `logging`, `stop_signal`, `stop_grace_period`, `pull_policy` | Equivalent (recorded only) |
| `cap_drop` | Rejected (platform-gap): the sandbox API exposes no capability control, and dropping is what the task relies on — silently ignoring it would let an exploit the task expects to fail succeed and grade as solved |
| `group_add`, `ulimits`, `pids_limit` | Rejected (platform-gap): the sandbox API exposes no supplementary groups, per-process rlimits, or process-count limit |
| `security_opt`, `sysctls` | Rejected (unsafe, mixed): each field spans both directions. `no-new-privileges` and namespaced sysctls (`net.*`, `kernel.shm*`, `fs.mqueue.*`) affect only this sandbox and belong with `cap_drop`; `seccomp=unconfined`, `apparmor=unconfined` and non-namespaced sysctls remove the sandbox's defenses or reach the host |
| `dns`, `dns_search`, `dns_opt` | Rejected (docker-internal): cluster DNS is fixed and always reachable; a task-supplied resolver would fight the egress policy |
| `platform`, `memswap_limit`, `domainname` | Rejected (docker-internal): every image is pinned by digest, which fixes `platform`; swap is not separately accountable; an NIS domain has no counterpart |
| `pid`, `ipc`, `userns_mode`, `runtime`, `cgroup` | Rejected (no-model): one sandbox runs exactly one container, so `pid`/`ipc` sharing has no peer and the sandbox is already the isolation domain. The runtime and cgroup placement are deployment properties |
| `tty`, `stdin_open` | Rejected (unimplemented): both require a pty |
| `devices` | Only `/dev/fuse`, `/dev/kvm` and `/dev/net/tun`, at the same path with `rwm` (the default). The service runs on the privileged runtime with the matching platform extension, written per instance so one service can hold a device without every sandbox carrying it; a runtime that cannot host the device refuses the create request carrying its extension. Read-only rootfs, tmpfs and `shm_size` are not available on that runtime. Trial volumes such a service mounts writable are opened to every identity (`chmod -R a+rwX`), because its root does not own anchor-created storage. Other devices are rejected (unsafe) |
| `cap_add` | Only `[NET_ADMIN]` together with `/dev/net/tun` (equivalent: root in the privileged runtime holds it in the sandbox network namespace). Other capabilities are rejected (unsafe) |
| `extends`, `profiles`, `secrets`, `configs`, `scale` | Rejected (unimplemented): each would expand into something the model already has — merged documents, mounts, or several sandboxes. The parser never expands `extends`, so it is reported by name rather than acted on |
| Any other key, including `deploy` keys other than resource limits and `x-*` | Rejected |

A service can have dependents requiring health and others requiring successful completion. Both conditions are tracked independently; a service required only for successful completion does not need to pass its healthcheck.

## Volumes and bind mounts

Short syntax `source:target[:ro|rw]` is a bind mount when the source starts with `.`, `/`, or `~`, and a named volume otherwise. Long syntax supports `type` (`volume`, `bind`, `tmpfs`), `source`, `target`, `read_only`, `volume.nocopy` (equivalent), and `bind.create_host_path`. Anonymous volumes are rejected, the target must be absolute (and not `/` or under `/.flotilla`), and bind sources must be relative paths inside `environment/`.

Every file-backed mount uses attempt-owned storage and requires the deployment's `shared_volume` capability. Nothing is baked into an image. Equal bind sources, and a mounted source together with its mounted descendants, share one backing root while each mount keeps its own read-only flag. Parent target mounts precede nested target mounts.

- A read-only bind group is mounted where it is published, straight from the task directory, and shared by every attempt of that task. Nothing is copied to run it.
- One writable reference makes the whole group attempt-owned, because the references share backing storage: it is copied out of the task directory first, so a run cannot write into published task data.
- Symlinks and nonregular entries inside bind trees are rejected; a missing bind source is rejected rather than fabricated, since the platform would otherwise mount an empty directory over the target and a task whose files were never committed would grade as unsolvable. Hardlinks spanning independent bind roots are rejected. This is not a complete POSIX snapshot contract (extended attributes and ACL fidelity are not promised).
- `/tmp` must stay writable (framework file transfers and the exec daemon write there), so a read-only mount on it is rejected. Mounting a single file not copied into an image requires the `volume_file` capability.
- A named volume always starts empty. Docker copies the image content under the mount target into a fresh volume; flotilla does not, because emulating that means reading every service image and shipping its content with the release. `volume.nocopy: true` is therefore recorded as equivalent — it asks for what already happens. A task that needs content in a volume should bind-mount it from the task directory, where it is visible and versioned.

For read-only root filesystems, even single-service writable volumes/binds use real environment-owned mounts. Framework scratch space is mounted explicitly at `/.flotilla-runtime`; this path and `/.flotilla` are reserved. See the [OpenSandbox backend](backends/opensandbox.md#read-only-root-filesystems-tmpfs-and-shared-memory) for deployment selection, supported syntax, limits and defaults.

## Networking

Services that share a Compose network can reach each other by service name, aliases, `hostname`, and `container_name`. Services on at least one non-`internal` network get the outbound policy from the deployment's `[network].external`; services only on internal networks, or with `network_mode: none`, get no outbound access. `OpenOptions.disable_internet=True` removes outbound access for every service. Multi-service tasks must stay within `[platform].link_max_members`.

Offline policies tolerate only explicitly declared cluster DNS resolvers on TCP/UDP port 53. The deployment lists their concrete IPs in `[platform].cluster_dns` and every exception in `[platform].implicit_egress`. Other implicit destinations, ports, and unrecognized declarations reject offline services during inspection and at runtime. This applies to task-level `no-network`, internal networks, Compose `none`, and the runtime option.

Compose `network_mode: none` also excludes the service from peer links and requires inbound isolation: either the deployment's recorded capability or explicit sandbox ingress enforcement. It isolates service traffic while preserving declared cluster DNS and platform management access; it does not remove the sandbox's network interface. See [OpenSandbox network configuration](backends/opensandbox.md#storage-and-network).

## Variable interpolation

Interpolation supports `${VAR}`, `${VAR:-default}`, `${VAR-default}`, `${VAR:+alt}`, `${VAR+alt}` and `$$` for a literal dollar, with references allowed inside a default. The host environment is never read: values come from `environment/.env`, from `CONTEXT_DIR` (the absolute environment directory), and from the values the caller supplies for the run — the deployment's `runtime_params` table and a dataset row's `parameters` (the more specific wins). Interpolation happens once, at preparation or inspection; there is no deferred substitution at open time.

Three rules are stricter than Compose, each a case where Compose substitutes something the author did not write, so flotilla refuses the task rather than preparing a surprising one:

- A reference that nothing defines and has no default is **rejected**. Compose only warns and substitutes a blank string, which would bake an empty string into (for example) an image reference and fail much later. A reference with a default (`${VAR:-x}`) needs no value.
- A bare `$VAR` is rejected; `${VAR}` is required.
- A `$` in an env file value or variable name is rejected, quoted or not: Compose interpolates those values in turn against the shell environment, earlier keys, earlier files, and the service's own `environment`, so one value can depend on another's.

Each stricter rule is pinned by a test asserting Compose reads what flotilla refuses, so it stays a deliberate choice. `tests/contract/compose_differential.py` is the agreement evidence: with `FLOTILLA_TEST_COMPOSE` (an official Compose executable, driven by `tests/contract/official_compose.py`) and `FLOTILLA_TEST_CORPUS` (dataset roots) it reads every task both ways and requires the resulting `Project`s to be equal field by field, findings included. Agreement over a corpus is evidence about that corpus; a field form no generator writes is covered by written cases instead.
