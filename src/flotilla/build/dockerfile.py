"""Decide whether a Dockerfile only changes image configuration, and synthesize that configuration.

A config-only Dockerfile (one stage; FROM, ARG, ENV, WORKDIR, USER, CMD, ENTRYPOINT and
metadata-only instructions) produces an image whose filesystem is its base image's. Preparation
reuses the base digest and applies the synthesized configuration to the task manifest instead
of building. Anything this module cannot reproduce exactly returns a reason and is built.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: Instructions that change only image configuration the manifest captures, or nothing it reads.
_CONFIG = frozenset({"ARG", "ENV", "WORKDIR", "USER", "CMD", "ENTRYPOINT"})
_METADATA = frozenset({"LABEL", "EXPOSE", "MAINTAINER", "STOPSIGNAL"})
#: Proxy build arguments: their build-time values are host settings, never task content.
PROXY_ARGS = frozenset(
    name
    for base in ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "NO_PROXY", "ALL_PROXY")
    for name in (base, base.lower())
)
_DIRECTIVE = re.compile(r"#\s*([A-Za-z]+)\s*=\s*(.*)")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Unfoldable(ValueError):
    """The Dockerfile needs a real build."""


@dataclass(frozen=True)
class Instruction:
    keyword: str  # upper case
    value: str  # everything after the keyword, continuation lines joined


@dataclass(frozen=True)
class Dockerfile:
    base: str  # the single FROM reference
    instructions: tuple[Instruction, ...]  # after FROM


def instructions(text: str) -> list[Instruction]:
    """Logical instructions with parser directives, comments and line continuations handled."""
    escape = "\\"
    lines = text.splitlines()
    start = 0
    for start, line in enumerate(lines):  # noqa: B007 - `start` is the first non-directive line
        match = _DIRECTIVE.fullmatch(line.strip())
        if not match:
            break
        if match.group(1).lower() == "escape" and match.group(2).strip() != escape:
            raise Unfoldable("only the default escape character is folded")
    else:
        start = len(lines)
    out: list[Instruction] = []
    current: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if current and stripped.startswith("#"):
            continue  # comments inside a continued instruction are dropped
        body = line.rstrip()
        if body.endswith(escape):
            current.append(body[: -len(escape)])
            continue
        current.append(body)
        logical = " ".join(part.strip() for part in current if part.strip())
        current = []
        keyword, _, value = logical.partition(" ")
        out.append(Instruction(keyword.upper(), value.strip()))
    if current:
        logical = " ".join(part.strip() for part in current if part.strip())
        keyword, _, value = logical.partition(" ")
        out.append(Instruction(keyword.upper(), value.strip()))
    return out


def parse(text: str) -> Dockerfile:
    """Parse a candidate config-only Dockerfile; raise `Unfoldable` otherwise."""
    items = instructions(text)
    froms = [i for i, item in enumerate(items) if item.keyword == "FROM"]
    if len(froms) != 1:
        raise Unfoldable("not a single-stage Dockerfile")
    if any(item.keyword != "ARG" for item in items[: froms[0]]):
        raise Unfoldable("instructions before FROM")
    words = items[froms[0]].value.split()
    if not words or words[0].startswith("--"):
        raise Unfoldable("FROM flags are not folded")
    if len(words) not in (1, 3) or (len(words) == 3 and words[1].lower() != "as"):
        raise Unfoldable("malformed FROM")
    if "$" in words[0] or words[0].lower() == "scratch":
        raise Unfoldable("FROM must name a concrete image")
    body = items[froms[0] + 1 :]
    for item in body:
        if item.keyword not in _CONFIG | _METADATA:
            raise Unfoldable(f"{item.keyword} changes the filesystem or is not folded")
    return Dockerfile(words[0], tuple(body))


def config_only(text: str) -> bool:
    """Static check used by selection; folding can still fall back after inspecting the base image."""
    try:
        parse(text)
    except Unfoldable:
        return False
    return True


def fold(text: str, base: Mapping[str, Any], args: Mapping[str, str]) -> dict[str, Any]:
    """The image `config` a build of `text` on a base with configuration `base` would produce.

    `args` are the service's build arguments. Raises `Unfoldable` whenever the result depends
    on something not reproduced here: base ONBUILD triggers, a custom base shell for shell-form
    commands, new working directories (a build creates them), undefined or proxy variables, or
    unsupported substitution forms.
    """
    dockerfile = parse(text)
    if base.get("OnBuild"):
        raise Unfoldable("base image has ONBUILD triggers")
    config = dict(base)
    env = _env_list(base.get("Env"))
    build_args: dict[str, str | None] = {}
    base_workdir = posixpath.normpath(base.get("WorkingDir") or "/")
    workdir = base_workdir
    cmd_set = False
    for item in dockerfile.instructions:
        variables: dict[str, str | None] = {**build_args, **dict(env)}
        if item.keyword == "ARG":
            for name, default in _arg(item.value):
                if name in PROXY_ARGS:
                    build_args[name] = None  # host-specific: any reference requires a build
                elif name in args:
                    build_args[name] = args[name]
                elif default is not None:
                    build_args[name] = _word(default, variables)
                else:
                    build_args[name] = None
        elif item.keyword == "ENV":
            # Every value of one ENV instruction sees the variables from before it.
            for name, raw in _env_pairs(item.value):
                env = _set(env, name, _word(raw, variables))
        elif item.keyword == "WORKDIR":
            path = _word(item.value, variables)
            workdir = posixpath.normpath(posixpath.join(workdir, path))
            if workdir not in (base_workdir, "/"):
                raise Unfoldable(f"WORKDIR {workdir} may not exist in the base image")
            config["WorkingDir"] = workdir
        elif item.keyword == "USER":
            config["User"] = _word(item.value, variables)
        elif item.keyword == "CMD":
            config["Cmd"] = _command(item.value, base)
            cmd_set = True
        elif item.keyword == "ENTRYPOINT":
            config["Entrypoint"] = _command(item.value, base)
            if not cmd_set:
                config["Cmd"] = None  # an ENTRYPOINT resets the inherited CMD
    config["Env"] = [f"{k}={v}" for k, v in env]
    return config


def _env_list(value: object) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(value, Sequence) and not isinstance(value, str):
        for item in value:
            key, sep, val = str(item).partition("=")
            if sep and key:
                out = _set(out, key, val)
    return out


def _set(env: list[tuple[str, str]], name: str, value: str) -> list[tuple[str, str]]:
    """Docker keeps an existing variable's position and appends new ones."""
    if any(k == name for k, _ in env):
        return [(k, value if k == name else v) for k, v in env]
    return [*env, (name, value)]


def _arg(value: str) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    for token in _tokens(value):
        name, sep, default = token.partition("=")
        if not _NAME.fullmatch(name):
            raise Unfoldable(f"unsupported ARG {token!r}")
        out.append((name, default if sep else None))
    return out


def _env_pairs(value: str) -> list[tuple[str, str]]:
    tokens = _tokens(value)
    if tokens and "=" not in tokens[0]:
        name, _, rest = value.partition(" ")  # legacy `ENV name value`: the rest of the line is the value
        if not _NAME.fullmatch(name) or not rest.strip():
            raise Unfoldable(f"unsupported ENV {value!r}")
        return [(name, rest.strip())]
    pairs = []
    for token in tokens:
        name, sep, raw = token.partition("=")
        if not sep or not _NAME.fullmatch(name):
            raise Unfoldable(f"unsupported ENV {value!r}")
        pairs.append((name, raw))
    return pairs


def _tokens(value: str) -> list[str]:
    """Split on unquoted whitespace, keeping quotes and escapes for `_word`."""
    tokens: list[str] = []
    current: list[str] = []
    quote = ""
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and quote != "'" and index + 1 < len(value):
            current.append(value[index : index + 2])
            index += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
            index += 1
            continue
        current.append(char)
        index += 1
    if quote:
        raise Unfoldable(f"unterminated quote in {value!r}")
    if current:
        tokens.append("".join(current))
    return tokens


def _word(raw: str, variables: Mapping[str, str | None]) -> str:
    """Docker word processing: quotes, backslash escapes and `$VAR`, `${VAR}`, `${VAR:-x}`, `${VAR:+x}`."""
    out: list[str] = []
    quote = ""
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote == "'":
            if char == "'":
                quote = ""
            else:
                out.append(char)
            index += 1
        elif char == "\\" and index + 1 < len(raw):
            following = raw[index + 1]
            if quote == '"' and following not in '"\\$':
                out.append(char)
            else:
                out.append(following)
                index += 1
            index += 1
        elif char in "'\"" and not quote:
            quote = char
            index += 1
        elif char == '"' and quote == '"':
            quote = ""
            index += 1
        elif char == "$":
            value, index = _expand(raw, index, variables)
            out.append(value)
        else:
            out.append(char)
            index += 1
    if quote:
        raise Unfoldable(f"unterminated quote in {raw!r}")
    return "".join(out)


def _expand(raw: str, index: int, variables: Mapping[str, str | None]) -> tuple[str, int]:
    rest = raw[index + 1 :]
    if rest.startswith("{"):
        end = rest.find("}")
        if end == -1:
            raise Unfoldable(f"unterminated substitution in {raw!r}")
        expression = rest[1:end]
        consumed = index + 2 + end
        name, op, word = expression, "", ""
        for candidate in (":-", ":+"):
            if candidate in expression:
                name, _, word = expression.partition(candidate)
                op = candidate
                break
        if not _NAME.fullmatch(name) or "$" in word or "{" in word:
            raise Unfoldable(f"unsupported substitution ${{{expression}}}")
        value = _lookup(name, variables, required=op == "")
        if op == ":-":
            return (value if value else word), consumed
        if op == ":+":
            return (word if value else ""), consumed
        return value or "", consumed
    match = _NAME.match(rest)
    if not match:
        return "$", index + 1
    return _lookup(match.group(0), variables, required=True) or "", index + 1 + match.end()


def _lookup(name: str, variables: Mapping[str, str | None], *, required: bool) -> str | None:
    if name not in variables:
        if required:
            raise Unfoldable(f"variable {name} is not defined by the Dockerfile or base image")
        return None
    value = variables[name]
    if value is None:
        raise Unfoldable(f"variable {name} has a build-host or undefined value")
    return value


def _command(value: str, base: Mapping[str, Any]) -> list[str]:
    try:
        parsed = json.loads(value)
    except ValueError:
        parsed = None
    if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
        return parsed
    shell = base.get("Shell")
    if shell and list(shell) != ["/bin/sh", "-c"]:
        raise Unfoldable("shell-form command on a base image with a custom SHELL")
    return ["/bin/sh", "-c", value]
