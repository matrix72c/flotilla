"""Execute the public API examples in the docs and validate documentation links."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

import flotilla
from flotilla.build.driver import build_task
from flotilla.config import FlotillaConfig
from flotilla.core import labels
from flotilla.integrations.harbor_source import task_environments
from flotilla.integrations.workspace import describe
from flotilla.manifest import Manifest
from flotilla.platform.base import ExecAttempt, ExecResult, InstanceHandle, ProcessSpec
from flotilla.runtime.workspace import Workspace
from flotilla.testing.build import _ctx
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as deployment

ROOT = Path(__file__).resolve().parents[2]


def workspace_compiler(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    task: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Stage real source bytes; fake only compiler tooling and missing SharedTree stat.

    Workspace creation/copying, HarborAttempt, role planning/open and cleanup remain real.
    """
    cfg, platform, tree = deployment
    source_key = f"datasets/demo/{task.name}"
    tree.dirs.update({"datasets", "datasets/demo", source_key})

    def stage(root: Path, key: str) -> None:
        tree.dirs.add(key)
        for path in root.rglob("*"):
            target = key + "/" + path.relative_to(root).as_posix()
            if path.is_dir():
                tree.dirs.add(target)
            else:
                tree.files.add(target)
                platform.tree_contents[target] = path.read_bytes()
                tree.modes[target] = f"{path.stat().st_mode & 0o777:04o}"

    stage(task, source_key)
    original_exec = platform.exec
    calls = []

    async def execute(handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None) -> ExecResult:
        argv = proc.argv
        if platform.spec_of(handle.iid).labels.get(labels.ROLE) == labels.ROLE_ANCHOR and argv[:4] == (
            "/.flotilla/bin/busybox",
            "stat",
            "-c",
            "%F",
        ):
            key = argv[-1].removeprefix(tree.mount)
            if key in tree.dirs:
                return ExecResult(0, b"directory\n", b"")
            if key in tree.files:
                return ExecResult(0, b"regular file\n", b"")
            return ExecResult(1, b"", b"missing fixture source")
        return await original_exec(handle, proc, attempt=attempt)

    async def compile_workspace(workspace: Workspace, runtime_params: dict[str, str] | None = None) -> dict[str, Any]:
        calls.append(workspace.key)
        assert workspace.task_key == source_key
        assert workspace.source_key in tree.dirs
        # Reconstruct only the already-copied fake remote source for local fake
        # build tooling; no direct read of the operator's original source here.
        root = tmp_path / "compiler" / workspace.id
        source = root / "source" / task.name
        source.mkdir(parents=True)
        for key in list(tree.files):
            if key.startswith(workspace.source_key + "/"):
                path = source / key[len(workspace.source_key) + 1 :]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(platform.tree_contents[key])
                path.chmod(int(tree.modes.get(key, "0644"), 8))
        result = describe(source, root)
        compiled = {}
        for role, environment in zip(result["definitions"], task_environments(source), strict=True):
            output = root / "generated" / role
            outcome = build_task(environment, output, _ctx(caps=cfg.platform_capabilities()))
            assert outcome.manifest_path is not None, outcome.reason
            manifest = Manifest.load_json(outcome.manifest_path.read_bytes())
            if outcome.needs_publish:
                files = outcome.manifest_path.parent / "files"
                files_key = workspace.key + "/" + files.relative_to(root).as_posix()
                stage(files, files_key)
                manifest = manifest.model_copy(update={"task_files": files_key, "task_files_published": None})
            compiled[role] = manifest.model_dump(mode="json", by_alias=True)
        stage(root / "public", workspace.key + "/public")
        result["environments"] = compiled
        return result

    monkeypatch.setattr(platform, "exec", execute)
    monkeypatch.setattr(Workspace, "prepare", compile_workspace)
    return calls


@pytest.mark.asyncio
async def test_readme_example(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, platform, tree = deployment
    monkeypatch.chdir(tmp_path)
    readme = (ROOT / "README.md").read_text()
    source = tmp_path / "task"
    (source / "environment").mkdir(parents=True)
    (source / "task.toml").write_text("[environment]\n")
    [compose] = re.findall(r"```yaml\n(.*?)```", readme, re.S)
    (source / "environment" / "docker-compose.yaml").write_text(compose)
    (source / "instruction.md").write_text("Check Redis connectivity.")
    (source / "tests").mkdir()
    (source / "tests/test.sh").write_text("#!/bin/sh\nset -eu\nprintf 'PING\\r\\n' | nc -w 2 cache 6379\n")
    calls = workspace_compiler(deployment, source, tmp_path, monkeypatch)
    client = flotilla.Flotilla
    monkeypatch.setattr(flotilla, "Flotilla", lambda **_: client(deployment=cfg))
    code = re.findall(r"```python\n(.*?)```", readme, re.S)[0]
    namespace: dict[str, object] = {}
    exec(compile(code.replace("asyncio.run(main())", ""), "README.md", "exec"), namespace)
    await namespace["main"]()  # type: ignore[operator]
    assert len(calls) == 1 and platform.instances() == []
    assert "datasets/demo/task/instruction.md" in tree.files
    assert not any(key.startswith(calls[0]) for key in tree.dirs | tree.files)


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

    (task / "instruction.md").write_text("Fetch the web page.")
    (task / "tests").mkdir()
    (task / "tests/test.sh").write_text("#!/bin/sh\nset -eu\ncurl -fsS http://web/ >/dev/null\n")
    calls = workspace_compiler(deployment, task, tmp_path, monkeypatch)
    client = flotilla.Flotilla
    monkeypatch.setattr(flotilla, "Flotilla", lambda **_: client(deployment=cfg))
    [code] = [block for block in re.findall(r"```python\n(.*?)```", doc, re.S) if "async def main" in block]
    namespace: dict[str, object] = {}
    exec(compile(code.replace("asyncio.run(main())", ""), "Quick_Start.md", "exec"), namespace)
    await namespace["main"]()  # type: ignore[operator]
    assert (tmp_path / "nginx.conf").read_bytes() == b"events {}\n"
    assert len(calls) == 1 and platform.instances() == []
    tree = deployment[2]
    assert "datasets/demo/hello-web/environment/site/index.html" in tree.files
    assert not any(key.startswith(calls[0]) for key in tree.dirs | tree.files)


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
