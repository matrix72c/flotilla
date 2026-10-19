"""Execute the README's public API example and validate documentation links."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import flotilla
from flotilla.config import FlotillaConfig
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as deployment

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_readme_example(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, _, _ = deployment
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "task"
    (source / "environment").mkdir(parents=True)
    (source / "task.toml").write_text('[environment]\ndocker_image="r.io/main:1"\n')
    client = flotilla.Flotilla
    monkeypatch.setattr(flotilla, "Flotilla", lambda **_: client(deployment=cfg))
    code = re.findall(r"```python\n(.*?)```", (ROOT / "README.md").read_text(), re.S)[0]
    namespace: dict[str, object] = {}
    exec(compile(code.replace("asyncio.run(main())", ""), "README.md", "exec"), namespace)
    await namespace["main"]()  # type: ignore[operator]


def test_local_documentation_links_resolve() -> None:
    paths = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", *ROOT.glob("docs/**/*.md")]
    for path in paths:
        for link in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            if "://" in link or link.startswith("#"):
                continue
            assert (path.parent / link.split("#")[0]).exists(), (path, link)
