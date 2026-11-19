"""Checking a dataset before anything is created: what `flotilla inspect` reports, and from where."""

from __future__ import annotations

from pathlib import Path

import pytest

from flotilla.config import FlotillaConfig
from flotilla.inspect import inspect_source, sources_of
from flotilla.models.plan import TaskSource
from flotilla.native.dataset import DatasetError
from flotilla.testing.config import config
from flotilla.testing.planning import APP, DIGEST2, FakeConfigs, _task


def _dataset(root: Path) -> Path:
    """A dataset of three task directories: one plain, one with a peer, one the dialect refuses."""
    _task(root, "solo", compose=f"services:\n  main:\n    image: {APP}\n")
    _task(root, "pair", compose=f"services:\n  main:\n    image: {APP}\n  db:\n    image: r.io/ns/db@{DIGEST2}\n")
    _task(root, "stray-network", compose=f"services:\n  main:\n    image: {APP}\n    networks: [missing]\n")
    return root


def _cfg(root: Path, **sections: object) -> FlotillaConfig:
    return config(
        datasets={"tb": {"local_root": str(root), "platform_root": "datasets/tb", "input": "datasets"}}, **sections
    )


def test_a_task_that_plans_reports_its_services(tmp_path: Path) -> None:
    """`planned` is the whole claim: this task would open, with these services."""
    root = _dataset(tmp_path / "tasks")
    cfg = _cfg(root)
    source = cfg.resolve_datasets()["tb"].source("pair")
    report = inspect_source(cfg, source, FakeConfigs())
    assert report.planned and report.services == ("db", "main")
    assert report.task == "datasets/tb/pair" and not report.rejected and not report.error


def test_a_refused_field_is_reported_without_reaching_the_registry(tmp_path: Path) -> None:
    """A field the dialect refuses costs no network: the scan runs before anything is read."""
    root = _dataset(tmp_path / "tasks")
    cfg = _cfg(root)
    configs = FakeConfigs()
    report = inspect_source(cfg, cfg.resolve_datasets()["tb"].source("stray-network"), configs)
    assert not report.planned
    assert any("networks" in reason for reason in report.rejected)
    assert configs.reads == []  # nothing left the machine for a task already known to fail


def test_an_unreadable_task_is_a_report_not_an_exception(tmp_path: Path) -> None:
    """One malformed task must not hide the rest of a dataset, so nothing raises."""
    root = tmp_path / "tasks"
    broken = _task(root, "broken", compose=f"services:\n  main:\n    image: {APP}\n")
    (broken / "task.toml").write_text("[task\n")
    report = inspect_source(config(), TaskSource(local=broken), FakeConfigs())
    assert not report.planned and report.error
    assert "broken" in report.task


def test_a_registry_failure_is_reported_against_the_task(tmp_path: Path) -> None:
    """A config the registry will not serve fails that task, not the whole pass."""
    root = _dataset(tmp_path / "tasks")
    cfg = _cfg(root)

    class Refusing(FakeConfigs):
        def image_config(self, pinned: str) -> dict[str, object]:
            raise OSError("registry unreachable")

    report = inspect_source(cfg, cfg.resolve_datasets()["tb"].source("solo"), Refusing())
    assert not report.planned and "OSError" in report.error
    assert not report.rejected  # the task itself is fine; the read is what failed


def test_planning_fills_the_image_config_cache(tmp_path: Path) -> None:
    """Checking a dataset warms the cache a run then uses, because planning is what fills it."""
    root = _dataset(tmp_path / "tasks")
    cfg = _cfg(root)
    configs = FakeConfigs()
    dataset = cfg.resolve_datasets()["tb"]
    for key in ("solo", "pair"):
        assert inspect_source(cfg, dataset.source(key), configs).planned
    assert configs.reads.count(APP) == 2  # a real client caches by digest; the fake counts every ask


def test_no_target_means_every_declared_dataset(tmp_path: Path) -> None:
    """What a run is given is a dataset name, so that is what checking a deployment defaults to."""
    first = _dataset(tmp_path / "one")
    second = _dataset(tmp_path / "two")
    cfg = config(
        datasets={
            "a": {"local_root": str(first), "platform_root": "datasets/a", "input": "datasets"},
            "b": {"local_root": str(second), "platform_root": "datasets/b", "input": "datasets"},
        }
    )
    keys = {source.key for source in sources_of(cfg, [])}
    assert keys == {f"datasets/{name}/{task}" for name in "ab" for task in ("pair", "solo", "stray-network")}
    # One dataset by name narrows it; each source carries the platform key a plan is checked against.
    one = {source.key for source in sources_of(cfg, ["a"])}
    assert one == {f"datasets/a/{t}" for t in ("pair", "solo", "stray-network")}


def test_a_local_path_is_accepted_with_no_platform_key(tmp_path: Path) -> None:
    """A dataset not declared yet has no platform key, so its plans skip the storage rules."""
    root = _dataset(tmp_path / "tasks")
    sources = sources_of(config(inputs={}, datasets={}), [str(root)])
    assert len(sources) == 3 and all(source.key is None for source in sources)
    assert sources_of(config(inputs={}, datasets={}), [str(root / "solo")])[0].local == root / "solo"


def test_an_unusable_target_says_so(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="neither a declared dataset nor a directory"):
        sources_of(config(), ["not-a-dataset"])
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(DatasetError, match="no task directory below"):
        sources_of(config(), [str(empty)])
    with pytest.raises(DatasetError, match="declares no datasets"):
        sources_of(config(inputs={}, datasets={}), [])
