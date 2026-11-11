"""Execute public native examples and validate documentation links."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from flotilla.config import FlotillaConfig, TaskRelease
from flotilla.manifest import Context, Manifest, Resources, Unit
from flotilla.native import ReleaseWriter, Task, Verification
from flotilla.native.platform_verify import verify
from flotilla.platform.base import ExecAttempt, ExecResult, InstanceHandle, ProcessSpec
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as deployment

ROOT = Path(__file__).resolve().parents[2]


def native_example(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    """Real release reader/writer and lifecycle; fake only platform execution."""
    cfg, platform, tree = deployment
    release = tmp_path / "release"
    unit = Unit(
        image="example.invalid/runtime@sha256:" + "a" * 64,
        context=Context(env={}, uid=0, gid=0, cwd="/"),
        resources=Resources(cpu="1", memory="128Mi"),
        command=None,
    )
    with ReleaseWriter(release) as writer:
        task = Task(
            key="documentation/example",
            roles={
                "main": Manifest(
                    task="documentation/example", agent_service="main", units={"main": unit, "cache": unit}
                )
            },
            verification=Verification(command=("true",)),
        )
        record = writer.add_task(task, "Check connectivity.")
        writer.publish([{"task_ref": record.task_ref, "task_digest": record.task_digest}])
    cfg = cfg.model_copy(
        update={"task_releases": {"demo": TaskRelease(local_root=release, platform_root="/mnt/shared/releases/demo")}}
    )
    monkeypatch.setattr(FlotillaConfig, "load", lambda _: cfg)
    monkeypatch.chdir(tmp_path)
    original = platform.exec
    checks: list[dict[str, Any]] = []

    async def execute(handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None) -> ExecResult:
        if proc.argv[:4] == ("python3", "-I", "-m", "flotilla.native.platform_verify"):
            if proc.argv[4] == "--clear-directory":
                return ExecResult(0, b"", b"")
            request_key = proc.argv[4].removeprefix(tree.mount)
            request = json.loads(platform.tree_contents[request_key])
            checks.append(request)
            # The fake platform shares the immutable release fixture. Run the real
            # installed helper logic; don't synthesize an integrity success flag.
            response = verify({**request, "root": str(release)})
            result_key = request["result"].removeprefix(tree.mount)
            tree.files.add(result_key)
            platform.tree_contents[result_key] = json.dumps(response).encode()
            return ExecResult(0, b"", b"")
        return await original(handle, proc, attempt=attempt)

    monkeypatch.setattr(platform, "exec", execute)
    return checks


@pytest.mark.asyncio
@pytest.mark.parametrize("document", ["README.md", "README_zh.md", "docs/Quick_Start.md"])
async def test_native_documentation_example(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document: str,
) -> None:
    checks = native_example(deployment, tmp_path, monkeypatch)
    blocks = re.findall(r"```python\n(.*?)```", (ROOT / document).read_text(), re.S)
    [code] = [block for block in blocks if "async def main" in block]
    namespace: dict[str, Any] = {}
    exec(compile(code.replace("asyncio.run(main())", ""), document, "exec"), namespace)
    await namespace["main"]()
    assert len(checks) == 1
    assert deployment[1].instances() == []
    assert not any(key.startswith("launches/") for key in deployment[2].dirs | deployment[2].files)


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
