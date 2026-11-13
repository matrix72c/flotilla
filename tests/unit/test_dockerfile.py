"""Config-only Dockerfile detection and Docker-equivalent configuration synthesis."""

from __future__ import annotations

from typing import Any

import pytest

from flotilla.build.dockerfile import Unfoldable, config_only, fold, instructions

BASE: dict[str, Any] = {
    "Env": ["PATH=/usr/bin", "LANG=C"],
    "WorkingDir": "/app",
    "Cmd": ["bash"],
    "User": "",
}


def test_from_and_noop_workdir_reuse_base_config() -> None:
    text = "# canary GUID 1234\nFROM r.io/kali:2.1.0\n\n\nWORKDIR /app\n"
    assert config_only(text)
    assert fold(text, BASE, {}) == {**BASE, "WorkingDir": "/app"}


def test_env_keeps_position_and_sees_previous_instructions_only() -> None:
    text = 'FROM b\nENV LANG=en_US A=1\nENV B="$A two" C=${LANG}\nENV A=2 D=$A\n'
    config = fold(text, BASE, {})
    assert config["Env"] == ["PATH=/usr/bin", "LANG=en_US", "A=2", "B=1 two", "C=en_US", "D=1"]


def test_legacy_env_quotes_and_escapes() -> None:
    text = 'FROM b\nENV GREETING hello  world\nENV Q=\'$A\' R="a\\"b" S=a\\ b\n'
    env = fold(text, BASE, {})["Env"]
    assert env[2:] == ["GREETING=hello  world", "Q=$A", 'R=a"b', "S=a b"]


def test_args_build_values_defaults_and_substitution_forms() -> None:
    text = "FROM b\nARG X\nARG Y=dflt\nARG E=\nENV A=$X B=${Y} C=${E:-fallback} D=${Y:+alt} F=${MISSING:-m}\n"
    env = fold(text, BASE, {"X": "given"})["Env"]
    assert env[2:] == ["A=given", "B=dflt", "C=fallback", "D=alt", "F=m"]


def test_workdir_user_and_commands() -> None:
    text = 'FROM b\nARG U=app\nWORKDIR /\nWORKDIR app\nUSER ${U}:staff\nCMD ["run", "x"]\nENTRYPOINT /entry --flag\n'
    config = fold(text, BASE, {})
    assert config["WorkingDir"] == "/app" and config["User"] == "app:staff"
    # CMD set in this Dockerfile survives a later ENTRYPOINT; shell form uses /bin/sh -c.
    assert config["Cmd"] == ["run", "x"] and config["Entrypoint"] == ["/bin/sh", "-c", "/entry --flag"]


def test_entrypoint_resets_inherited_cmd() -> None:
    assert fold('FROM b\nENTRYPOINT ["/e"]\n', BASE, {})["Cmd"] is None


def test_continuations_and_comments() -> None:
    text = "FROM b\nENV A=1 \\\n# note\n    B=2\nLABEL x=$UNDEFINED\nEXPOSE 80\n"
    assert fold(text, BASE, {})["Env"][2:] == ["A=1", "B=2"]


@pytest.mark.parametrize(
    "text",
    [
        "FROM b\nRUN true\n",
        "FROM b\nCOPY x /x\n",
        "FROM a AS one\nFROM b\n",
        "FROM $BASE\n",
        "FROM --platform=linux/amd64 b\n",
        "FROM b\nHEALTHCHECK CMD true\n",
        'FROM b\nSHELL ["/bin/bash", "-c"]\n',
        "FROM b\nVOLUME /data\n",
        "FROM b\nONBUILD RUN true\n",
        "# escape=`\nFROM b\n",
    ],
)
def test_filesystem_and_unmodelled_instructions_are_not_config_only(text: str) -> None:
    assert not config_only(text)
    with pytest.raises(Unfoldable):
        fold(text, BASE, {})


@pytest.mark.parametrize(
    ("text", "base"),
    [
        ("FROM b\nWORKDIR /new\n", BASE),  # a build would create the directory
        ("FROM b\nENV A=$UNDEFINED\n", BASE),
        ("FROM b\nARG HTTP_PROXY\nENV P=$HTTP_PROXY\n", BASE),  # host-specific value
        ("FROM b\nENV HTTPS_PROXY=${HTTPS_PROXY}\n", BASE),
        ("FROM b\nARG X\nENV A=$X\n", BASE),  # no value, no default
        ("FROM b\nENV A=${B#x}\n", BASE),
        ("FROM b\n", {**BASE, "OnBuild": ["RUN true"]}),
        ("FROM b\nCMD run\n", {**BASE, "Shell": ["/bin/bash", "-c"]}),
    ],
)
def test_results_depending_on_a_build_are_unfoldable(text: str, base: dict[str, Any]) -> None:
    assert config_only(text)
    with pytest.raises(Unfoldable):
        fold(text, base, {})


def test_instructions_join_continuations_and_skip_directives() -> None:
    parsed = instructions("# syntax=docker/dockerfile:1\nfrom b\nrun a \\\n  b\n")
    assert [(i.keyword, i.value) for i in parsed] == [("FROM", "b"), ("RUN", "a b")]
