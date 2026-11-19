"""The image's own metadata, as the registry publishes it.

This read used to belong to a builder that had the image locally and could create a container
from it. The planner has only the config blob, which is enough for everything a trial needs:
the main process argv, the working directory, and the healthcheck the image declares.

Identity is not resolved here. A named `USER` would have to be looked up in the image's own
`/etc/passwd`, which means pulling the image; `plan_task` refuses such a task instead.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from flotilla.compose.model import Healthcheck, ImageMeta


def parse_config(config: Mapping[str, Any]) -> ImageMeta:
    """`config` section of an image config -> plan fields. Keys use the OCI / Docker capitalized form.

    `User` must already be numeric `uid[:gid]`; an empty value is root.
    """
    uid, gid = _identity(_str(config.get("User")))
    return ImageMeta(
        entrypoint=_argv(config.get("Entrypoint")),
        cmd=_argv(config.get("Cmd")),
        uid=uid,
        gid=gid,
        workdir=_str(config.get("WorkingDir")) or "/",
        env=_env(config.get("Env")),
        healthcheck=_healthcheck(config.get("Healthcheck")),
    )


def _identity(spec: str) -> tuple[int, int]:
    if not spec:
        return 0, 0
    user, _, group = spec.partition(":")
    if not user.isdigit() or (group and not group.isdigit()):
        raise ValueError(f"image identity must be numeric uid[:gid]: {spec!r}")
    return int(user), int(group) if group else 0


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
