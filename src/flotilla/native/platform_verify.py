"""Installed Anchor helper: clear a verifier output directory in place; `--self-test` checks the install."""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path


def clear_directory(root: Path) -> None:
    """Remove every child without replacing the (mounted) directory inode."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("private output must be a real directory")
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def self_test() -> None:
    from flotilla.manifest import Context, Manifest, Resources, Unit

    from .models import Task
    from .release import ReleaseReader, ReleaseWriter

    with tempfile.TemporaryDirectory(prefix="flotilla-native-") as directory:
        base = Path(directory)
        manifest = Manifest(
            task="self-test",
            agent_service="main",
            units={
                "main": Unit(
                    image="example.invalid/runtime@sha256:" + "a" * 64,
                    context=Context(env={}, uid=0, gid=0, cwd="/"),
                    resources=Resources(cpu="1", memory="128Mi"),
                    command=None,
                )
            },
        )
        with ReleaseWriter(base / "release", "self-test") as writer:
            writer.add_task(Task(key="self-test", roles={"main": manifest}), "self-test")
            (row,) = writer.publish([{"task": "self-test"}])
        reader = ReleaseReader(base / "release")
        if reader.read_dataset() != [row] or reader.instruction("self-test") != "self-test":
            raise AssertionError("native release round trip failed")
        try:
            Task.model_validate({"schema_version": 999, "key": "bad", "roles": {"main": manifest}})
        except ValueError:
            pass
        else:
            raise AssertionError("native protocol version check failed")
        output = base / "output"
        (output / "nested").mkdir(parents=True)
        (output / "reward.txt").write_text("1")
        clear_directory(output)
        if any(output.iterdir()):
            raise AssertionError("verifier output directory was not cleared")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--self-test", action="store_true")
    group.add_argument("--clear-directory", type=Path)
    args = parser.parse_args()
    if args.clear_directory:
        clear_directory(args.clear_directory)
    else:
        self_test()


if __name__ == "__main__":
    main()
