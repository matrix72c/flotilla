"""Evaluate declared scalar runtime environment values, never Compose documents."""

from __future__ import annotations

import re
from collections.abc import Mapping

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def evaluate(text: str, values: Mapping[str, str]) -> tuple[str, set[str]]:
    """Expand prepared scalar references, defaults, alternatives and escaped dollars."""
    missing: set[str] = set()

    def expand(source: str, *, active: bool = True) -> str:
        out: list[str] = []
        index = 0
        while index < len(source):
            if source.startswith("$$", index):
                out.append("$")
                index += 2
            elif source.startswith("${", index):
                start = index + 2
                close, depth = start, 0
                while close < len(source):
                    if source.startswith("$$", close):
                        close += 2
                        continue
                    if source.startswith("${", close):
                        depth += 1
                        close += 2
                        continue
                    if source[close] == "}":
                        if not depth:
                            break
                        depth -= 1
                    close += 1
                if close == len(source):
                    raise ValueError("unterminated runtime value reference")
                body = source[start:close]
                match = _NAME.match(body)
                if match is None:
                    raise ValueError("invalid runtime value reference")
                name = match.group()
                suffix = body[match.end() :]
                operator = next((op for op in (":-", ":+", "-", "+") if suffix.startswith(op)), "")
                if suffix and not operator:
                    raise ValueError("unsupported runtime value operator")
                present = name in values
                nonempty = present and values[name] != ""
                use_arg = (
                    (operator == ":-" and not nonempty)
                    or (operator == "-" and not present)
                    or (operator == ":+" and nonempty)
                    or (operator == "+" and present)
                )
                argument = expand(suffix[len(operator) :], active=active and use_arg)
                if active and not present and not operator:
                    missing.add(name)
                result = argument if use_arg else ("" if operator in ("+", ":+") else values.get(name, ""))
                out.append(result)
                index = close + 1
            else:
                if source[index] == "$" and _NAME.match(source, index + 1):
                    raise ValueError("bare runtime variable requires ${NAME} syntax")
                out.append(source[index])
                index += 1
        return "".join(out)

    return expand(text), missing
