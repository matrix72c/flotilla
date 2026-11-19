"""Execute the public examples in the documentation and validate documentation links."""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from flotilla.config import DatasetBinding, FlotillaConfig
from flotilla.platform.base import ExecAttempt, ExecResult, InstanceHandle, ProcessSpec
from flotilla.runtime.client import Flotilla
from flotilla.testing.config import importable
from flotilla.testing.planning import APP, FakeConfigs
from flotilla.testing.platform import FakePlatform, SharedTree
from flotilla.testing.runtime import deployment as deployment

ROOT = Path(__file__).resolve().parents[2]


def documented_example(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, ...]]:
    """Real planning and lifecycle over a real task directory; fake only the platform and registry."""
    cfg, platform, _tree = deployment
    task = tmp_path / "tasks" / "demo" / "redis"
    (task / "environment").mkdir(parents=True)
    (task / "instruction.md").write_text("Check connectivity.\n")
    (task / "tests").mkdir()
    (task / "tests/test.sh").write_text("#!/bin/sh\nexit 0\n")
    (task / "task.toml").write_text(f'[environment]\ndocker_image = "{APP}"\n')
    (task / "environment/docker-compose.yaml").write_text(
        f'services:\n  main: {{}}\n  cache:\n    image: {APP}\n    command: ["/bin/true"]\n'
    )
    bindings = {
        "example": DatasetBinding(local_root=tmp_path / "tasks", platform_root="/mnt/shared/datasets/example"),
        "demo": DatasetBinding(local_root=tmp_path / "tasks", platform_root="/mnt/shared/datasets/demo-v1"),
    }
    cfg = cfg.model_copy(update={"datasets": bindings})
    # The platform's copy of the same tree: staging the hidden tests reads it through the anchor.
    for binding in bindings.values():
        key = binding.platform_root.removeprefix("/mnt/shared/")
        tests_key = f"{key}/demo/redis/tests"
        # `cp -a -- <src>/. <dest>` is how the anchor copies contents; SharedTree models the `/.` form.
        _tree.dirs.update({key, f"{key}/demo", f"{key}/demo/redis", tests_key, f"{tests_key}/."})
        _tree.files.add(f"{key}/demo/redis/tests/test.sh")
        platform.tree_contents[f"{key}/demo/redis/tests/test.sh"] = b"#!/bin/sh\nexit 0\n"
    # The examples import the spec from a module the reader writes; stand in for exactly that.
    importable(cfg, name="myproject.deployment", attribute="PLATFORM")
    monkeypatch.setitem(sys.modules, "myproject", types.ModuleType("myproject"))
    monkeypatch.chdir(tmp_path)

    original_init = Flotilla.__init__

    def patched(self: Flotilla, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.image_configs = FakeConfigs()  # type: ignore[assignment]

    monkeypatch.setattr(Flotilla, "__init__", patched)

    original, original_read = platform.exec, platform.read_file
    helper_calls: list[tuple[str, ...]] = []

    async def execute(handle: InstanceHandle, proc: ProcessSpec, *, attempt: ExecAttempt | None = None) -> ExecResult:
        if proc.argv[:2] == ("/.flotilla/bin/busybox", "find") and proc.argv[3:] == ("-mindepth", "1", "-delete"):
            helper_calls.append(proc.argv)
            return ExecResult(0, b"", b"")
        if proc.argv[:2] == ("redis-cli", "ping"):
            return ExecResult(0, b"PONG\n", b"")
        if proc.argv[:2] == ("bash", "/tests/test.sh"):
            return ExecResult(0, b"", b"")
        if proc.argv[:3] == ("test", "-f", "/logs/verifier/reward.txt"):
            return ExecResult(1, b"", b"")  # this example's verifier writes reward.json
        if proc.argv[:3] == ("test", "-f", "/logs/verifier/reward.json"):
            return ExecResult(0, b"", b"")
        return await original(handle, proc, attempt=attempt)

    async def read(handle: InstanceHandle, path: str, **kwargs: Any) -> bytes:
        if path == "/logs/verifier/reward.json":
            return b'{"reward": 1}'
        return await original_read(handle, path, **kwargs)

    monkeypatch.setattr(platform, "exec", execute)
    monkeypatch.setattr(platform, "read_file", read)
    return helper_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("document", ["README.md", "README_zh.md", "docs/Quick_Start.md"])
async def test_documented_example_runs(
    deployment: tuple[FlotillaConfig, FakePlatform, SharedTree],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document: str,
) -> None:
    helper_calls = documented_example(deployment, tmp_path, monkeypatch)
    blocks = re.findall(r"```python\n(.*?)```", (ROOT / document).read_text(), re.S)
    [code] = [block for block in blocks if "async def main" in block]
    namespace: dict[str, Any] = {}
    exec(compile(code.replace("asyncio.run(main())", ""), document, "exec"), namespace)
    await namespace["main"]()
    # The anchor clears a mounted output directory with one busybox find call.
    expected = ("/.flotilla/bin/busybox", "find", "...", "-mindepth", "1", "-delete")
    assert all(argv[:2] == expected[:2] and argv[3:] == expected[3:] for argv in helper_calls)
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
