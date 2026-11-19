"""Read a task's Compose files into the document `normalize()` consumes.

This is flotilla's Compose front end: YAML load, variable interpolation, `env_file`
expansion, the ordered merge of several files, and Harbor's synthetic `main` service.
flotilla supports a subset of Compose, so it maintains that subset's agreement with
Docker itself rather than shelling out to `docker compose config`.

The boundary that matters is which files get opened: only the ones named in `files`, a
task-local `.env`, and the `env_file` paths a service declares, each confined to the
task's environment directory. `include` and `extends` would name further files, so they
are never acted on; they reach `normalize()`, which rejects them by name.

What this module does not decide is which fields are supported. That stays in
`normalize()`, which turns the document below into a `Project` with one finding per
unsupported field.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from flotilla.compose.interpolate import InterpolationError, interpolate
from flotilla.compose.model import MAIN, Project
from flotilla.compose.normalize import KEEPALIVE, normalize
from flotilla.compose.paths import confined

#: Loading a task's Compose file must not construct arbitrary Python objects; the C loader is
#: used when the build has it. Both resolve `<<` merge keys while loading.
_LOADER: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

DOTENV = ".env"

#: The only service keys Compose lets a task write with no value, reading the null as "unset".
#: Its schema forbids it everywhere else, including every top-level key. The null is kept rather
#: than dropped, because across several files it resets what an earlier file set, and
#: `normalize()` already reads it the same way it reads an absent key.
_NULLABLE = frozenset({"command", "entrypoint", "deploy"})

#: An unquoted `env_file` value ends at an inline comment, which needs whitespace before the `#`.
_INLINE_COMMENT = re.compile(r"\s+#")


class ComposeError(ValueError):
    """The task's Compose input cannot be read: unparsable YAML, an escaping path, an unprovided variable.

    A verdict on the task's input, the same class of problem as the findings `normalize()`
    reports; the difference is only that these stop the read before a project exists.
    """


def load_project(
    project_dir: Path,
    *,
    files: Sequence[Path],
    environment: Mapping[str, str],
    main_image: str | None,
    main_build: bool,
    workdir: str | None = None,
) -> Project:
    """Read `files` into a `Project`, merged in order over Harbor's `main` service.

    `environment` holds the caller's interpolation values. They override a task-local `.env`,
    as the process environment overrides it in Compose, and the ambient process environment is
    never consulted: a task resolves to the same project on any host.
    """
    root = project_dir.resolve()
    selected = [_input_path(root, path) for path in files]
    variables = {**_dotenv(root), **environment, "CONTEXT_DIR": str(root)}

    document: dict[str, Any] = {"services": {MAIN: _main_service(main_image, main_build)}}
    for path in selected:
        document = _merge(document, _read(root, path, variables))

    main = document["services"].get(MAIN)
    if not isinstance(main, dict):
        raise ComposeError(f"Compose input redefines the {MAIN} service as something other than a service")
    # Harbor's explicit prebuilt image supersedes the task's build; these are semantic Harbor
    # overrides, not a second generic merge.
    if main_image is not None:
        main["image"] = main_image
        main.pop("build", None)
    if workdir is not None:
        main["working_dir"] = workdir
    _dedupe_mounts(document, "the Compose input")
    _relative_paths(document, root)
    return normalize(document)


def _main_service(main_image: str | None, main_build: bool) -> dict[str, Any]:
    """Harbor's agent service: kept alive, so the agent's own commands decide when the trial ends."""
    base: dict[str, Any] = {"command": list(KEEPALIVE)}
    if main_image is not None:
        base["image"] = main_image
    elif main_build:
        # Unsupported, but the field has to be present for `normalize()` to reject it by name
        # instead of reporting the vaguer "no image".
        base["build"] = {"context": "."}
    return base


def _read(root: Path, path: Path, variables: Mapping[str, str]) -> dict[str, Any]:
    """One Compose file: load, interpolate, expand `env_file`."""
    try:
        loaded = yaml.load(path.read_bytes(), Loader=_LOADER)
    except yaml.YAMLError as exc:
        raise ComposeError(f"invalid Compose YAML in {path.name}: {exc}") from exc
    except OSError as exc:
        raise ComposeError(f"cannot read Compose file {path.name}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ComposeError(f"{path.name}: a Compose file is a mapping, not {type(loaded).__name__}")
    _reject_nulls(loaded, path.name)
    document = _interpolate(loaded, variables, path.name)
    _expand_env_files(document, root, path.name)
    return document


def _reject_nulls(document: Mapping[str, Any], name: str) -> None:
    """Refuse a key written with no value, except the few Compose reads as "unset".

    `volumes:` with nothing under it reads as an empty list to a careless parser and as a schema
    violation to Compose, which refuses the project. Accepting it would run a task Docker rejects
    outright, with mounts the author meant to declare silently absent.
    """
    for key, value in document.items():
        if value is None:
            raise ComposeError(f"{name}: {key} has no value; remove the key or give it one")
    services = document.get("services")
    for service, definition in services.items() if isinstance(services, dict) else ():
        if not isinstance(definition, Mapping):
            continue
        for key, value in definition.items():
            if value is None and key not in _NULLABLE:
                raise ComposeError(f"{name}: services.{service}.{key} has no value; remove the key or give it one")


def _input_path(root: Path, path: Path) -> Path:
    """A Compose file must live under the task's environment directory."""
    value = str(path)
    if path.is_absolute():
        try:
            value = str(path.relative_to(root))
        except ValueError as exc:
            raise ComposeError(f"Compose input path escapes the task directory: {value!r}") from exc
    try:
        return confined(root, value)
    except ValueError as exc:
        raise ComposeError(str(exc)) from exc


def _interpolate(document: dict[str, Any], variables: Mapping[str, str], name: str) -> dict[str, Any]:
    """Substitute `${VAR}` throughout, refusing a bare reference nothing provides a value for.

    Compose substitutes a blank string and warns, exiting 0, so `image: app:${TAG}` quietly
    becomes `app:` and the task fails much later with an unrelated-looking error.
    """
    try:
        result = interpolate(document, variables)
    except InterpolationError as exc:
        raise ComposeError(f"{name}: {exc}") from exc
    required = sorted(key for key, default in result.unresolved.items() if default is None)
    if required:
        raise ComposeError(
            f"{name}: references variables the task does not provide and that have no default: {required}"
        )
    assert isinstance(result.value, dict)  # a mapping in, a mapping out
    return result.value


def _expand_env_files(document: dict[str, Any], root: Path, name: str) -> None:
    """Fold each service's `env_file` into its `environment`, which wins over the files.

    Only task-local literal paths, in short (`path`) or long (`{path, required}`) form. The
    files are read in order and a later one overrides an earlier one, as in Compose.
    """
    services = document.get("services")
    if not isinstance(services, dict):
        return
    for service, definition in services.items():
        if not isinstance(definition, dict) or "env_file" not in definition:
            continue
        path = f"{name}: services.{service}.env_file"
        values: dict[str, Any] = {}
        for entry, required in _env_file_entries(definition.pop("env_file"), path):
            try:
                resolved = confined(root, entry)
            except ValueError as exc:
                raise ComposeError(f"{path}: {exc}") from exc
            if not resolved.is_file():
                if required:
                    raise ComposeError(f"{path}: missing file {entry!r}")
                continue
            values.update(read_env_file(resolved))
        # A declared `environment` wins, whichever form it takes; its own syntax errors are
        # `normalize()`'s to report, so an unrecognized shape is left untouched for it to see.
        declared = definition.get("environment")
        if declared is None or isinstance(declared, dict | list):
            values.update(_declared_environment(declared))
            definition["environment"] = values


def _env_file_entries(spec: object, path: str) -> list[tuple[str, bool]]:
    """`env_file` as a list of (task-relative path, required)."""
    items = spec if isinstance(spec, list) else [spec]
    out: list[tuple[str, bool]] = []
    for item in items:
        if isinstance(item, str):
            entry, required = item, True
        elif isinstance(item, dict):
            value = item.get("path")
            if "format" in item:
                raise ComposeError(f"{path}: env_file `format` is unsupported; only the default format is read")
            if not isinstance(value, str) or set(item) - {"path", "required"}:
                raise ComposeError(f"{path}: the long form takes a `path` and an optional `required`")
            entry, required = value, bool(item.get("required", True))
        else:
            raise ComposeError(f"{path}: expected a path or a list of paths")
        if not entry or "$" in entry:
            raise ComposeError(f"{path}: requires a task-local literal path, not {entry!r}")
        out.append((entry, required))
    return out


def _declared_environment(value: object) -> dict[str, Any]:
    """A service's own `environment`, flattened to a mapping so `env_file` values can sit under it.

    A key-only entry keeps `None`, which is what `normalize()` rejects it for: it would
    inherit from the host environment, and no host environment enters a task.
    """
    if isinstance(value, list):
        out: dict[str, Any] = {}
        for item in value:
            key, sep, text = str(item).partition("=")
            out[key] = text if sep else None
        return out
    return dict(value) if isinstance(value, dict) else {}


def _dotenv(root: Path) -> dict[str, str]:
    """The task's own `.env`, which supplies interpolation values only."""
    path = root / DOTENV
    return read_env_file(path) if path.is_file() else {}


def read_env_file(path: Path) -> dict[str, str]:
    """`KEY=VALUE` lines: `#` starts a comment, and a surrounding pair of quotes is stripped.

    A `$` anywhere in a value is refused. Compose interpolates these values too, sequentially,
    against the shell environment, the keys defined above in the same file, the files read before
    it, and the service's own `environment` -- a cascade in which an env file's value can depend
    on what another env file said. Rejecting the character keeps that out of the dialect: a value
    here means what it says, and a task that wants a reference writes it in the Compose file.
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError as exc:
        raise ComposeError(f"cannot read env file {path.name}: {exc}") from exc
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        if not sep or not key:
            raise ComposeError(f"{path.name}:{number}: expected `KEY=VALUE`")
        if "$" in key:
            # Compose refuses the file outright; it does not interpolate a key.
            raise ComposeError(f"{path.name}:{number}: `$` is not allowed in a variable name")
        out[key] = _env_value(value.strip(), path.name, number)
    return out


def _env_value(value: str, name: str, number: int) -> str:
    text = value if value.startswith(("'", '"')) else _INLINE_COMMENT.split(value, maxsplit=1)[0].rstrip()
    if value.startswith(("'", '"')):
        quote, end = value[0], value.find(value[0], 1)
        if end < 0:
            raise ComposeError(f"{name}:{number}: unterminated {quote} in the value")
        tail = value[end + 1 :].lstrip()
        if tail and not tail.startswith("#"):
            raise ComposeError(f"{name}:{number}: trailing text after the closing {quote}")
        text = value[1:end]
        if quote == '"' and "\\" in text:
            raise ComposeError(f"{name}:{number}: escape sequences in a double-quoted value are unsupported")
    if "$" in text:
        raise ComposeError(
            f"{name}:{number}: `$` in a value is unsupported; write the reference in the Compose file instead"
        )
    return text


def _dedupe_mounts(document: Mapping[str, Any], name: str) -> None:
    """One mount per target: a repeated target keeps the last declaration at the first position.

    Compose resolves a repeated target this way, and refuses the project when two targets differ
    as written but name the same directory (`/x/` and `/x`). Without both rules a task that
    declares a target twice gets whichever of the two the layout happens to keep, which decides
    whether a service sees the files it was given or an empty volume.
    """
    services = document.get("services")
    for service, definition in services.items() if isinstance(services, Mapping) else ():
        mounts = definition.get("volumes") if isinstance(definition, Mapping) else None
        if not isinstance(mounts, list):
            continue
        path = f"{name}: services.{service}.volumes"
        kept: dict[str, int] = {}  # target as written -> its position in the result
        canonical: dict[str, str] = {}  # resolved target -> the first spelling that claimed it
        out: list[Any] = []
        for index, mount in enumerate(mounts):
            target = _mount_target(mount)
            if target is None:  # an unreadable entry: `normalize()` reports it with its own path
                out.append(mount)
                continue
            resolved = posixpath.normpath(target)
            if target not in kept and canonical.setdefault(resolved, target) != target:
                raise ComposeError(f"{path}[{index}]: target {target} is already mounted as {canonical[resolved]}")
            if target in kept:
                out[kept[target]] = mount
            else:
                kept[target] = len(out)
                out.append(mount)
        mounts[:] = out


def _mount_target(mount: object) -> str | None:
    """A mount's target as written, in long or short form; None when the entry is unreadable."""
    if isinstance(mount, Mapping):
        target = mount.get("target")
        return target if isinstance(target, str) else None
    if isinstance(mount, str):
        parts = mount.split(":")
        return parts[1] if len(parts) in (2, 3) else None
    return None


def _relative_paths(document: dict[str, Any], root: Path) -> None:
    """Rewrite each bind source and build context as a task-relative path, refusing one that escapes.

    A bind source is what the layout keys attempt-owned storage by, so `conf`, `./conf` and
    `./x/../conf` have to name the same thing. The normalized form carries `./` to keep
    `normalize()` reading it as a path rather than a named volume.
    """
    services = document.get("services")
    if not isinstance(services, dict):
        return
    for service in services.values():
        if not isinstance(service, dict):
            continue
        build = service.get("build")
        if isinstance(build, dict) and isinstance(build.get("context"), str):
            build["context"] = _relative(root, build["context"])
        mounts = service.get("volumes")
        if not isinstance(mounts, list):
            continue
        for index, mount in enumerate(mounts):
            if isinstance(mount, dict) and mount.get("type") == "bind" and isinstance(mount.get("source"), str):
                mount["source"] = "./" + _relative(root, mount["source"])
            elif isinstance(mount, str):
                source, sep, rest = mount.partition(":")
                # Only the short form's bind sources; `normalize()` reads a source with no
                # leading `.`, `/` or `~` as a named volume, and that stays its decision.
                if sep and source.startswith((".", "/", "~")):
                    mounts[index] = "./" + _relative(root, source) + ":" + rest


def _relative(root: Path, value: str) -> str:
    try:
        return str(_input_path(root, Path(value)).relative_to(root))
    except ValueError as exc:
        raise ComposeError(f"path is outside the task directory: {value!r}") from exc


def _merge(base: Any, over: Any) -> Any:
    """Compose's ordered merge: a later file deep-merges over an earlier one, and lists replace.

    Compose appends for `ports`, `expose`, `dns` and a few more. That is not reproduced, because
    flotilla's `main` service is the only base here and declares none of them: a task file either
    replaces the `command` that keeps `main` alive, or leaves it in place.
    """
    if isinstance(base, dict) and isinstance(over, dict):
        return {**base, **{key: _merge(base[key], value) if key in base else value for key, value in over.items()}}
    return over
