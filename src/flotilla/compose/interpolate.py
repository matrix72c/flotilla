"""Compose 变量插值（Architecture §4.2 第 1 步）。

支持数据中出现的写法：`${VAR}`、`${VAR:-default}`（未设置或为空时取默认）、`${VAR-default}`（未设置时取默认）、
`$$`（字面 `$`）。不支持的写法（`${VAR:?err}`、`${VAR:+alt}`、裸 `$VAR`、嵌套）报 `InterpolationError`。

插值只用调用方给的变量（任务目录内的 `.env` 等），**不读构建机环境**。引用了未提供、且没有默认值的变量时
不报错，而是记下来：`environment` 中这样的变量是运行时参数（§4.2），由调用方决定怎么处理。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

# `$$` 或 `${...}`；其后的 `$` 加名字（裸 `$VAR`）单独识别以便拒绝。
_TOKEN = re.compile(r"\$\$|\$\{(?P<body>[^}]*)\}|\$(?P<bare>[A-Za-z_])")
_BODY = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<op>:-|-)(?P<default>[^$}]*))?")


class InterpolationError(ValueError):
    """插值写法不受支持。"""


@dataclass
class Interpolated:
    """一次插值的结果：替换后的值，以及引用了未提供变量的位置（变量名 → 默认值或 None）。"""

    value: object
    unresolved: dict[str, str | None] = field(default_factory=dict)


def interpolate(value: object, variables: Mapping[str, str]) -> Interpolated:
    """对任意嵌套的 YAML 值做插值：字符串替换，字典与列表递归，其余原样。字典的键不插值。

    未提供的变量：有默认值时用默认值，没有时替换为空串；两种情况都记入 `unresolved`。
    """
    unresolved: dict[str, str | None] = {}
    return Interpolated(_walk(value, variables, unresolved, path="$"), unresolved)


def _walk(value: object, variables: Mapping[str, str], unresolved: dict[str, str | None], path: str) -> object:
    if isinstance(value, str):
        return _string(value, variables, unresolved, path)
    if isinstance(value, dict):
        return {k: _walk(v, variables, unresolved, f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, variables, unresolved, f"{path}[{i}]") for i, v in enumerate(value)]
    return value


def _string(text: str, variables: Mapping[str, str], unresolved: dict[str, str | None], path: str) -> str:
    def replace(m: re.Match[str]) -> str:
        if m.group(0) == "$$":
            return "$"
        if m.group("bare") is not None:
            raise InterpolationError(f"{path}：不支持裸 $VAR 写法，请用 ${{VAR}}：{text!r}")
        body = m.group("body")
        parsed = _BODY.fullmatch(body)
        if parsed is None:
            raise InterpolationError(f"{path}：不支持的插值写法 ${{{body}}}")
        name, op, default = parsed.group("name"), parsed.group("op"), parsed.group("default")
        if name in variables:
            current = variables[name]
            if op == ":-" and current == "":
                return default or ""
            return current
        unresolved[name] = default if op is not None else None
        return default or ""

    return _TOKEN.sub(replace, text)
