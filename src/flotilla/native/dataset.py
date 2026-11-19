"""Point a run at a task directory on shared storage.

A release was a directory tree flotilla wrote, plus a `dataset.jsonl` naming rows inside it, plus
a `[task_releases.<name>]` table binding each release to where the platform could see it. All
three existed to connect a host that prepared tasks to a trainer that ran them.

The dataset is now the input, unchanged: a directory of task directories that someone else
publishes to shared storage. What is left is the mapping between the client's view of it and the
platform's, which is the one thing neither side can infer.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from flotilla.core.plan import TaskSource
from flotilla.native.models import relative_path

#: What makes a directory a task. Named here rather than imported from the parser, because finding
#: tasks must not need Compose: this module also runs installed inside a sandbox.
TASK_FILE = "task.toml"

#: Storage keys a dataset may not claim: the launch tree and the shared tools are flotilla's own.
#: Spelled out rather than imported, so finding tasks does not pull the orchestration core in.
RESERVED = frozenset({"launches", "share"})


class DatasetError(ValueError):
    """A dataset root is unusable: not a directory, not on declared storage, or holding no tasks."""


class Dataset:
    """One directory of task directories, as the client sees it and as the platform sees it.

    Both views are explicit. A client path cannot be translated into a platform key without
    knowing how the storage is mounted, and guessing a prefix is how a run ends up mounting the
    wrong directory and grading against the wrong files.
    """

    def __init__(self, local: str | Path, platform_key: str, *, root: str = "work") -> None:
        self.local = Path(local).expanduser().absolute()
        if not self.local.is_dir():
            raise DatasetError(f"dataset root is not a directory: {self.local}")
        self.key = relative_path(platform_key)
        if self.key.split("/")[0] in RESERVED:
            raise DatasetError(f"a dataset cannot live under {self.key.split('/')[0]}/, which flotilla owns")
        self.root = root

    @classmethod
    def from_platform_root(cls, local: str | Path, platform_root: str, *, storage: Any, root: str = "work") -> Dataset:
        """Build from a platform path or PVC identity, mapped through this deployment's storage."""
        return cls(local, platform_key(platform_root, storage), root=root)

    def tasks(self) -> list[str]:
        """Every task directory below the root, as keys relative to it, in a stable order."""
        out: list[str] = []
        for path in sorted(self.local.rglob(TASK_FILE)):
            if path.is_file():
                out.append(path.parent.relative_to(self.local).as_posix())
        if not out:
            raise DatasetError(f"no {TASK_FILE} below {self.local}")
        return out

    def source(self, task: str) -> TaskSource:
        """Where one task's files are, on both sides."""
        key = relative_path(task)
        local = self.local / key
        if not (local / TASK_FILE).is_file():
            raise DatasetError(f"{task} is not a task directory under {self.local}")
        if not local.resolve().is_relative_to(self.local.resolve()):
            raise DatasetError(f"task path escapes the dataset: {task}")
        return TaskSource(local=local, key=f"{self.key}/{key}", root=self.root)


def platform_key(value: str, storage: Any) -> str:
    """Map an explicit host path or PVC identity to a storage key; never guess a mount prefix."""
    if storage.volumes == "host":
        if value.startswith("pvc:"):
            raise ValueError("host storage requires an explicit host path, not a PVC identity")
        if not value.startswith("/"):
            key = value
        elif not storage.host_path.startswith("/"):
            raise ValueError(
                "storage host path is a platform identifier, not a filesystem path; use a relative platform root"
            )
        else:
            root, path = PurePosixPath(storage.host_path, storage.root_subpath), PurePosixPath(value)
            if str(path) != value or ".." in path.parts:
                raise ValueError("noncanonical platform root")
            try:
                key = str(path.relative_to(root))
            except ValueError as exc:
                raise ValueError("platform root is outside configured host storage") from exc
    elif value.startswith("pvc:"):
        prefix = f"pvc:{storage.claim_name}/" + (storage.root_subpath + "/" if storage.root_subpath else "")
        if not value.startswith(prefix):
            raise ValueError("platform root is outside configured PVC storage")
        key = value[len(prefix) :]
    else:
        raise ValueError("PVC storage requires an explicit pvc:<claim>/<subpath>/... platform root")
    key = relative_path(key)
    if key.split("/")[0] in RESERVED:
        raise ValueError("a dataset cannot overlap runtime or shared-tools storage")
    return key


def datasets(
    bindings: Mapping[str, Mapping[str, Any]], *, storage: Any, inputs: Mapping[str, Any]
) -> dict[str, Dataset]:
    """Build the deployment's declared datasets by name, each on `work` or a named input root."""
    out: dict[str, Dataset] = {}
    for name, value in bindings.items():
        root = str(value.get("root", "work"))
        if root != "work" and root not in inputs:
            raise DatasetError(f"dataset {name!r} is bound to unconfigured storage root {root!r}")
        out[name] = Dataset.from_platform_root(
            value["local_root"],
            str(value["platform_root"]),
            storage=storage if root == "work" else inputs[root],
            root=root,
        )
    return out
