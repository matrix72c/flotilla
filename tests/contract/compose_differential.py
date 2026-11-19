"""Differential check: flotilla's own Compose parser against pinned official Compose.

flotilla reads a subset of Compose and maintains that subset's agreement with Docker itself,
rather than shelling out to `docker compose config`. This is the evidence for that claim: the
same input is read both ways and the two `Project`s must be equal.

The last attempt at an in-house parser was withdrawn because the two quietly diverged -- an
undefined variable became a blank string on one path and a rejection on the other, and `inspect`
disagreed with preparation about the same task. So the comparison here is over whole `Project`
values, not spot checks, and it covers the findings as well as the services.

Two sets of inputs, because they fail differently. The corpus is every task of a real dataset,
which is what the generated tasks actually use and therefore the only check that covers what runs
today; it says nothing about a field no generator writes yet. The written cases below cover what
the corpus does not exercise at all -- interpolation, `env_file`, `.env`, multi-file merge, short
volume syntax -- where a divergence would otherwise wait for whoever writes that field first.

The executable is required; the corpus is separate, so a checkout without the datasets still
runs the written cases:

    FLOTILLA_TEST_COMPOSE=/abs/docker-compose \\
    FLOTILLA_TEST_CORPUS=/abs/datasets/vulhub2tb:/abs/datasets/vulhub_RL \\
    pytest tests/contract/compose_differential.py
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from flotilla.compose.adapter import ComposeConfigAdapter
from flotilla.compose.model import Finding, Project, Service
from flotilla.compose.parse import load_project
from flotilla.compose.task import COMPOSE_FILE, ENV_DIR, TASK_FILE

BINARY = os.environ.get("FLOTILLA_TEST_COMPOSE")
CORPUS = os.environ.get("FLOTILLA_TEST_CORPUS")
pytestmark = pytest.mark.skipif(not BINARY, reason="explicit pinned Compose executable required")


def _tasks() -> list[Path]:
    """Every task directory under the corpus roots, as `load_task` would find them."""
    assert CORPUS is not None
    out: list[Path] = []
    for entry in CORPUS.split(os.pathsep):
        root = Path(entry).resolve()
        if (root / TASK_FILE).is_file():
            out.append(root)
            continue
        for current, dirs, files in os.walk(root):
            if TASK_FILE in files:
                dirs.clear()
                out.append(Path(current))
            else:
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
    return sorted(out)


TASKS = _tasks() if BINARY and CORPUS else []


def _inputs(task: Path) -> tuple[dict[str, Any], Path]:
    """The arguments `load_environment` would pass, read from the task's own `task.toml`."""
    config = tomllib.loads((task / TASK_FILE).read_text())
    environment = config.get("environment") or {}
    env_dir = task / ENV_DIR
    compose = env_dir / COMPOSE_FILE
    image = environment.get("docker_image")
    main_image = image if isinstance(image, str) and image else None
    return {
        "files": (compose,) if compose.is_file() else (),
        "environment": {},
        "main_image": main_image,
        "main_build": main_image is None and (env_dir / "Dockerfile").is_file(),
        "workdir": environment.get("workdir"),
    }, env_dir


def _differences(official: Project, own: Project) -> list[str]:
    """Every field-level disagreement, so one run names them all instead of the first."""
    out: list[str] = []
    if set(official.services) != set(own.services):
        out.append(f"services: {sorted(official.services)} != {sorted(own.services)}")
    for name in sorted(set(official.services) & set(own.services)):
        out += [f"services.{name}.{d}" for d in _service_differences(official.services[name], own.services[name])]
    if official.networks != own.networks:
        out.append(f"networks: {official.networks} != {own.networks}")
    if official.volumes != own.volumes:
        out.append(f"volumes: {sorted(official.volumes)} != {sorted(own.volumes)}")
    out += _finding_differences(official.findings, own.findings)
    return out


def _service_differences(official: Service, own: Service) -> list[str]:
    return [
        f"{f.name}: {getattr(official, f.name)!r} != {getattr(own, f.name)!r}"
        for f in fields(Service)
        if getattr(official, f.name) != getattr(own, f.name)
    ]


def _finding_differences(official: tuple[Finding, ...], own: tuple[Finding, ...]) -> list[str]:
    """Findings are a report, so their order is not part of the claim; their contents are.

    The category is carried in `kind` plus the reason text, and both are compared: a field that
    is rejected on one path and merely recorded on the other is exactly the divergence that made
    `inspect` and preparation disagree before.
    """
    key = lambda f: (f.path, f.kind, f.reason)  # noqa: E731
    missing = sorted(set(map(key, official)) - set(map(key, own)))
    added = sorted(set(map(key, own)) - set(map(key, official)))
    return [f"finding only from official Compose: {f}" for f in missing] + [
        f"finding only from flotilla's parser: {f}" for f in added
    ]


def compare(label: str, env_dir: Path, **arguments: Any) -> None:
    """Read `env_dir` both ways and assert the two agree, on the verdict and then on every field."""
    assert BINARY is not None
    official_error: Exception | None = None
    own_error: Exception | None = None
    official = own = None
    try:
        official = ComposeConfigAdapter(command=(BINARY,)).load(env_dir, **arguments)
    except (ValueError, OSError) as exc:
        official_error = exc
    try:
        own = load_project(env_dir, **arguments)
    except (ValueError, OSError) as exc:
        own_error = exc
    # Either both read the input or neither does. The messages differ by design, so only the
    # verdict is compared: refusing an input the other accepts is the divergence that matters.
    assert (official_error is None) == (own_error is None), (
        f"{label}: official Compose {'failed: ' + str(official_error) if official_error else 'succeeded'}, "
        f"flotilla {'failed: ' + str(own_error) if own_error else 'succeeded'}"
    )
    if official is None or own is None:
        return
    differences = _differences(official, own)
    assert not differences, f"{label}:\n  " + "\n  ".join(differences)


@pytest.mark.skipif(not TASKS, reason="no task corpus given in FLOTILLA_TEST_CORPUS")
@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_corpus_task_reads_the_same_both_ways(task: Path) -> None:
    arguments, env_dir = _inputs(task)
    compare(str(task), env_dir, **arguments)


#: `(name, compose files, interpolation values, extra files to write)`. Each exercises something
#: the corpus does not contain at all, so a divergence here would otherwise surface only when a
#: generator first writes that field.
WRITTEN: list[tuple[str, list[str], dict[str, str], dict[str, str]]] = [
    (
        "interpolation forms",
        [
            """services:
  main:
    image: alpine
    environment:
      GIVEN: ${GIVEN}
      DEFAULTED: ${ABSENT:-default}
      UNSET_DEFAULTED: ${ABSENT-unset-default}
      BLANK_KEEPS_DEFAULT: ${BLANK:-fallback}
      BLANK_IS_SET: ${BLANK-not-used}
      ALTERNATE: ${GIVEN:+present}
      ALTERNATE_BLANK: ${BLANK:+not-used}
      ALTERNATE_SET: ${BLANK+set-but-blank}
      NESTED: ${ABSENT:-${GIVEN}}
      LITERAL: $$NOT_A_REFERENCE
      JOINED: prefix-${GIVEN}-suffix
    command: ["echo", "${GIVEN}", "$$HOME"]
"""
        ],
        {"GIVEN": "value", "BLANK": ""},
        {},
    ),
    (
        "env_file under environment",
        [
            """services:
  main:
    image: alpine
    env_file:
      - base.env
      - path: later.env
      - path: absent.env
        required: false
    environment:
      DECLARED_WINS: from-compose
"""
        ],
        {},
        {
            "base.env": "# a comment\nFROM_BASE=base\nOVERRIDDEN=base\nDECLARED_WINS=file-loses\n"
            "QUOTED='spaces  kept'\nDOUBLE=\"also kept\"\nTRAILING=value # not part of it\n"
            "export EXPORTED=exported\n",
            "later.env": "OVERRIDDEN=later\nFROM_LATER=later\n",
        },
    ),
    (
        "dotenv supplies interpolation values",
        [
            """services:
  main:
    image: alpine
    environment:
      FROM_DOTENV: ${FROM_DOTENV}
      CALLER_WINS: ${CALLER_WINS}
"""
        ],
        {"CALLER_WINS": "caller"},
        {".env": "FROM_DOTENV=dotenv\nCALLER_WINS=dotenv-loses\n"},
    ),
    (
        "ordered merge over several files",
        [
            """services:
  main:
    image: alpine
    command: ["first"]
    environment:
      KEPT: first
      REPLACED: first
    volumes:
      - {type: volume, source: data, target: /data}
  extra:
    image: alpine
    working_dir: /first
volumes:
  data: {}
""",
            """services:
  main:
    command: ["second", "two words"]
    environment:
      REPLACED: second
      ADDED: second
  extra:
    working_dir: /second
""",
        ],
        {},
        {},
    ),
    (
        "short volume syntax and unnormalized bind sources",
        [
            """services:
  main:
    image: alpine
    volumes:
      - "data:/named"
      - "./files:/plain"
      - "./files/../files:/same-source-unnormalized:ro"
      - "files_sibling:/not-a-path"
volumes:
  data: {}
  files_sibling: {}
"""
        ],
        {},
        {"files/kept": "", "files_sibling/kept": ""},
    ),
    (
        "repeated mount target",
        [
            """services:
  main:
    image: alpine
    volumes:
      - {type: volume, source: first, target: /x}
      - {type: volume, source: middle, target: /y}
      - {type: volume, source: last, target: /x}
volumes:
  first: {}
  middle: {}
  last: {}
"""
        ],
        {},
        {},
    ),
    (
        "command and entrypoint reset to null",
        [
            """services:
  main:
    image: alpine
    command: ["base"]
    entrypoint: ["base-entrypoint"]
""",
            """services:
  main:
    command:
    entrypoint:
""",
        ],
        {},
        {},
    ),
    (
        "a key written with no value",
        ["services:\n  main:\n    image: alpine\n    volumes:\n"],
        {},
        {},
    ),
    (
        "a reference nothing provides",
        ["services:\n  main:\n    image: alpine:${NOWHERE}\n"],
        {},
        {},
    ),
    (
        "short form environment and depends_on",
        [
            """services:
  main:
    image: alpine
    environment:
      - PAIR=value
      - EMPTY=
    depends_on: [peer]
  peer:
    image: alpine
    command: sh -c "echo 'quoted word'"
"""
        ],
        {},
        {},
    ),
]


@pytest.mark.parametrize(
    "name,files,variables,extra", WRITTEN, ids=lambda value: value if isinstance(value, str) else ""
)
def test_written_case_reads_the_same_both_ways(
    tmp_path: Path, name: str, files: list[str], variables: dict[str, str], extra: dict[str, str]
) -> None:
    for relative, text in extra.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    written = []
    for index, text in enumerate(files):
        path = tmp_path / f"compose-{index}.yaml"
        path.write_text(text)
        written.append(path)
    compare(
        name,
        tmp_path,
        files=written,
        environment=variables,
        main_image="alpine@sha256:" + "0" * 64,
        main_build=False,
    )


#: Where the dialect is deliberately stricter than Compose, which Compose accepts and flotilla
#: refuses. Each is a case Compose resolves by substituting something the author did not write,
#: so the task that reaches a sandbox is not the one on disk. The corpus contains none of them.
STRICTER: list[tuple[str, str, dict[str, str]]] = [
    (
        "bare $VAR",
        "services:\n  main:\n    image: alpine\n    environment:\n      VALUE: $GIVEN\n",
        {},
    ),
    (
        "a $ in an env file value, which Compose interpolates in turn",
        "services:\n  main:\n    image: alpine\n    env_file: ref.env\n",
        {"ref.env": "VALUE=${GIVEN}\n"},
    ),
    (
        "a $ in a single-quoted env file value, which Compose keeps literal",
        "services:\n  main:\n    image: alpine\n    env_file: quoted.env\n",
        {"quoted.env": "VALUE='${GIVEN}'\n"},
    ),
]


@pytest.mark.parametrize("name,text,extra", STRICTER, ids=lambda value: value if isinstance(value, str) else "")
def test_the_dialect_refuses_what_compose_would_resolve_silently(
    tmp_path: Path, name: str, text: str, extra: dict[str, str]
) -> None:
    assert BINARY is not None
    for relative, content in extra.items():
        (tmp_path / relative).write_text(content)
    compose = tmp_path / "compose.yaml"
    compose.write_text(text)
    arguments: dict[str, Any] = {
        "files": [compose],
        "environment": {"GIVEN": "value"},
        "main_image": "alpine@sha256:" + "0" * 64,
        "main_build": False,
    }
    # Compose reads it, so this is a choice, not a limitation. If Compose ever starts refusing
    # one of these, the stricter rule has become redundant and the case belongs above.
    ComposeConfigAdapter(command=(BINARY,)).load(tmp_path, **arguments)
    with pytest.raises(ValueError):
        load_project(tmp_path, **arguments)
