"""Installed Anchor helper: verify actual selected published files, not just manifests."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from .models import DatasetRow, Task
from .release import ReleaseReader, ReleaseWriter


def verify(request: dict[str, Any]) -> dict[str, Any]:
    reader = ReleaseReader(request["root"], ownership=True)
    row = DatasetRow.model_validate(request["row"])
    # Only the launch manager supplies this set, after earlier successful checks
    # of the same release/root. There is no cross-launch cache.
    reader._assets.update(set(request.get("validated_assets", ())) & set(reader.manifest.assets))
    reader.read_task(row)
    return {"release_id": reader.manifest.release_id, "task_digest": row.task_digest, "assets": sorted(reader._assets)}


def self_test() -> None:
    from flotilla.manifest import Context, Manifest, Resources, Unit

    with tempfile.TemporaryDirectory(prefix="flotilla-native-") as directory:
        base = Path(directory)
        source = base / "source"
        source.mkdir()
        (source / "data").write_text("native-protocol-self-test")
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
        with ReleaseWriter(base / "release") as writer:
            asset = writer.add_asset(source)
            record = writer.add_task(Task(key="self-test", roles={"main": manifest}, assets=(asset,)), "self-test")
            release = writer.publish([{"task_ref": record.task_ref, "task_digest": record.task_digest}])
        row = DatasetRow(release_id=release.release_id, task_ref=record.task_ref, task_digest=record.task_digest)
        request = {"root": str(base / "release"), "row": row.model_dump()}
        verify(request)
        (base / "release" / asset / "data").write_text("drift")
        try:
            verify(request)
        except ValueError:
            pass
        else:
            raise AssertionError("native integrity check failed to detect file drift")
        try:
            Task.model_validate({"schema_version": 999, "key": "bad", "roles": {"main": manifest}})
        except ValueError:
            pass
        else:
            raise AssertionError("native protocol version check failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", nargs="?")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--clear-directory")
    args = parser.parse_args()
    if args.clear_directory:
        import shutil

        root = Path(args.clear_directory)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("private output must be a real directory")
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        return
    if args.self_test:
        self_test()
        return
    if not args.request:
        parser.error("request is required")
    request = json.loads(Path(args.request).read_text())
    result = verify(request)
    Path(request["result"]).write_text(json.dumps(result))


if __name__ == "__main__":
    main()
