# Compose compatibility for Harbor attempts

Flotilla translates a supported Compose subset into sandbox processes and mounts.
It does **not** run Docker Compose containers and does not promise full Docker,
PID 1, signal-delivery, process-tree, or container-inspection equivalence.
Unsupported declarations fail closed; there is no automatic Docker-in-Docker
fallback. `privileged: true` runs on the privileged runtime with a warning;
that sandbox is not treated as Docker privileged mode on the host.

## Reading a project

`flotilla.compose.parse.load_project` reads a task's Compose files in-process:
YAML load, interpolation, `env_file` expansion, the ordered merge of several
files, and Harbor's synthetic `main` service. There is no Compose executable and
no Docker daemon, so a host that prepares or inspects a task needs neither.

The caller supplies an environment directory, ordered Compose files, and
explicit interpolation values; the loader defaults to that directory's
`docker-compose.yaml`. Caller values override a task-local `.env`, as the
process environment overrides it in Compose, and the ambient process
environment is never read: a task resolves to the same project on any host.
`CONTEXT_DIR` is the environment directory. Other Harbor variables must be
supplied explicitly by the caller.

Only three kinds of file are opened: the Compose files named in the arguments, a
task-local `.env`, and the literal `env_file` paths a service declares. Each is
confined to the environment directory, symlinks resolved. A dynamic `$` path in
`env_file` is refused. `include` and `extends` would name further files and are
never acted on: they reach `normalize` and are rejected there by name, with
their field path, rather than expanded.

Harbor's explicit `docker_image` supersedes the resulting main image, and its
explicit working directory also wins. Bind sources and build contexts are
rewritten to normalized task-relative paths, because the layout keys
attempt-owned storage by bind source and `conf`, `./conf` and `./x/../conf` must
name one thing. A path outside the environment directory is refused. Arbitrary
Docker resource names and external resources are not supported.

`normalize` is the pure translation entry: it maps that document to the runtime
model and classifies fields, and never substitutes a variable or opens a file.

## Where the dialect differs from Compose

Interpolation supports `${VAR}`, `${VAR:-d}`, `${VAR-d}`, `${VAR:+a}`,
`${VAR+a}` and `$$`, with references allowed inside a default. A reference
nothing provides a value for and that has no default is **refused**: Compose
only warns and substitutes a blank string, so a task referencing `${TAG}` would
otherwise bake an empty string into an image reference and fail much later with
an unrelated-looking error.

Three further rules are stricter than Compose, each a case where Compose
substitutes something the author did not write. A bare `$VAR` is refused;
`${VAR}` is required. A `$` in an env file value or variable name is refused,
quoted or not: Compose interpolates those values in turn, against the shell
environment, the keys above them in the same file, the files read before them,
and the service's own `environment`, so one env file's value can depend on
another's, and a single-quoted value stays literal while a double-quoted one is
substituted. Each is pinned by a test asserting Compose reads what flotilla
refuses, so it stays a choice rather than quietly becoming redundant.

Three Compose rules that are easy to miss are reproduced rather than
simplified. A key written with no value is refused, as Compose's schema refuses
it, except `command`, `entrypoint` and `deploy`, where the null means "unset"
and resets what an earlier file set. One mount survives per target: a repeated
target keeps the last declaration at the first position, and two targets that
differ as written but name the same directory (`/x/` and `/x`) are refused. A
declared network or named volume nothing uses is dropped.

YAML anchors and `<<` merge keys are resolved while loading. Compose's
`!reset` and `!override` tags are not implemented and fail the load.

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

`tests/unit/test_compose_parse.py` states each parser rule once, so CI covers
them with no executable and no datasets: which files are opened, what is
refused, interpolation form by form, and the Compose rules above. Model and
preparation tests cover null/empty argv, shared mixed RO/RW binds, nested
mounts, named volumes, real single-reference mounts and hardlinks.

`tests/contract/compose_differential.py` is the evidence that the parser agrees
with Docker. It is opt-in: `FLOTILLA_TEST_COMPOSE` names an official Compose
2.39.4 executable, which `tests/contract/official_compose.py` drives to produce
a reference `Project`. That reader lives in the tests, not the package — the
package has no Compose adapter.

Its two sets of inputs fail differently and neither replaces the other.
`FLOTILLA_TEST_CORPUS` names dataset roots: every task in them is read both ways
and the resulting `Project`s must be equal field by field, findings included.
That covers what generated tasks actually use and says nothing about a field no
generator writes yet, so written cases cover what a corpus does not contain at
all — interpolation forms, `env_file`, `.env`, multi-file merge, short volume
syntax, null argv reset — and the strictness rules above are pinned by a test
asserting Compose reads what flotilla refuses.

Agreement over a corpus is evidence about that corpus. A field form no generator
writes today is unverified until someone writes it, which is the standing cost
of maintaining the dialect rather than shelling out. This normalization-only
check uses no Docker daemon or workloads, and translation is not proof of Docker
equivalence.
