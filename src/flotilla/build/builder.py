"""BuildKit requests, static base-image substitutions, and derived images."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from flotilla.build.images import BuildSettings, ImageError

MAX_TAG = 128
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def image_tag(target: str, task: str, service: str, build_key: str) -> str:
    """`<target>:<task-slug>--<service>--<buildkey12>`.

    Tags are at most 128 chars; an overlong `task-slug` is truncated with a content hash so tasks never collide.
    """
    suffix = f"--{_slug(service)}--{build_key[:12]}"
    budget = MAX_TAG - len(suffix)
    if budget < 8:
        raise ImageError(f"service name too long to form a tag: {service!r}")
    return f"{target}:{_slug(task, budget)}{suffix}"


def _slug(text: str, budget: int | None = None) -> str:
    """Normalize to tag-safe characters; truncate with a hash when exceeding `budget`."""
    slug = _UNSAFE.sub("-", text).strip("-._") or "x"
    if budget is None or len(slug) <= budget:
        return slug
    digest = hashlib.sha256(text.encode()).hexdigest()[:8]
    return f"{slug[: budget - 9].rstrip('-._')}-{digest}"


def base_images(dockerfile_text: str) -> list[str]:
    """Base images referenced by `FROM` in a Dockerfile, deduplicated in order of appearance.

    Skips `FROM <stage>` referencing an earlier stage (a name defined by `AS <name>`) and `FROM scratch`.
    `ARG` interpolation is not expanded: references containing `$` are returned as-is for the caller to handle.
    """
    stages: set[str] = set()
    out: list[str] = []
    logical = re.sub(r"\\\r?\n", " ", dockerfile_text)
    for line in logical.splitlines():
        if not re.match(r"(?i)^\s*FROM\s", line):
            continue
        parts = shlex.split(line, comments=True)
        if not parts or parts[0].lower() != "from":
            continue
        index = 1
        while index < len(parts) and parts[index].startswith("--"):
            index += 1
        if index == len(parts):
            raise ImageError("FROM requires an image")
        ref = parts[index]
        if ref.lower() not in stages and ref.lower() != "scratch" and ref not in out:
            out.append(ref)
        if len(parts) > index + 2 and parts[index + 1].lower() == "as":
            stages.add(parts[index + 2].lower())
    return out


def build_contexts(dockerfile_text: str, settings: BuildSettings) -> dict[str, str]:
    """Base image substitutions: original `FROM` reference -> substituted reference.

    The mapping feeds BuildKit's `--build-context <original>=docker-image://<substitute>`; the Dockerfile is unchanged.
    References containing `$` (ARG interpolation) are skipped: they cannot be expanded and are left to `build-arg`.
    """
    out: dict[str, str] = {}
    for ref in base_images(dockerfile_text):
        if "$" in ref:
            continue
        replaced = settings.replace(ref)
        if replaced != ref:
            out[ref] = replaced
    return out


@dataclass(frozen=True)
class BuildRequest:
    """A single image build.

    `context` is the build context directory; for derive it holds only the bind sources to bake in (may be empty).
    Exactly one of `dockerfile` (a path inside the context, for build) or `dockerfile_text` (synthesized, for derive).
    """

    service: str
    tag: str
    context: Path
    dockerfile: str | None = None
    dockerfile_text: str | None = None
    args: Mapping[str, str] = field(default_factory=dict)
    contexts: Mapping[str, str] = field(default_factory=dict)  # `FROM` substitutions

    def __post_init__(self) -> None:
        if (self.dockerfile is None) == (self.dockerfile_text is None):
            raise ImageError(f"service {self.service}: exactly one of dockerfile and dockerfile_text is required")


class Builder(Protocol):
    """BuildKit builder. Builds and pushes, returning the pushed manifest digest (`sha256:...`)."""

    def build(self, request: BuildRequest) -> str: ...


def derive_dockerfile(
    base: str, *, copy: Sequence[tuple[str, str]] = (), clear: Sequence[str] = (), user: str = "0:0"
) -> str:
    """Dockerfile for the derived layer.

    - `clear`: `nocopy` volume mount points; empties existing image content under the path, keeping the directory;
    - `copy`: (source in context, target in image); bakes a bind into the image target. Existing target content is
      removed first so that directory shadowing also holds.

    Derived file preparation runs as root and requires the source image to provide `/bin/sh`.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)?", user):
        raise ImageError(f"invalid USER for derived image: {user!r}")
    # File preparation needs root, but the final USER must be restored; other config is inherited via FROM.
    # An explicit gid avoids BuildKit failing to look up uid 0's group on minimal images without /etc/passwd.
    lines = [f"FROM {base}", "USER 0:0"]
    for target in clear:
        quoted = shlex.quote(target)
        lines.append(_run(f"mkdir -p {quoted} && rm -rf {quoted}/* {quoted}/.[!.]* {quoted}/..?*"))
    for source, target in copy:
        lines.append(_run(f"rm -rf {shlex.quote(target)}"))
        lines.append(f"COPY [{json.dumps(source)}, {json.dumps(target)}]")
    lines.append(f"USER {user}")
    return "\n".join(lines) + "\n"


def _run(script: str) -> str:
    return f"RUN [{json.dumps('/bin/sh')}, {json.dumps('-c')}, {json.dumps(script)}]"
