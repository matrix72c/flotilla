"""Parse image metadata; named identities use the image passwd and group databases."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from flotilla.build.images import ImageError
from flotilla.compose.model import Healthcheck, ImageMeta

#: Resolver from a `USER` name to (uid, gid) (reads /etc/passwd and /etc/group inside the image).
UserLookup = Callable[[str], tuple[int, int]]
GroupLookup = Callable[[str], int]


def parse_user(
    spec: str, *, lookup: UserLookup | None = None, group_lookup: GroupLookup | None = None
) -> tuple[int, int]:
    """Docker `USER` / Compose `user` -> (uid, gid).

    Forms: `uid`, `uid:gid`, `name`, `name:group`. Without a group, gid is the primary group from the image passwd;
    an unknown numeric UID gets gid 0.
    Named users and groups must be resolved from the image databases.
    """
    user, _, group = spec.partition(":")
    if not user:
        raise ImageError(f"invalid USER: {spec!r}")
    uid, gid = _resolve_user(user, spec, lookup)
    if group:
        if group.isdigit():
            gid = int(group)
        elif group_lookup is not None:
            gid = group_lookup(group)
        else:
            raise ImageError("named group requires the image group database")
    return uid, gid


def _resolve_user(user: str, spec: str, lookup: UserLookup | None) -> tuple[int, int]:
    if user.isdigit():
        value = int(user)
        return value, lookup(user)[1] if lookup is not None else 0
    if lookup is None:
        raise ImageError(f"USER {spec!r} is a name and needs the image /etc/passwd to resolve to numeric uid / gid")
    return lookup(user)


def parse_config(
    config: Mapping[str, Any], *, lookup: UserLookup | None = None, group_lookup: GroupLookup | None = None
) -> ImageMeta:
    """`config` section of the image config -> manifest fields. Keys use the OCI / Docker capitalized form."""
    user = _str(config.get("User"))
    uid, gid = parse_user(user, lookup=lookup, group_lookup=group_lookup) if user else (0, 0)
    return ImageMeta(
        entrypoint=_argv(config.get("Entrypoint")),
        cmd=_argv(config.get("Cmd")),
        uid=uid,
        gid=gid,
        workdir=_str(config.get("WorkingDir")) or "/",
        env=_env(config.get("Env")),
        healthcheck=_healthcheck(config.get("Healthcheck")),
    )


def _argv(value: object) -> tuple[str, ...] | None:
    """`Entrypoint` / `Cmd`: a list is argv; missing or null means unset in the image (distinct from empty)."""
    if not isinstance(value, Sequence) or isinstance(value, str):
        return None
    return tuple(str(x) for x in value)


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _env(value: object) -> dict[str, str]:
    """`Env` is a `KEY=VALUE` list; entries without `=` are ignored, as in Docker."""
    out: dict[str, str] = {}
    if isinstance(value, Sequence) and not isinstance(value, str):
        for item in value:
            key, sep, val = str(item).partition("=")
            if sep and key:
                out[key] = val
    return out


def _healthcheck(value: object) -> Healthcheck | None:
    """The image `HEALTHCHECK`: durations are nanoseconds; `Test` starts with `CMD` / `CMD-SHELL` / `NONE`.

    `NONE` (or an unusable `Test`) means the image disables the healthcheck; returns None, same as no HEALTHCHECK.
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
    elif kind not in ("CMD", "CMD-SHELL"):  # older images may give a raw shell string
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
    """Nanoseconds -> seconds; 0 or missing uses Docker's default (`start_period` defaults to 0)."""
    if not isinstance(value, int | float) or value <= 0:
        return default
    return float(value) / 1e9


@dataclass(frozen=True)
class ImageAccounts:
    """Resolve identity against one image's files, never the build host's account database."""

    passwd: str
    groups: str

    def user(self, name: str) -> tuple[int, int]:
        for line in self.passwd.splitlines():
            fields = line.split(":")
            if len(fields) >= 4 and (fields[0] == name or (name.isdigit() and fields[2] == name)):
                try:
                    return int(fields[2]), int(fields[3])
                except ValueError as exc:
                    raise ImageError("invalid numeric identity in image passwd") from exc
        if name.isdigit():
            return int(name), 0
        raise ImageError(f"user {name!r} absent from image passwd")

    def group(self, name: str) -> int:
        if name.isdigit():
            return int(name)
        for line in self.groups.splitlines():
            fields = line.split(":")
            if len(fields) >= 3 and fields[0] == name:
                try:
                    return int(fields[2])
                except ValueError as exc:
                    raise ImageError("invalid numeric identity in image group") from exc
        raise ImageError(f"group {name!r} absent from image group")
