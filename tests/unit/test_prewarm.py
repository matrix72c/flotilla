"""Legacy warmup diagnostics preserve repeated platform keys; the public prepare is the native release API."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from flotilla.prepare import legacy_warmup


@pytest.mark.asyncio
async def test_prewarm_preserves_duplicates_and_releases_every_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, closed = [], []

    @asynccontextmanager
    async def workspace(key: str) -> AsyncIterator[SimpleNamespace]:
        entered.append(key)
        try:
            yield SimpleNamespace(
                task_id=key.rsplit("/", 1)[-1],
                prepare=AsyncMock(return_value={"environments": {"main": {}, "verifier": {}}}),
            )
        finally:
            closed.append(key)

    @asynccontextmanager
    async def fleet(**kwargs: object) -> AsyncIterator[SimpleNamespace]:
        assert kwargs == {"deployment": "deployment.toml"}
        yield SimpleNamespace(workspace=AsyncMock(side_effect=lambda key: workspace(key)))

    monkeypatch.setattr("flotilla.runtime.client.Flotilla", fleet)
    keys = ["dataset/a/t", "dataset/b/t", "dataset/a/t"]
    result = await legacy_warmup(keys, deployment="deployment.toml")
    assert entered == closed == keys
    assert [r["task_key"] for r in result] == keys
    assert all(r["id"] == "t" and r["status"] == "ready" for r in result)
    assert all("env" not in r and "task_dir" not in r for r in result)


@pytest.mark.asyncio
async def test_prewarm_reports_failed_task_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def workspace(key: str) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(task_id="t", prepare=AsyncMock(side_effect=ValueError("unsupported Compose feature")))

    @asynccontextmanager
    async def fleet(**kwargs: object) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(workspace=AsyncMock(side_effect=lambda key: workspace(key)))

    monkeypatch.setattr("flotilla.runtime.client.Flotilla", fleet)
    result = await legacy_warmup(["data/t", "data/t"], deployment="deployment.toml")
    assert len(result) == 2
    assert all(r["status"] == "failed" and "unsupported Compose" in r["error"] for r in result)
