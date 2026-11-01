"""Compose variable interpolation without inheriting the host environment."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Longest first: `:-` must not be read as a name followed by `-`.
_OPERATORS = (":-", ":+", "-", "+")


class InterpolationError(ValueError):
    """Unsupported interpolation syntax."""


@dataclass
class Interpolated:
    """Result of interpolation: the substituted value and unprovided variables referenced (name -> default or None)."""

    value: object
    unresolved: dict[str, str | None] = field(default_factory=dict)
    references: set[str] = field(default_factory=set)


def interpolate(value: object, variables: Mapping[str, str]) -> Interpolated:
    """Interpolate an arbitrarily nested YAML value: strings are substituted, dicts and lists recursed, others kept.
    Dict keys are not interpolated.

    Unprovided variables use their default if given, otherwise the empty string; both are recorded in `unresolved`.
    """
    unresolved: dict[str, str | None] = {}
    references: set[str] = set()
    return Interpolated(_walk(value, variables, unresolved, references, path="$"), unresolved, references)


def _walk(
    value: object, variables: Mapping[str, str], unresolved: dict[str, str | None], references: set[str], path: str
) -> object:
    if isinstance(value, str):
        return _string(value, variables, unresolved, references, path)
    if isinstance(value, dict):
        return {k: _walk(v, variables, unresolved, references, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, variables, unresolved, references, f"{path}[{i}]") for i, v in enumerate(value)]
    return value


def _string(
    text: str, variables: Mapping[str, str], unresolved: dict[str, str | None], references: set[str], path: str
) -> str:
    """Substitute `$$`, `${VAR}`, `${VAR:-default}`, `${VAR-default}`, `${VAR:+alt}` and `${VAR+alt}`.

    Defaults and alternatives may themselves contain references (`${NO_PROXY:+${NO_PROXY},}`), as in Compose.
    An alternative is evaluated only when it is used, so variables inside an unused one are not recorded.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("$$", i):
            out.append("$")
            i += 2
        elif text.startswith("${", i):
            value, i = _reference(text, i, variables, unresolved, references, path)
            out.append(value)
        elif text[i] == "$" and _NAME.match(text, i + 1):
            raise InterpolationError(f"{path}: bare $VAR is not supported, use ${{VAR}}: {text!r}")
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _reference(
    text: str,
    start: int,
    variables: Mapping[str, str],
    unresolved: dict[str, str | None],
    references: set[str],
    path: str,
) -> tuple[str, int]:
    """Substitute the `${...}` starting at `start`; returns the value and the index after its closing brace."""
    close = _closing(text, start + 2, path)
    body = text[start + 2 : close]
    name = _NAME.match(body)
    op = next((o for o in _OPERATORS if name and body.startswith(o, name.end())), None)
    if name is None or (op is None and name.end() != len(body)):
        raise InterpolationError(f"{path}: unsupported interpolation syntax ${{{body}}}")
    var = name.group()
    references.add(var)
    arg = body[name.end() + len(op) :] if op else ""

    value: str | None  # None: the argument's value
    if var in variables:
        current = variables[var]
        if op in (":+", "+"):
            used = op == "+" or current != ""
            value = None if used else ""
        elif op == ":-" and current == "":
            used, value = True, None
        else:
            used, value = False, current
    else:
        # Unprovided: `-` forms fall back to their default, `+` forms and bare references to the empty string.
        used = op in (":-", "-")
        value = None if used else ""
    # An unused argument is still checked for syntax; its missing variables do not become required.
    argument = _string(arg, variables, unresolved if used else {}, references, path)
    result = argument if value is None else value
    # Only a bare reference makes the variable required; if any reference lacks a default, later defaults
    # cannot lift that.
    if var not in variables and (var not in unresolved or op is None):
        unresolved[var] = result if op is not None else None
    return result, close + 1


def _closing(text: str, i: int, path: str) -> int:
    """Index of the `}` closing a reference whose body starts at `i`, skipping nested references and `$$`."""
    depth = 0
    while i < len(text):
        if text.startswith("$$", i):
            i += 2
            continue
        if text.startswith("${", i):
            depth += 1
            i += 2
            continue
        if text[i] == "}":
            if depth == 0:
                return i
            depth -= 1
        i += 1
    raise InterpolationError(f"{path}: unterminated interpolation in {text!r}")
