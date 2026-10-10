"""`Registry` 的 docker 实现（Architecture §4.2 第 3 步）：`docker buildx imagetools` 查 digest、按 digest 复制。

`imagetools` 只和 registry 打交道，不把镜像层拉到本地：`inspect` 读 manifest，`create` 在 registry 之间按 digest
复制。命令、超时、凭证（docker config 目录）都可注入，便于测试与适配不同部署的 docker 调用方式。
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field

from flotilla.build.images import _DIGEST, ImageError


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

    def copy(self, source: str, target: str) -> None:
        self._run(["buildx", "imagetools", "create", "--tag", target, source])

    def _run(self, args: Sequence[str]) -> str:
        cmd = [*self.docker_cmd, *args]
        try:
            # 参数由本模块拼装，不经 shell；source/target 来自已解析的 ImageRef 与配置的 target。
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_s, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ImageError(f"docker 命令失败（{' '.join(args)}）：{exc}") from exc
        if result.returncode != 0:
            raise ImageError(f"docker 命令退出 {result.returncode}（{' '.join(args)}）：{result.stderr.strip()[:300]}")
        return result.stdout
