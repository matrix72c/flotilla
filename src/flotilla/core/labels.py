"""sandbox 标签的键与取值（Architecture §5.2）。回收、对账与 gc 都按这些标签列出。

键与值按最严格的通行约束生成（Kubernetes 标签规则，§5.2）：值不超过 `max_label_value_len`（C1 要求 ≥ 63），
只含字母、数字、`-`、`_`、`.`，首尾为字母或数字（或为空）。用于列出的值（launch、trial、单元）不能改写——
改了就列不到——不合规时直接拒绝；只用于诊断的值（任务、使用方标签）清洗后截断。
"""

from __future__ import annotations

import hashlib
import re

LAUNCH = "flotilla/launch"
TRIAL = "flotilla/trial"
UNIT = "flotilla/unit"
TASK = "flotilla/task"
ROLE = "flotilla/role"

ROLE_UNIT = "unit"
ROLE_ANCHOR = "anchor"

_VALID = re.compile(r"(?:[A-Za-z0-9](?:[-A-Za-z0-9_.]*[A-Za-z0-9])?)?")
_INVALID_CHARS = re.compile(r"[^-A-Za-z0-9_.]+")


def anchor_labels(launch_id: str) -> dict[str, str]:
    """一个 launch 的锚点的标签；也是列出它的条件。"""
    return {LAUNCH: launch_id, ROLE: ROLE_ANCHOR}


def is_valid_value(value: str, limit: int) -> bool:
    return len(value) <= limit and _VALID.fullmatch(value) is not None


def require_value(key: str, value: str, limit: int) -> str:
    """用于列出的标签值：必须原样合规，否则抛 `ValueError`。"""
    if not is_valid_value(value, limit):
        raise ValueError(f"标签 {key} 的值 {value!r} 不合规（≤{limit} 字符，字母数字与 -_.，首尾为字母数字）")
    return value


def sanitize_value(value: str, limit: int) -> str:
    """只用于诊断的标签值：不合规的字符换成 `-`，去掉首尾非字母数字；超长或被改写时截断并附 8 位哈希，
    使不同的原值仍可区分。"""
    if is_valid_value(value, limit):
        return value
    cleaned = _INVALID_CHARS.sub("-", value).strip("-_.")
    digest = hashlib.sha256(value.encode()).hexdigest()[:8]
    head = cleaned[: max(0, limit - len(digest) - 1)].rstrip("-_.")
    return f"{head}-{digest}" if head else digest
