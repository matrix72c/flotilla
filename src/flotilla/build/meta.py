"""镜像元数据：registry 的 image config → `ImageMeta`（Architecture §4.2 第 5 步）。

清单需要每个服务的镜像元数据（`ENTRYPOINT`、`CMD`、`USER`、`WORKDIR`、`ENV`、`HEALTHCHECK`），三种处置
（link / mirror / build）都要。image config 可以只从 registry 读 manifest 与 config blob 得到，不下载镜像层。

`USER` 要解析为数字 uid / gid（§4.2 第 5 步）：`1000`、`1000:1000` 这类纯数字形式在这里直接解析；`root` 按约定为
`0:0`；其余名字要查镜像内的 `/etc/passwd`、`/etc/group`，本模块给不出，由调用方传入 `lookup` 解析，没有 `lookup`
时以 `ImageError` 报出（不猜、不静默当成 0）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from flotilla.build.images import ImageError
from flotilla.compose.model import Healthcheck

#: 镜像里以名字出现、但各发行版都一致的身份。其余名字须经 `lookup` 解析。
WELL_KNOWN_USERS: Mapping[str, tuple[int, int]] = {"root": (0, 0)}

#: `USER` 名字 → (uid, gid) 的解析器（读镜像内的 /etc/passwd、/etc/group）。
UserLookup = Callable[[str], tuple[int, int]]


def parse_user(spec: str, *, lookup: UserLookup | None = None) -> tuple[int, int]:
    """Docker `USER` / Compose `user` → (uid, gid)。

    形式：`uid`、`uid:gid`、`name`、`name:group`。省略 group 时 gid 取 uid（数字形式）或解析结果的 gid。
    数字与 `root` 纯本地解析；其余名字须有 `lookup`。
    """
    user, _, group = spec.partition(":")
    if not user:
        raise ImageError(f"USER 不合法：{spec!r}")
    uid, gid = _resolve_user(user, spec, lookup)
    if group:
        gid = _resolve_group(group, spec, lookup)
    return uid, gid


def _resolve_user(user: str, spec: str, lookup: UserLookup | None) -> tuple[int, int]:
    if user.isdigit():
        value = int(user)
        return value, value  # 只给 uid 时，Docker 的 gid 取同一个数字
    if user in WELL_KNOWN_USERS:
        return WELL_KNOWN_USERS[user]
    if lookup is None:
        raise ImageError(f"USER {spec!r} 是名字，需要镜像内的 /etc/passwd 才能解析为数字 uid / gid")
    return lookup(user)


def _resolve_group(group: str, spec: str, lookup: UserLookup | None) -> int:
    if group.isdigit():
        return int(group)
    if group in WELL_KNOWN_USERS:
        return WELL_KNOWN_USERS[group][1]
    if lookup is None:
        raise ImageError(f"USER {spec!r} 的组是名字，需要镜像内的 /etc/group 才能解析为数字 gid")
    return lookup(group)[1]


@dataclass(frozen=True)
class ImageMetaFields:
    """`parse_config` 的结果。字段与 `flotilla.compose.manifest_gen.ImageMeta` 一一对应。"""

    entrypoint: tuple[str, ...] | None
    cmd: tuple[str, ...] | None
    uid: int
    gid: int
    workdir: str
    env: Mapping[str, str]
    healthcheck: Healthcheck | None


def parse_config(config: Mapping[str, Any], *, lookup: UserLookup | None = None) -> ImageMetaFields:
    """image config 的 `config` 段 → 清单需要的字段。键按 OCI / Docker 的大写形式读取。"""
    user = _str(config.get("User"))
    uid, gid = parse_user(user, lookup=lookup) if user else (0, 0)
    return ImageMetaFields(
        entrypoint=_argv(config.get("Entrypoint")),
        cmd=_argv(config.get("Cmd")),
        uid=uid,
        gid=gid,
        workdir=_str(config.get("WorkingDir")) or "/",
        env=_env(config.get("Env")),
        healthcheck=_healthcheck(config.get("Healthcheck")),
    )


def _argv(value: object) -> tuple[str, ...] | None:
    """`Entrypoint` / `Cmd`：列表为 argv；缺失或 null 表示镜像没有设置（与空列表区分）。"""
    if not isinstance(value, Sequence) or isinstance(value, str):
        return None
    return tuple(str(x) for x in value)


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _env(value: object) -> dict[str, str]:
    """`Env` 是 `KEY=VALUE` 列表；没有 `=` 的条目按 Docker 的做法忽略。"""
    out: dict[str, str] = {}
    if isinstance(value, Sequence) and not isinstance(value, str):
        for item in value:
            key, sep, val = str(item).partition("=")
            if sep and key:
                out[key] = val
    return out


def _healthcheck(value: object) -> Healthcheck | None:
    """镜像的 `HEALTHCHECK`：时长是纳秒；`Test` 的首元素是 `CMD` / `CMD-SHELL` / `NONE`。

    `NONE`（或 `Test` 不可用）表示镜像显式关闭健康检查，返回 None——与"没有 HEALTHCHECK"在清单里一样不设。
    """
    if not isinstance(value, Mapping):
        return None
    test = value.get("Test")
    if not isinstance(test, Sequence) or isinstance(test, str) or not test:
        return None
    kind, *rest = (str(x) for x in test)
    if kind == "NONE":
        return None
    if kind == "CMD-SHELL" and len(rest) == 1:
        argv: tuple[str, ...] = ("/bin/sh", "-c", rest[0])
    elif kind == "CMD" and rest:
        argv = tuple(rest)
    elif kind not in ("CMD", "CMD-SHELL"):  # 旧镜像可能直接给 shell 串
        argv = ("/bin/sh", "-c", " ".join([kind, *rest]))
    else:
        return None
    return Healthcheck(
        test=argv,
        interval_s=_seconds(value.get("Interval"), 30.0),
        timeout_s=_seconds(value.get("Timeout"), 30.0),
        retries=max(1, int(value.get("Retries") or 3)),
        start_period_s=_seconds(value.get("StartPeriod"), 0.0),
        start_interval_s=_seconds(value.get("StartInterval"), 5.0),
    )


def _seconds(value: object, default: float) -> float:
    """纳秒 → 秒；0 或缺失用 Docker 的默认值（`start_period` 的默认就是 0）。"""
    if not isinstance(value, int | float) or value <= 0:
        return default
    return float(value) / 1e9
