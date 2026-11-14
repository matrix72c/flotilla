"""Content keys for application builds; the registry tag carrying a key is the cache entry."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from flotilla.build.dockerfile import PROXY_ARGS

#: Bump when the inputs or their meaning change: every earlier cache entry is then ignored.
RULES = "build-cache-v1"


def build_key(
    *,
    dockerfile: str,
    dockerfile_text: str,
    context: Path,
    args: Mapping[str, str],
    bases: Mapping[str, str],
    contexts: Mapping[str, str],
    platform: str = "linux/amd64",
) -> str:
    """Key over everything a build reads: Dockerfile, pinned bases, non-proxy args and the whole context.

    `bases` maps each FROM reference to its pinned `repo@sha256:...`. The context includes files a
    `.dockerignore` would exclude, so a key may change without the image changing, never the reverse.
    File owners are excluded: build-context ownership is not part of COPY results.
    """
    document = {
        "rules": RULES,
        "platform": platform,
        "dockerfile": dockerfile,
        "dockerfile_sha256": hashlib.sha256(dockerfile_text.encode()).hexdigest(),
        "args": {k: v for k, v in sorted(args.items()) if k not in PROXY_ARGS},
        "bases": dict(sorted(bases.items())),
        "contexts": dict(sorted(contexts.items())),
        "files": _tree(context),
    }
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cache_tag(key: str) -> str:
    return f"bk-{key[:40]}"


def _tree(root: Path) -> list[list[str]]:
    entries: list[list[str]] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        base = Path(current)
        for name in sorted([*dirs, *files]):
            path = base / name
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            mode = f"{stat.S_IMODE(info.st_mode):o}"
            if stat.S_ISLNK(info.st_mode):
                entries.append([relative, "link", mode, os.readlink(path)])
            elif stat.S_ISDIR(info.st_mode):
                entries.append([relative, "dir", mode])
            elif stat.S_ISREG(info.st_mode):
                entries.append([relative, "file", mode, _sha256(path)])
            else:
                entries.append([relative, "special", mode])
    return entries


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
