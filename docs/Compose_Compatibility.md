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
image. Official `compose config --format json --no-path-resolution` owns
file merging, interpolation, `env_file` loading, and short-form expansion.
Harbor's explicit `docker_image` supersedes the resulting main image;
its explicit working directory also wins. Compose's serialized `$$` escaping is
decoded exactly once, including mapping keys, without evaluating variable names
or expressions. Resolved output is translated without a second interpolation
pass: literal dollars remain literal and there are no implicitly deferred
environment variables. Compose-resolved bind paths
must remain within the environment directory. Before any official CLI call, a
narrow YAML-node gate rejects `include`, `extends`, `label_file`, `configs`, and
`secrets`, and confines `.env` and literal service `env_file` paths to that
directory (including symlink resolution). Dynamic `$` paths in `env_file` are
unsupported. This gate preserves YAML aliases and official `!reset`/`!override`
tags without implementing Compose merging or interpolation. Project-generated
resource names are removed; arbitrary Docker resource names and external
resources are not supported.

`normalize` is the pure resolved-model translation entry: it maps the adapter's
output to the runtime model and classifies fields, and never substitutes a
variable or opens a file. Offline tests drive it through a double that merges
the selected files without interpolating, not through a second parser.

## The dialect parser

`flotilla.compose.parse.load_project` reads the same inputs without an external
executable, producing the document `normalize` consumes. Nothing uses it yet;
it exists to be compared against official Compose before it replaces the
adapter, and that comparison is the only thing that makes it trustworthy (see
[verification scope](#verification-scope)).

It opens only the files named in its arguments, a task-local `.env`, and the
literal `env_file` paths a service declares, each confined to the environment
directory. `include` and `extends` are never acted on, so they reach `normalize`
and are rejected there by name rather than expanded. Caller values override
`.env`; the process environment is never read, so a task resolves to the same
project on any host. `${VAR}`, `${VAR:-d}`, `${VAR-d}`, `${VAR:+a}`, `${VAR+a}`
and `$$` are supported, with references inside a default; a reference nothing
provides a value for and that has no default is refused.

It is stricter than Compose in three places, each a case where Compose
substitutes something the author did not write. A bare `$VAR` is refused
(`${VAR}` is required). A `$` in an env file value or variable name is refused,
quoted or not: Compose interpolates those values in turn, against the shell
environment, the keys above them in the same file, the files read before them,
and the service's own `environment`, so one env file's value can depend on
another's, and a single-quoted value is kept literal while a double-quoted one
is substituted.

Beyond interpolation it reproduces three Compose rules that are easy to miss. A
key written with no value is refused, as Compose's schema refuses it, except
`command`, `entrypoint` and `deploy`, where the null means "unset" and resets
what an earlier file set. One mount survives per target: a repeated target keeps
the last declaration at the first position, and two targets that differ as
written but name the same directory (`/x/` and `/x`) are refused. Bind sources
and build contexts are rewritten to normalized task-relative paths, because the
layout keys attempt-owned storage by bind source and `conf`, `./conf` and
`./x/../conf` must name one thing.

## Storage and command behavior

- All file-backed mounts use attempt-owned storage, even one writable reference
  or an exclusively read-only volume. There is no reference-count-based image
  baking and no public persistent task-data dataset in the Harbor path.
- Equal bind sources and a mounted source plus its mounted descendants share
  one backing root. Each mount retains its own read-only/read-write flag.
  Parent target mounts precede nested target mounts.
- Build export `files/binds/<index>` and the adjacent `file-identities.json`
  are **internal transient staging**, retained for the lower-level runtime's
  copy contract. The attempt owner must upload and clean them up, restore
  numeric metadata, and preserve hardlinks during transfer. The manifest uses
  trial-scope mounts and trial-volume `copy_from`, not shared read-only task
  mounts.
- Hardlinks inside one bind source tree are preserved during export. Hardlinks
  spanning independent bind roots are rejected because separate runtime copies
  cannot preserve their identity. Symlinks and nonregular entries inside bind
  trees are rejected; missing bind sources are rejected rather than fabricated.
  This is not a complete POSIX filesystem snapshot contract (for example,
  extended attributes and ACL fidelity are not promised).
- Named volumes always start empty, unlike Docker, which copies the image
  content under the mount target into a fresh volume. `volume.nocopy: true` is
  recorded as equivalent: it asks for the behavior already in force. A task that
  needs content in a volume binds it from the task directory. `TrialVolume.owner`
  declares the empty directory's root identity.
- Null command/entrypoint means inherit; an empty string/list means explicitly
  empty. An explicit entrypoint drops the image CMD unless command supplies a
  replacement. Command strings are argv, not an implicit shell. These are argv
  selection rules only, not a promise that the selected process is container PID 1.

## Docker-only semantics

Fields that only change local Docker behavior are recorded, not approximated:
published ports (peers on a shared network reach every port), `init`,
`logging`, `stop_signal`, `stop_grace_period`, `pull_policy`, bridge
`gateway_mode`/name/MTU options and network attachment priorities. Restart
policies other than `no` never restart a service; an exit Docker would restart
voids the run. `hostname` and `container_name` become names peers resolve;
`gethostname()` keeps the platform's value.

The scanner/build boundary still rejects Docker privileged mode outside the
privileged runtime, other bridge driver options, and the security, device and
namespace fields listed in [Compose support](Compose_Support.md).

Filesystem mounts, networking, health checks, and resource limits also remain
subject to the deployment's declared/probed capabilities. Successful model
translation is not proof of Docker equivalence or deployment support.

## Verification scope

Unit contracts mock the official Compose subprocess, cover version/error
handling and explicit arguments, and verify that resolved dollar expressions are
not expanded again and that an undefined reference without a default is refused
rather than blanked. Model and preparation tests cover null/empty argv, shared mixed RO/RW
binds, nested mounts, named volumes, real single-reference mounts and hardlinks.

`tests/contract/test_compose_official.py` is an opt-in check against an explicit
official Compose 2.39.4 executable, selected with `FLOTILLA_TEST_COMPOSE`. It
covers ordered files, reset/override tags, null/empty argv, env files, and one
through five literal dollars originating in YAML, explicit environment, `.env`,
and raw or quoted service env files. This normalization-only check uses no
Docker daemon or workloads and does not verify the management image's
build/runtime toolchain. Translation is not proof of Docker equivalence.

`tests/contract/compose_differential.py` compares the dialect parser against
that same executable. Its two sets of inputs fail differently and neither
replaces the other. `FLOTILLA_TEST_CORPUS` names dataset roots: every task in
them is read both ways and the resulting `Project`s must be equal field by
field, findings included. That covers what generated tasks actually use and says
nothing about a field no generator writes yet, so written cases cover what a
corpus does not contain at all — interpolation forms, `env_file`, `.env`,
multi-file merge, short volume syntax — and the three deliberate strictness
rules are pinned by a test asserting Compose reads what flotilla refuses, so
each stays a choice rather than quietly becoming redundant.

Agreement over a corpus is evidence about that corpus. A field form no
generator writes today is unverified until someone writes it, which is the
standing cost of maintaining the dialect rather than shelling out.

