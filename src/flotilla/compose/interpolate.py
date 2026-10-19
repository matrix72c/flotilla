"""Compose variable interpolation without inheriting the host environment."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# `$$` or `${...}`; a `$` followed by a name (bare `$VAR`) is matched separately so it can be rejected.
_TOKEN = re.compile(r"\$\$|\$\{(?P<body>[^}]*)\}|\$(?P<bare>[A-Za-z_])")
_BODY = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<op>:-|-)(?P<default>[^$}]*))?")


class InterpolationError(ValueError):
    """Unsupported interpolation syntax."""


@dataclass
class Interpolated:
    """Result of interpolation: the substituted value and unprovided variables referenced (name -> default or None)."""

    value: object
    unresolved: dict[str, str | None] = field(default_factory=dict)


def interpolate(value: object, variables: Mapping[str, str], *, defer: bool = False) -> Interpolated:
    """Interpolate an arbitrarily nested YAML value: strings are substituted, dicts and lists recursed, others kept.
    Dict keys are not interpolated.

    Unprovided variables use their default if given, otherwise the empty string; both are recorded in `unresolved`.
    """
    unresolved: dict[str, str | None] = {}
    return Interpolated(_walk(value, variables, unresolved, path="$", defer=defer), unresolved)


def _walk(
    value: object, variables: Mapping[str, str], unresolved: dict[str, str | None], path: str, *, defer: bool
) -> object:
    if isinstance(value, str):
        return _string(value, variables, unresolved, path, defer=defer)
    if isinstance(value, dict):
        return {k: _walk(v, variables, unresolved, f"{path}.{k}", defer=defer) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, variables, unresolved, f"{path}[{i}]", defer=defer) for i, v in enumerate(value)]
    return value


def _string(
    text: str, variables: Mapping[str, str], unresolved: dict[str, str | None], path: str, *, defer: bool
) -> str:
    def replace(m: re.Match[str]) -> str:
        if m.group(0) == "$$":
            return "$$" if defer else "$"
        if m.group("bare") is not None:
            raise InterpolationError(f"{path}: bare $VAR is not supported, use ${{VAR}}: {text!r}")
        body = m.group("body")
        parsed = _BODY.fullmatch(body)
        if parsed is None:
            raise InterpolationError(f"{path}: unsupported interpolation syntax ${{{body}}}")
        name, op, default = parsed.group("name"), parsed.group("op"), parsed.group("default")
        if name in variables:
            current = variables[name]
            if op == ":-" and current == "":
                return default or ""
            return current.replace("$", "$$") if defer else current
        # If any reference lacks a default, the variable is required; later defaults cannot lift that.
        if name not in unresolved or op is None:
            unresolved[name] = default if op is not None else None
        return m.group(0) if defer else (default or "")

    return _TOKEN.sub(replace, text)
