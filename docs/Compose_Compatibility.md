# Compose compatibility for Harbor attempts

Flotilla translates a supported Compose subset into sandbox processes and mounts.
It does **not** run Docker Compose containers and does not promise full Docker,
PID 1, signal-delivery, process-tree, or container-inspection equivalence.
Unsupported declarations fail closed; there is no automatic Docker-in-Docker
fallback. `privileged: true` runs on the privileged runtime with a warning;
that sandbox is not treated as Docker privileged mode on the host.

## Production normalization

Harbor production loading uses `flotilla.compose.adapter.ComposeConfigAdapter`.
The adapter requires the official Compose CLI at the exact configured release
(default **2.39.4**). Its executable argv and expected version are constructor
arguments. Missing tooling, a different version, or a failed `config` invocation
is an error, never a reason to use the lightweight parser. The adapter does not
install tooling or contact a Docker daemon.

The caller supplies an attempt-private environment directory, ordered Compose
files, and explicit interpolation variables. The loader defaults to that
directory's `docker-compose.yaml`. The adapter passes an explicit project
directory, project name, file list and `.env` path (or `/dev/null`). The process
environment contains a fixed executable-search PATH and caller-supplied values,
not the invoking shell's environment. `CONTEXT_DIR` is the environment directory.
Other Harbor variables must be supplied explicitly by the caller.

An explicit base document provides Harbor's main-service keepalive and default
image/build. Official `compose config --format json --no-path-resolution` owns
file merging, interpolation, `env_file` loading, and short-form expansion.
Harbor's explicit `docker_image` supersedes the resulting main image/build;
its explicit working directory also wins. Compose's serialized `$$` escaping is
decoded exactly once, including mapping keys, without evaluating variable names
or expressions. Resolved output is translated without a second interpolation
pass: literal dollars remain literal and there are no implicitly deferred
environment variables. Compose-resolved bind/build paths
must remain within the environment directory. Before any official CLI call, a
narrow YAML-node gate rejects `include`, `extends`, `label_file`, `configs`, and
`secrets`, and confines `.env` and literal service `env_file` paths to that
directory (including symlink resolution). Dynamic `$` paths in `env_file` are
unsupported. This gate preserves YAML aliases and official `!reset`/`!override`
tags without implementing Compose merging or interpolation. Project-generated
resource names are removed; arbitrary Docker resource names and external
resources are not supported.

`normalize(..., resolved=True)` is the pure resolved-model translation entry.
The legacy lightweight `normalize`/loader mode remains available for offline
model tests and existing lower-layer callers; it is **not** an official Compose
merge/interpolation implementation or a production fallback.

## Storage and command behavior

- All file-backed mounts use attempt-owned storage, even one writable reference
  or an exclusively read-only volume. There is no reference-count-based image
  baking and no public persistent task-data dataset in the Harbor path.
- Equal bind sources and a mounted source plus its mounted descendants share
  one backing root. Each mount retains its own read-only/read-write flag.
  Parent target mounts precede nested target mounts.
- Build export `files/binds/<index>`, `files/seeds/<volume>.tar`, and adjacent
  `file-identities.json` are **internal transient staging**, retained for the
  lower-level runtime's copy/seed contract. The attempt owner must upload and
  clean them up, restore numeric metadata, and preserve hardlinks during
  transfer. The manifest uses trial-scope mounts and trial-volume `copy_from`
  or `seed`, not shared read-only task mounts.
- Hardlinks inside one bind source tree are preserved during export. Hardlinks
  spanning independent bind roots are rejected because separate runtime copies
  cannot preserve their identity. Symlinks and nonregular entries inside bind
  trees are rejected; missing bind sources are rejected rather than fabricated.
  This is not a complete POSIX filesystem snapshot contract (for example,
  extended attributes and ACL fidelity are not promised).
- Named volumes start empty with `volume.nocopy: true`. Copy-enabled references
  can supply image content and numeric root/file metadata through a seed tar.
  A nocopy reference cannot provide a seed but still sees content initialized by
  another copy-enabled reference. Multiple copy-enabled references must identify
  the same pinned image digest and image path (registry aliases are allowed).
  Different image/path sources are rejected before extraction because identical
  initialization content cannot be proven; the exporter never silently chooses
  the first populated source or approximates Docker's initialization race.
- Null command/entrypoint means inherit; an empty string/list means explicitly
  empty. An explicit entrypoint drops the image CMD unless command supplies a
  replacement. Command strings are argv, not an implicit shell. These are argv
  selection rules only, not a promise that the selected process is container PID 1.

## Explicitly unsupported semantics

The scanner/build boundary rejects, rather than silently approximates:

- Published ports, including target-only declarations that would allocate a host
  port automatically. `expose` alone does not publish ports.
- Actual `hostname` and `container_name`. Network aliases are not replacements
  for these identities.
- Restart policies other than `no`, dependency restart propagation, `init`,
  `stop_signal`, and `stop_grace_period`.
- Docker privileged mode, logging-driver configuration, pull policies, bridge
  driver options, nonzero network attachment priorities, and the other
  unsupported security/device/namespace fields reported by normalization.

Filesystem mounts, networking, health checks, and resource limits also remain
subject to the deployment's declared/probed capabilities. Successful model
translation is not proof of Docker equivalence or deployment support.

## Verification scope

Unit contracts mock the official Compose subprocess, cover version/error
handling and explicit arguments, and verify that resolved dollar expressions
are not expanded again. Model/build tests cover null/empty argv, shared mixed
RO/RW binds, nested mounts, nocopy seeds, real single-reference mounts, hardlinks,
and rejected formerly ignored semantics. Opt-in tests in
`tests/contract/test_compose_official.py` also ran successfully against a
checksum-verified temporary official Compose 2.39.4 executable. They cover
ordered files, reset/override tags, null/empty argv, env files, and one through
five literal dollars originating in YAML, explicit environment, `.env`, and raw
or quoted service env files. This normalization-only check uses no Docker daemon
or workloads and does not verify the management image's build/runtime toolchain.
The temporary executable was not installed into the resident environment.

`tests/contract/compose_runtime.py` additionally compares actual service-startup
observations under official Docker Compose and `HarborAttempt`. The 2026-11-08
OpenSandbox run passed explicit expectations on both sides for spaced/empty argv,
literal dollars, `.env` and quoted/raw environment files, shared RO/RW binds,
descendant sharing, and nested read-only overlays. Owned Docker/platform resources
and fixture data were confirmed removed. This is a deterministic runtime contract,
not Agent/verifier or general Docker-equivalence evidence.

To repeat on an admitted deployment, supply an immutable Python 3 image and a
scratch directory mounted at the same absolute path on the driver and Docker host:

```sh
python tests/contract/compose_runtime.py \
  --deployment /path/to/deployment.toml --out /path/to/result.json \
  --compose /absolute/path/to/docker-compose \
  --image 'python@sha256:<digest>' --local-root /same/path/scratch
```

The driver needs explicitly configured Docker access and platform credentials;
these must never be mounted into the task fixture. Each runtime must match the
expected values, not merely produce identical output.
