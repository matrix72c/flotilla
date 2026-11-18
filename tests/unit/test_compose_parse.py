"""The dialect parser's own rules, with no Compose executable.

The claim that this parser agrees with Docker is made by
`tests/contract/compose_differential.py`, which needs the pinned executable and a task corpus.
What is left for here is each rule stated once, so a change to one is visible in CI without
either: which files it will open, what it refuses, and the three Compose behaviors that are easy
to get wrong (a key written with no value, a repeated mount target, unnormalized bind sources).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flotilla.compose.model import MAIN
from flotilla.compose.normalize import KEEPALIVE
from flotilla.compose.parse import ComposeError, load_project

IMAGE = "alpine@sha256:" + "0" * 64


def load(root: Path, *texts: str, variables: dict[str, str] | None = None, main_image: str | None = IMAGE):  # type: ignore[no-untyped-def]
    files = []
    for index, text in enumerate(texts):
        path = root / f"compose-{index}.yaml"
        path.write_text(text)
        files.append(path)
    return load_project(root, files=files, environment=variables or {}, main_image=main_image, main_build=False)


def test_the_main_service_is_synthesized_and_kept_alive(tmp_path: Path) -> None:
    project = load(tmp_path)
    assert not project.rejected, project.findings
    assert project.services[MAIN].image == IMAGE
    assert project.services[MAIN].command == KEEPALIVE


def test_a_task_file_overrides_the_keepalive_command(tmp_path: Path) -> None:
    project = load(tmp_path, f'services:\n  {MAIN}:\n    command: ["own"]\n')
    assert project.services[MAIN].command == ("own",)


def test_harbor_image_and_workdir_supersede_the_task_file(tmp_path: Path) -> None:
    compose = tmp_path / "compose-0.yaml"
    compose.write_text(f"services:\n  {MAIN}:\n    image: ignored\n    working_dir: /ignored\n")
    project = load_project(
        tmp_path, files=[compose], environment={}, main_image=IMAGE, main_build=False, workdir="/wins"
    )
    assert project.services[MAIN].image == IMAGE
    assert project.services[MAIN].working_dir == "/wins"


def test_build_is_declared_so_it_can_be_rejected_by_name(tmp_path: Path) -> None:
    """Without `docker_image`, a `Dockerfile` becomes `build:`, which `normalize()` names."""
    project = load_project(tmp_path, files=[], environment={}, main_image=None, main_build=True)
    assert f"services.{MAIN}.build" in [f.path for f in project.findings if f.kind == "reject"]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("${GIVEN}", "value"),
        ("pre-${GIVEN}", "pre-value"),
        ("${ABSENT:-default}", "default"),
        ("${ABSENT-default}", "default"),
        ("${BLANK:-fallback}", "fallback"),
        ("${BLANK-kept}", ""),
        ("${GIVEN:+alternate}", "alternate"),
        ("${BLANK:+unused}", ""),
        ("${BLANK+set}", "set"),
        ("${ABSENT:-${GIVEN}}", "value"),
        ("$$LITERAL", "$LITERAL"),
    ],
)
def test_interpolation_forms(tmp_path: Path, value: str, expected: str) -> None:
    project = load(
        tmp_path,
        f"services:\n  {MAIN}:\n    environment:\n      RESULT: '{value}'\n",
        variables={"GIVEN": "value", "BLANK": ""},
    )
    assert project.services[MAIN].environment["RESULT"] == expected


def test_a_reference_with_no_value_and_no_default_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ComposeError, match="NOWHERE"):
        load(tmp_path, f"services:\n  {MAIN}:\n    image: app:${{NOWHERE}}\n")


def test_a_bare_reference_is_refused(tmp_path: Path) -> None:
    """Compose substitutes it; requiring the braces keeps a shell-looking value literal."""
    with pytest.raises(ComposeError, match="bare"):
        load(tmp_path, f"services:\n  {MAIN}:\n    working_dir: $GIVEN\n", variables={"GIVEN": "value"})


def test_caller_values_override_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("FROM_DOTENV=dotenv\nOVERRIDDEN=dotenv-loses\n")
    project = load(
        tmp_path,
        f"services:\n  {MAIN}:\n    environment:\n      A: ${{FROM_DOTENV}}\n      B: ${{OVERRIDDEN}}\n",
        variables={"OVERRIDDEN": "caller"},
    )
    assert project.services[MAIN].environment == {"A": "dotenv", "B": "caller"}


def test_env_file_values_sit_under_the_declared_environment(tmp_path: Path) -> None:
    (tmp_path / "base.env").write_text(
        "# comment\nFROM_FILE=file\nOVERRIDDEN=file-loses\nQUOTED='two  spaces'\n"
        "TRAILING=value # dropped\nexport EXPORTED=exported\n"
    )
    (tmp_path / "later.env").write_text("FROM_FILE=later-wins\n")
    project = load(
        tmp_path,
        f"""services:
  {MAIN}:
    env_file: [base.env, later.env]
    environment:
      OVERRIDDEN: compose
""",
    )
    assert project.services[MAIN].environment == {
        "FROM_FILE": "later-wins",
        "OVERRIDDEN": "compose",
        "QUOTED": "two  spaces",
        "TRAILING": "value",
        "EXPORTED": "exported",
    }


def test_an_optional_env_file_may_be_absent_but_a_required_one_may_not(tmp_path: Path) -> None:
    project = load(
        tmp_path,
        f"services:\n  {MAIN}:\n    env_file:\n      - path: gone.env\n        required: false\n",
    )
    assert project.services[MAIN].environment == {}
    with pytest.raises(ComposeError, match="missing file"):
        load(tmp_path, f"services:\n  {MAIN}:\n    env_file: gone.env\n")


def test_a_dollar_in_an_env_file_is_refused(tmp_path: Path) -> None:
    """Compose interpolates these values in turn; the dialect keeps that cascade out."""
    (tmp_path / "ref.env").write_text("VALUE=${GIVEN}\n")
    with pytest.raises(ComposeError, match=r"\$"):
        load(tmp_path, f"services:\n  {MAIN}:\n    env_file: ref.env\n", variables={"GIVEN": "value"})


@pytest.mark.parametrize("entry", ["../outside.env", "/etc/passwd", "${GIVEN}.env"])
def test_an_env_file_outside_the_task_is_refused(tmp_path: Path, entry: str) -> None:
    with pytest.raises(ComposeError):
        load(
            tmp_path,
            f"services:\n  {MAIN}:\n    env_file: '{entry}'\n",
            variables={"GIVEN": "dynamic"},
        )


def test_a_compose_file_outside_the_task_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.yaml"
    outside.write_text("services: {}\n")
    with pytest.raises(ComposeError, match="escapes"):
        load_project(tmp_path, files=[outside], environment={}, main_image=IMAGE, main_build=False)


def test_a_later_file_merges_over_an_earlier_one(tmp_path: Path) -> None:
    project = load(
        tmp_path,
        f"""services:
  {MAIN}:
    environment:
      KEPT: first
      REPLACED: first
  peer:
    image: {IMAGE}
    working_dir: /first
""",
        f"""services:
  {MAIN}:
    environment:
      REPLACED: second
      ADDED: second
  peer:
    working_dir: /second
""",
    )
    assert project.services[MAIN].environment == {"KEPT": "first", "REPLACED": "second", "ADDED": "second"}
    assert project.services["peer"].working_dir == "/second"


@pytest.mark.parametrize("key", ["volumes", "environment", "networks", "healthcheck", "image"])
def test_a_key_written_with_no_value_is_refused(tmp_path: Path, key: str) -> None:
    """Compose's schema refuses these; reading one as empty would drop what the author declared."""
    with pytest.raises(ComposeError, match="no value"):
        load(tmp_path, f"services:\n  peer:\n    image: {IMAGE}\n    {key}:\n")


def test_a_top_level_key_written_with_no_value_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ComposeError, match="no value"):
        load(tmp_path, "volumes:\n")


@pytest.mark.parametrize("key", ["command", "entrypoint"])
def test_a_null_argv_resets_what_an_earlier_file_set(tmp_path: Path, key: str) -> None:
    project = load(
        tmp_path,
        f'services:\n  {MAIN}:\n    {key}: ["from-first"]\n',
        f"services:\n  {MAIN}:\n    {key}:\n",
    )
    assert getattr(project.services[MAIN], key) is None


def test_a_repeated_mount_target_keeps_the_last_declaration_at_the_first_position(tmp_path: Path) -> None:
    project = load(
        tmp_path,
        f"""services:
  {MAIN}:
    volumes:
      - {{type: volume, source: first, target: /x}}
      - {{type: volume, source: middle, target: /y}}
      - {{type: volume, source: last, target: /x}}
volumes:
  first: {{}}
  middle: {{}}
  last: {{}}
""",
    )
    assert not project.rejected, project.findings
    assert [(m.source, m.target) for m in project.services[MAIN].volumes] == [("last", "/x"), ("middle", "/y")]
    # `first` is now mounted by nobody, so it is not reserved.
    assert project.volumes == frozenset({"last", "middle"})


def test_two_spellings_of_one_target_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ComposeError, match="already mounted"):
        load(
            tmp_path,
            f"""services:
  {MAIN}:
    volumes:
      - {{type: volume, source: a, target: /x/}}
      - {{type: volume, source: b, target: /x}}
volumes: {{a: {{}}, b: {{}}}}
""",
        )


def test_bind_sources_are_normalized_so_one_directory_is_one_backing_root(tmp_path: Path) -> None:
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "kept").write_text("")
    project = load(
        tmp_path,
        f"""services:
  {MAIN}:
    volumes:
      - "./files:/plain"
      - {{type: bind, source: files, target: /no-prefix}}
      - {{type: bind, source: ./files/../files, target: /unnormalized}}
""",
    )
    assert not project.rejected, project.findings
    assert {m.source for m in project.services[MAIN].volumes} == {"./files"}


def test_a_bind_source_outside_the_task_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ComposeError, match="outside the task"):
        load(tmp_path, f'services:\n  {MAIN}:\n    volumes:\n      - "../outside:/x"\n')


def test_unparsable_yaml_is_a_task_verdict(tmp_path: Path) -> None:
    with pytest.raises(ComposeError, match="invalid Compose YAML"):
        load(tmp_path, "services: [unclosed\n")


def test_a_compose_file_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ComposeError, match="is a mapping"):
        load(tmp_path, "- a list\n")


def test_an_empty_compose_file_leaves_the_main_service_alone(tmp_path: Path) -> None:
    project = load(tmp_path, "")
    assert project.services[MAIN].command == KEEPALIVE


def test_include_and_extends_reach_normalize_to_be_rejected_by_name(tmp_path: Path) -> None:
    """The parser never expands them, and a verdict naming the field beats a parse error."""
    project = load(tmp_path, "include:\n  - other.yaml\n")
    assert [f.path for f in project.findings if f.kind == "reject"] == ["include"]
    project = load(tmp_path, f"services:\n  peer:\n    image: {IMAGE}\n    extends:\n      service: other\n")
    assert "services.peer.extends" in [f.path for f in project.findings if f.kind == "reject"]


def test_context_dir_is_the_environment_directory(tmp_path: Path) -> None:
    project = load(tmp_path, f"services:\n  {MAIN}:\n    environment:\n      WHERE: ${{CONTEXT_DIR}}\n")
    assert project.services[MAIN].environment["WHERE"] == str(tmp_path.resolve())
