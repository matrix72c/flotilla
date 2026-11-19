# Running published tasks

Flotilla owns the execution protocol. A task is a published directory; running it is a local
computation over that directory plus each image's config, so there is no build step, no
intermediate artifact and nothing Flotilla writes and reads back. Harbor supplies the task
format and is a required dependency; using Harbor as an evaluation consumer is optional.

## Install

Use Linux x86_64 and Python 3.12. Install a wheel from this repository or a full Git commit from
`https://github.com/matrix72c/flotilla.git`. The distribution, import and executable are
`flotilla`. The public PyPI project with that name is unrelated: do not use bare
`pip install flotilla`. Do not install `flotilla-compose` alongside the renamed package. Use a new
environment rather than modifying an environment used by running jobs.

Nothing needs Docker, a Compose executable or a GPU stack. Flotilla loads Compose itself and reads
image configs from the registry over HTTP, by digest. A CPU environment does not require Torch,
CUDA, XTuner or LMDeploy.

## A task directory

```text
<task>/
  task.toml                       # [environment] image and resources, [verifier] env and timeout
  instruction.md                  # what the agent is asked to do
  environment/
    docker-compose.yaml           # the services, or omitted for a single-service task
    ...                           # bind sources, referenced relative to this directory
  tests/                          # hidden tests, staged only for verification
```

Every `image:` must be `repo@sha256:...`. A tag is refused: it can move between two reads, so a run
pinned to one would not be reproducible, and its provenance would not be in the task. Resolve tags
to digests when the dataset is published.

A task declaring `build:`, or a single-service task with only a `Dockerfile` and no
`[environment].docker_image`, is refused. Publish the image first and name it; producing images is a
different job from running them.

Every image's own `USER` must be root (empty, `0`, `0:0` or `root`): the exec daemon is the sandbox
entrypoint. Declare the application identity with Compose `user`, numerically as `uid[:gid]`. A named
user is refused, because resolving it would mean reading the image's `/etc/passwd`.

Multi-step Harbor tasks are refused by the protocol itself — the lifecycle is one agent run plus one
verification — not by a consumer option.

## A dataset

A dataset is a directory of task directories. Both sides are declared because neither can infer the
other: a client path says nothing about how the platform mounts the storage.

```python
datasets={
    "example": DatasetBinding(
        local_root="/srv/datasets/example",          # absolute: a spec outlives its own directory
        platform_root="/shared/datasets/example",
        # input="datasets",                          # the inputs[<name>] root it lives on; None means work
    )
}
```

`local_root` must be absolute: a spec is imported by the client, Harbor workers and `flotilla gc`
alike, and a relative path would mean something different in each. For host storage, `platform_root` is a key relative to the shared root or an absolute path under it. For
PVC storage use the explicit `pvc:<claim>/<configured-root-subpath>/<key>` form; a bare relative key
is refused, and an arbitrary absolute host path is not turned into a PVC key by stripping a prefix.

`launches/` and `share/` are refused as dataset roots: those are Flotilla's own state, not task data.

Loading a deployment opens no dataset. Resolving one checks that the root is a directory on declared
storage; reading a task happens when it is planned.

## Publishing is yours

Publish the task directories with your existing rsync, shared storage or object-storage tools.
Flotilla does not upload a dataset for you, and it never writes to one: task files are mounted
read-only from where they are published. Preserve bytes and modes. A gateway PUT acknowledgement may
precede visibility through a separate NFS client; wait for visibility before consuming.

Publish shared tools and the agent runtime separately, and grant the platform read access to the
fixed runtime images. Do not place credentials, source checkout paths or image archives in a dataset.

A dataset must be maintained by a trusted publisher and read-only to workloads. Do not mutate one
that running jobs are reading.

An optional typed `[agent_runtime]` section binds a consumer's runtime layers (arbitrary names, each
with local/platform roots); Flotilla validates shape and roots but not which layers exist — that
contract belongs to the consumer. Flotilla does not interpret agent types, GPU resources, reward or
training configuration.

## Running one

```python
from flotilla import Flotilla
from flotilla.native import NativeAttempt

from myproject.deployment import PLATFORM

dataset = PLATFORM.resolve_datasets()["example"]

async with Flotilla(deployment=PLATFORM) as fleet:
    for key in dataset.tasks():
        async with await NativeAttempt.create(fleet, dataset.source(key)) as attempt:
            environment = await attempt.open("main")
            # Run the agent through the existing environment/service API.
            # Verification stages hidden tests and explicit role handoffs.
            result = await attempt.verify()
```

`dataset.tasks()` lists the keys below the root in a stable order; `dataset.source(key)` names one on
both sides. Which keys to run, and how often, is your selection: duplicates stay duplicate attempts.

## Checking a dataset before spending GPU time

Resolving a task is pure and synchronous, so a whole dataset can be checked first:

```python
for key in dataset.tasks():
    task = NativeAttempt.resolve(fleet, dataset.source(key))   # raises on a task that cannot run
```

`Flotilla.plan(source)` returns the same trial for a single-role task without the grading contract.
Both read image configs through the client's cache, so checking a dataset also warms it: a digest
names one config forever, and a second task naming the same image costs a dict lookup.

`flotilla inspect` is the same check as a command, for a shell script or a submit hook that has no
client yet:

```sh
flotilla inspect example --deployment myproject.deployment:PLATFORM
```

With no argument it checks every dataset the spec declares; a local path checks a dataset that is not
declared yet, planned with no platform key so its storage rules go unchecked. A nonzero exit means at
least one task would not open, and `--out report.json` records each one with the fields that were
refused. Making that a gate — refusing to submit a job whose dataset does not fully resolve — belongs
to whatever submits your jobs; Flotilla provides the check and the exit code, not the policy.

## Anchor image builder

```python
from flotilla.anchor_build import AnchorBuildOptions, build_anchor

result = build_anchor(AnchorBuildOptions(image="registry.example.org/team/anchor:VERSION"))
# Add push=True and optionally authfile= to publish explicitly.
```

The builder assembles a scratch filesystem from packaged assets and a hash-verified pinned BusyBox:
the multicall binary at `/.flotilla/bin/busybox`, PATH symlinks for the anchor's directory commands,
`/etc/passwd` and `/etc/group`, the GPL text, and a self-check script. It copies no source checkout,
credentials or compiler stack, and pulls no base image. Build context and downloaded
archives are isolated temporary directories.

A new container verifies the applets without network or source/credential mounts. Publication
uses configurable Docker/Skopeo argv and manifest format (`v2s2` by default for the current internal
registry). `result.anchor_pin` is set only after obtaining the remote digest. Local image IDs
are not deployment pins. Platform behavior is still measured by probe; self-containment does not
replace platform or GPU acceptance.

## What a consumer may and may not do

Consumers may pull the fixed runtime images normally. Missing assets and incompatible packages fail
explicitly. Infrastructure and cleanup failures are errors, not task scores. Reward interpretation
belongs to the consumer.

Installation, fake-platform tests, real platform behavior and GPU training are different
verification scopes. Consult recorded acceptance results rather than assuming one proves the others.
