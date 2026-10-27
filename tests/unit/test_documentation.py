"""Execute the public API examples in the docs and validate documentation links."""

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
    readme = (ROOT / "README.md").read_text()
    source = tmp_path / "task"
    (source / "environment").mkdir(parents=True)
    (source / "task.toml").write_text("[environment]\n")
    [compose] = re.findall(r"```yaml\n(.*?)```", readme, re.S)
    (source / "environment" / "docker-compose.yaml").write_text(compose)
    client = flotilla.Flotilla
    monkeypatch.setattr(flotilla, "Flotilla", lambda **_: client(deployment=cfg))
    code = re.findall(r"```python\n(.*?)```", readme, re.S)[0]
    namespace: dict[str, object] = {}
    exec(compile(code.replace("asyncio.run(main())", ""), "README.md", "exec"), namespace)
    await namespace["main"]()  # type: ignore[operator]


@pytest.mark.asyncio
async def test_quick_start_example(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, platform, _ = deployment
    platform.initial_files["/etc/nginx/nginx.conf"] = b"events {}\n"
    doc = (ROOT / "docs/Quick_Start.md").read_text()
    # Mirrors the tutorial's shell commands; the Compose file is read from the page itself.
    task = tmp_path / "hello-web"
    (task / "environment" / "site").mkdir(parents=True)
    (task / "task.toml").write_text("[environment]\n")
    (task / "environment" / "Dockerfile").write_text("FROM alpine:3.20\nRUN apk add --no-cache curl\n")
    (task / "environment" / "site" / "index.html").write_text("<h1>hello from web</h1>\n")
    [compose] = re.findall(r"```yaml\n(.*?)```", doc, re.S)
    (task / "environment" / "docker-compose.yaml").write_text(compose)
    monkeypatch.chdir(tmp_path)
    client = flotilla.Flotilla
    monkeypatch.setattr(flotilla, "Flotilla", lambda **_: client(deployment=cfg))
    [code] = [block for block in re.findall(r"```python\n(.*?)```", doc, re.S) if "async def main" in block]
    namespace: dict[str, object] = {}
    exec(compile(code.replace("asyncio.run(main())", ""), "Quick_Start.md", "exec"), namespace)
    await namespace["main"]()  # type: ignore[operator]
    assert (tmp_path / "nginx.conf").read_bytes() == b"events {}\n"


@pytest.mark.asyncio
async def test_integration_guide_test_example(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree], tmp_path: Path
) -> None:
    blocks = re.findall(r"```python\n(.*?)```", (ROOT / "docs/Integration_Guide.md").read_text(), re.S)
    [code] = [block for block in blocks if "def test_" in block]
    namespace: dict[str, object] = {}
    exec(compile(code, "Integration_Guide.md", "exec"), namespace)
    [test] = [value for name, value in namespace.items() if name.startswith("test_")]
    await test(deployment, tmp_path)  # type: ignore[operator]


def test_local_documentation_links_resolve() -> None:
    paths = [ROOT / "README.md", ROOT / "README_zh.md", ROOT / "CONTRIBUTING.md", *ROOT.glob("docs/**/*.md")]
    for path in paths:
        for link in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            if "://" in link or link.startswith("#"):
                continue
            assert (path.parent / link.split("#")[0]).exists(), (path, link)
