"""`Registry` 与 `Builder` 的 docker 实现（Architecture §4.2 第 3、5、8 步）。

- `DockerRegistry`：`docker buildx imagetools` 查 digest、读 image config、按 digest 复制。`imagetools` 只和
  registry 打交道，不把镜像层拉到本地（`inspect` 读 manifest 与 config blob，`create` 在 registry 之间复制）；
- `DockerBuilder`：`docker buildx build --push` 构建并推送，从 metadata 文件取推送后的 digest。

命令、超时、凭证（docker config 目录）都可注入，便于测试与适配不同部署的 docker 调用方式。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flotilla.build.builder import BuildRequest
from flotilla.build.images import _DIGEST, ImageError


def _docker(args: Sequence[str], *, docker_cmd: Sequence[str], timeout_s: float) -> str:
    """跑一条 docker 命令，返回 stdout。失败按 `ImageError` 报出（含 stderr 摘要）。"""
    cmd = [*docker_cmd, *args]
    try:
        # 参数由本模块拼装，不经 shell；镜像引用来自已解析的 ImageRef 与配置的 target。
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImageError(f"docker 命令失败（{' '.join(args)}）：{exc}") from exc
    if result.returncode != 0:
        raise ImageError(f"docker 命令退出 {result.returncode}（{' '.join(args)}）：{result.stderr.strip()[:300]}")
    return result.stdout


@dataclass(frozen=True)
class DockerRegistry:
    """用 `docker buildx imagetools` 实现 `Registry`。

    `docker_cmd` 是 docker 可执行与前置参数，例如 `("sudo", "docker", "--config", "/home/u/.docker")`。
    `timeout_s` 作用于单条命令。
    """

    docker_cmd: Sequence[str] = field(default_factory=lambda: ("docker",))
    timeout_s: float = 300.0

    def digest(self, name: str, reference: str) -> str:
        ref = f"{name}@{reference}" if reference.startswith("sha256:") else f"{name}:{reference}"
        out = self._run(["buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest.Digest}}"])
        digest = out.strip().strip('"')
        if not _DIGEST.fullmatch(digest):  # 老 docker 不认 --format：从默认输出里挑 `Digest:` 行
            digest = next(
                (line.split(":", 1)[1].strip() for line in out.splitlines() if line.strip().startswith("Digest:")),
                "",
            )
        if not _DIGEST.fullmatch(digest):
            raise ImageError(f"无法从 {ref} 解析 digest（imagetools 输出：{out[:200]!r}）")
        return digest

    def image_config(self, pinned: str) -> Mapping[str, Any]:
        """镜像的 image config 的 `config` 段（§4.2 第 5 步）。只读 manifest 与 config blob，不拉镜像层。"""
        out = self._run(["buildx", "imagetools", "inspect", pinned, "--format", "{{json .Image}}"])
        try:
            image = json.loads(out)
        except ValueError as exc:
            raise ImageError(f"无法解析 {pinned} 的 image config：{exc}") from exc
        # 多架构镜像时 `.Image` 是 平台 → config 的映射；取 linux/amd64（构建与运行都是 x86_64）。
        if isinstance(image, Mapping) and "config" not in image:
            for key in ("linux/amd64", *sorted(image)):
                entry = image.get(key)
                if isinstance(entry, Mapping) and "config" in entry:
                    image = entry
                    break
        config = image.get("config") if isinstance(image, Mapping) else None
        if not isinstance(config, Mapping):
            raise ImageError(f"{pinned} 的 image config 里没有 config 段：{out[:200]!r}")
        return config

    def copy(self, source: str, target: str) -> None:
        self._run(["buildx", "imagetools", "create", "--tag", target, source])

    def _run(self, args: Sequence[str]) -> str:
        return _docker(args, docker_cmd=self.docker_cmd, timeout_s=self.timeout_s)


@dataclass(frozen=True)
class DockerBuilder:
    """用 `docker buildx build --push` 实现 `Builder`（§4.2 第 3、8 步）。

    构建完成即推送，digest 从 `--metadata-file` 读（`containerimage.digest`），不靠解析日志。
    """

    docker_cmd: Sequence[str] = field(default_factory=lambda: ("docker",))
    timeout_s: float = 1800.0
    platform: str = "linux/amd64"

    def build(self, request: BuildRequest) -> str:
        with tempfile.TemporaryDirectory(prefix="flotilla-build-") as tmp:
            metadata = Path(tmp) / "metadata.json"
            args = [
                "buildx",
                "build",
                "--push",
                "--platform",
                self.platform,
                "--metadata-file",
                str(metadata),
                "--tag",
                request.tag,
            ]
            if request.dockerfile_text is not None:
                dockerfile = Path(tmp) / "Dockerfile"
                dockerfile.write_text(request.dockerfile_text)
                args += ["--file", str(dockerfile)]
            else:
                assert request.dockerfile is not None  # BuildRequest 已校验二选一
                args += ["--file", str(request.context / request.dockerfile)]
            for key, value in sorted(request.args.items()):
                args += ["--build-arg", f"{key}={value}"]
            args.append(str(request.context))
            self._run(args)
            return self._digest_from(metadata, request)

    @staticmethod
    def _digest_from(metadata: Path, request: BuildRequest) -> str:
        try:
            data = json.loads(metadata.read_text())
        except (OSError, ValueError) as exc:
            raise ImageError(f"服务 {request.service}：读不到构建的 metadata：{exc}") from exc
        digest = data.get("containerimage.digest")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ImageError(f"服务 {request.service}：构建 metadata 里没有可用的 digest：{data!r}")
        return digest

    def _run(self, args: Sequence[str]) -> str:
        return _docker(args, docker_cmd=self.docker_cmd, timeout_s=self.timeout_s)
