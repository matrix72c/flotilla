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
    """Prove the installed package can do the one thing the anchor calls it for.

    It used to also round-trip a release through its own reader and writer. There is no release
    to round-trip now: the only installed-side behaviour is clearing a mounted output directory,
    so that is what this checks.
    """
    with tempfile.TemporaryDirectory(prefix="flotilla-native-") as directory:
        output = Path(directory) / "output"
        (output / "nested").mkdir(parents=True)
        (output / "nested" / "reward.txt").write_text("1")
        (output / "reward.txt").write_text("1")
        (output / "link").symlink_to(output / "reward.txt")
        clear_directory(output)
        if any(output.iterdir()):
            raise AssertionError("verifier output directory was not cleared")
        if not output.is_dir():
            raise AssertionError("verifier output directory was replaced rather than cleared")


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
