from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("harbor")
from harbor.models.task.config import TaskConfig
from harbor.models.task.task import strip_canary
from pydantic import ValidationError

from flotilla.integrations.harbor_source_reader import NativeTaskSource, SourceMapping


def make_task(root: Path, name: str = "task", *, instruction: str = "Do the work.\n", config: str = "") -> Path:
    task = root / name
    task.mkdir(parents=True)
    (task / "task.toml").write_text(config, encoding="utf-8")
    (task / "instruction.md").write_text(instruction, encoding="utf-8")
    return task


@pytest.fixture
def source(tmp_path: Path) -> NativeTaskSource:
    make_task(tmp_path)
    return NativeTaskSource([{"local_root": tmp_path, "platform_prefix": "datasets/native"}])


@pytest.mark.parametrize(
    "instruction",
    [
        "Do the work.\n",
        "# BENCHMARK CANARY marker\n\nDo the work.\n",
        " <!-- Canary marker --> \n# another CANARY\n\nDo the work.\n",
        "\n\nDo the work.\n# trailing canary stays\n",
    ],
)
def test_native_defaults_and_canary(tmp_path: Path, instruction: str) -> None:
    task = make_task(tmp_path, instruction=instruction)
    source = NativeTaskSource([SourceMapping(local_root=tmp_path, platform_prefix="data")])
    metadata = source.read_metadata("data/task")
    assert metadata == {
        "task_id": "task",
        "instruction": strip_canary(instruction),
        "task_config": TaskConfig.model_validate_toml("").model_dump(mode="json"),
    }
    assert source.resolve_task("data/task") == task
    assert source.resolve_task("data") == tmp_path


def test_no_payload_traversal_or_filesystem_cache(source: NativeTaskSource, monkeypatch: pytest.MonkeyPatch) -> None:
    task = source.resolve_task("datasets/native/task")
    # Neither an invalid hidden tree nor absent payloads affect metadata reads.
    (task / "tests").symlink_to("/not-a-real-hidden-tree")
    (task / "solution").symlink_to("/not-a-real-solution-tree")
    original = Path.read_text
    opened = []

    def track(path: Path, *args: Any, **kwargs: Any) -> str:
        opened.append(path)
        assert path in {task / "task.toml", task / "instruction.md"}
        return original(path, *args, **kwargs)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("metadata reader traversed a payload tree")

    monkeypatch.setattr(Path, "read_text", track)
    for method in ("rglob", "glob", "iterdir", "walk"):
        monkeypatch.setattr(Path, method, forbidden)
    monkeypatch.setattr(os, "scandir", forbidden)
    monkeypatch.setattr(os, "walk", forbidden)
    assert source.read_metadata("datasets/native/task")["instruction"] == "Do the work.\n"
    (task / "instruction.md").write_text("New instruction", encoding="utf-8")
    assert source.read_metadata("datasets/native/task")["instruction"] == "New instruction"
    assert opened == [task / "task.toml", task / "instruction.md"] * 2


@pytest.mark.parametrize("bad", ["", "/abs", "a/../b", "a/./b", "a//b", "a/", "a\\b", "a\x00b", "a\x7fb"])
def test_invalid_keys_and_prefixes(source: NativeTaskSource, bad: str) -> None:
    with pytest.raises(ValueError):
        source.resolve_task(bad)
    with pytest.raises(ValidationError):
        SourceMapping(local_root=source.sources[0].local_root, platform_prefix=bad)


def test_source_mapping_validation(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SourceMapping.model_validate({"local_root": first, "platform_prefix": "a", "unexpected": True})
    with pytest.raises(FileNotFoundError):
        NativeTaskSource([{"local_root": tmp_path / "missing", "platform_prefix": "a"}])
    plain_file = tmp_path / "file"
    plain_file.touch()
    with pytest.raises(NotADirectoryError):
        NativeTaskSource([{"local_root": plain_file, "platform_prefix": "a"}])
    for left, right, prefix in [
        (first, second, "a"),
        (first, second, "a/b"),
        (first, first, "b"),
        (tmp_path, first, "b"),
    ]:
        with pytest.raises(ValueError, match="overlapping"):
            NativeTaskSource(
                [{"local_root": left, "platform_prefix": "a"}, {"local_root": right, "platform_prefix": prefix}]
            )
    # Component boundaries, not string startswith, define overlap.
    source = NativeTaskSource(
        [{"local_root": first, "platform_prefix": "a"}, {"local_root": second, "platform_prefix": "ab"}]
    )
    assert source.resolve_task("ab") == second
    assert [mapping.platform_prefix for mapping in source.sources] == ["a", "ab"]
    with pytest.raises(ValueError, match="exactly one"):
        source.resolve_task("abc")


def test_missing_malformed_and_non_file_inputs(source: NativeTaskSource) -> None:
    key = "datasets/native/task"
    task = source.resolve_task(key)
    with pytest.raises(FileNotFoundError):
        source.resolve_task("datasets/native/missing")
    with pytest.raises(NotADirectoryError):
        source.resolve_task(key + "/task.toml")
    (task / "task.toml").write_text("not valid TOML", encoding="utf-8")
    with pytest.raises(ValueError):
        source.read_metadata(key)
    (task / "task.toml").write_text('[environment]\ncpu = "not-a-number"\ncpus = "bad"', encoding="utf-8")
    with pytest.raises(ValidationError):
        source.read_metadata(key)
    (task / "task.toml").unlink()
    with pytest.raises(FileNotFoundError):
        source.read_metadata(key)
    (task / "task.toml").mkdir()
    with pytest.raises(ValueError, match="regular file"):
        source.read_metadata(key)


@pytest.mark.parametrize("filename", ["task.toml", "instruction.md"])
def test_metadata_symlink_escape_rejected(tmp_path: Path, filename: str) -> None:
    root = tmp_path / "root"
    task = make_task(root)
    sibling = make_task(root, "sibling")
    source = NativeTaskSource([{"local_root": root, "platform_prefix": "data"}])
    (task / filename).unlink()
    (task / filename).symlink_to(sibling / filename)
    with pytest.raises(ValueError, match="escapes task"):
        source.read_metadata("data/task")


def test_task_escape_and_contained_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    task = make_task(root)
    outside = make_task(tmp_path, "outside")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    source = NativeTaskSource([{"local_root": root, "platform_prefix": "data"}])
    with pytest.raises(ValueError, match="escapes source"):
        source.resolve_task("data/escape")
    instruction = task / "instruction.md"
    instruction.rename(task / "real-instruction.md")
    instruction.symlink_to("real-instruction.md")
    assert source.read_metadata("data/task")["instruction"] == "Do the work.\n"
    alias = tmp_path / "root-alias"
    alias.symlink_to(root, target_is_directory=True)
    assert NativeTaskSource([{"local_root": alias, "platform_prefix": "data"}]).sources[0].local_root == root
    with pytest.raises(ValueError, match="overlapping"):
        NativeTaskSource([{"local_root": root, "platform_prefix": "a"}, {"local_root": alias, "platform_prefix": "b"}])
