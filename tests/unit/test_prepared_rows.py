"""Prepared rows: the jsonl contract between `flotilla prepare` and consumers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from flotilla import PreparedTask, load_prepared
from flotilla.manifest import SCHEMA_VERSION


def _env(image: str = "r.io/a@sha256:" + "b" * 64) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "task": "ds/t",
        "agent_service": "main",
        "units": {
            "main": {
                "image": image,
                "command": None,
                "context": {"env": {}, "uid": 0, "gid": 0, "cwd": "/"},
                "resources": {},
            }
        },
    }


def _row(ident: str = "ds/t", **extra: Any) -> dict[str, Any]:
    return {"id": ident, "task_dir": f"/data/{ident}", "services": 1, "env": {"main": _env()}, **extra}


def test_row_roundtrip_keeps_attributes_and_consumer_fields() -> None:
    row = _row(runtime_params={"FLAG": "x"}, dataset_rev="abc", flotilla_version="1.0")
    task = PreparedTask.from_row(row)
    assert task.environment().manifest().units["main"].image.endswith("b" * 64)
    assert task.verifier is None
    assert task.attrs == {"services": 1, "runtime_params": {"FLAG": "x"}}
    assert PreparedTask.from_row(task.to_row()) == task


def test_env_is_strict() -> None:
    with pytest.raises(ValueError, match="env"):
        PreparedTask.from_row({"id": "ds/t", "task_dir": "/x"})
    with pytest.raises(ValueError, match="at most 'verifier'"):
        PreparedTask.from_row(_row(env={"main": _env(), "extra": _env()}))
    unknown = _env()
    unknown["surprise"] = 1
    with pytest.raises(ValueError):
        PreparedTask.from_row(_row(env={"main": unknown}))
    old = _env()
    old["schema"] = 5
    with pytest.raises(ValueError, match="schema"):
        PreparedTask.from_row(_row(env={"main": old}))


def test_load_schema_six_row_without_rewriting_source(tmp_path: Path) -> None:
    old = _env()
    old["schema"] = 6
    old["runtime_params"] = [{"service": "main", "var": "VALUE", "expr": "${TOKEN}", "param": "TOKEN", "default": None}]
    path = tmp_path / "rows.jsonl"
    original = json.dumps(_row(env={"main": old})) + "\n"
    path.write_text(original)
    [task] = load_prepared(path)
    manifest = task.environment().manifest()
    assert manifest.schema_ == SCHEMA_VERSION
    assert manifest.runtime_params[0].expr == "${TOKEN}"
    assert path.read_text() == original


def test_load_prepared_last_row_wins_and_reports_line(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    newer = _row(env={"main": _env("r.io/a@sha256:" + "c" * 64)})
    path.write_text("\n".join(json.dumps(r) for r in (_row(), _row("ds/u"), newer)) + "\n\n")
    rows = load_prepared(path)
    assert [r.id for r in rows] == ["ds/t", "ds/u"]
    assert rows[0].environment().manifest().units["main"].image.endswith("c" * 64)
    path.write_text(json.dumps(_row()) + "\n{not json\n")
    with pytest.raises(ValueError, match=r"rows\.jsonl:2"):
        load_prepared(path)
