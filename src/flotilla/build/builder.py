"""BuildKit 构建与派生、镜像命名（Architecture §4.2 第 3、7、8 步、§4.3）。

两种产出镜像的方式：

- **build**：服务有 `build`，用它的上下文与 Dockerfile 构建；
- **derive**：服务是现成 `image`，但要改镜像（§4.2 第 7 步：bind 写入目标路径、`nocopy` 清空、补 `/bin/sh`）——
  合成一个 `FROM <源镜像>` 的 Dockerfile，只加改动的那一层，不重建原镜像。

两者都经 `Builder` 推到使用方的仓库，返回 digest。镜像命名见 §4.3：tag 只供人看与盘点，运行期一律按 digest 拉。
Dockerfile 一律用 exec 形式（`RUN ["/bin/sh","-c",…]`、`COPY ["src","dst"]`），不经镜像的 shell 解析。
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from flotilla.build.images import ImageError

MAX_TAG = 128
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def image_tag(target: str, task: str, service: str, build_key: str) -> str:
    """`<target>:<task-slug>--<service>--<buildkey12>`（§4.3）。

    tag 总长不超过 128；`task-slug` 过长时截断并附内容哈希，保证不同任务不会撞到同一个 tag。
    """
    suffix = f"--{_slug(service)}--{build_key[:12]}"
    budget = MAX_TAG - len(suffix)
    if budget < 8:
        raise ImageError(f"服务名过长，无法生成 tag：{service!r}")
    return f"{target}:{_slug(task, budget)}{suffix}"


def _slug(text: str, budget: int | None = None) -> str:
    """规范化为 tag 允许的字符；超出 `budget` 时截断并附哈希（§4.3）。"""
    slug = _UNSAFE.sub("-", text).strip("-._") or "x"
    if budget is None or len(slug) <= budget:
        return slug
    digest = hashlib.sha256(text.encode()).hexdigest()[:8]
    return f"{slug[: budget - 9].rstrip('-._')}-{digest}"


@dataclass(frozen=True)
class BuildRequest:
    """一次镜像构建。

    `context` 是构建上下文目录；derive 时上下文只含要写入镜像的 bind 源（没有要拷的文件时可以是空目录）。
    `dockerfile` 与 `dockerfile_text` 二选一：前者是上下文内的路径（build），后者是合成的内容（derive）。
    """

    service: str
    tag: str
    context: Path
    dockerfile: str | None = None
    dockerfile_text: str | None = None
    args: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.dockerfile is None) == (self.dockerfile_text is None):
            raise ImageError(f"服务 {self.service}：dockerfile 与 dockerfile_text 须给且只给一个")


class Builder(Protocol):
    """BuildKit 构建器。构建并推送，返回推送后的 manifest digest（`sha256:…`）。"""

    def build(self, request: BuildRequest) -> str: ...


def derive_dockerfile(base: str, *, copy: Sequence[tuple[str, str]] = (), clear: Sequence[str] = ()) -> str:
    """派生层的 Dockerfile（§4.2 第 7 步）。

    - `clear`：`nocopy` 卷的挂载点——清空镜像中该路径下的原有内容，保留目录本身（§7.3）；
    - `copy`：(上下文内的源, 镜像内的目标)——bind 写入镜像目标路径。写入前先删除目标原有内容，使"目录遮蔽"
      也成立（§4.4）。

    删除与写入都用 exec 形式，不经镜像的 shell；镜像没有 `/bin/sh` 时 `clear` 用不了（那种镜像先补 shell 层）。
    """
    lines = [f"FROM {base}"]
    for target in clear:
        quoted = shlex.quote(target)
        lines.append(_run(f"rm -rf {quoted}/* {quoted}/.[!.]* 2>/dev/null; mkdir -p {quoted}"))
    for source, target in copy:
        lines.append(_run(f"rm -rf {shlex.quote(target)}"))
        lines.append(f"COPY [{json.dumps(source)}, {json.dumps(target)}]")
    return "\n".join(lines) + "\n"


def shell_dockerfile(base: str, busybox_image: str) -> str:
    """补 `/bin/sh` 的派生层（§3.4）：从一个自带静态 busybox 的镜像拷进来，不依赖源镜像里的任何工具。"""
    return f'FROM {busybox_image} AS shell\nFROM {base}\nCOPY --from=shell ["/bin/busybox", "/bin/sh"]\n'


def _run(script: str) -> str:
    return f"RUN [{json.dumps('/bin/sh')}, {json.dumps('-c')}, {json.dumps(script)}]"
