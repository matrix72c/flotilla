"""预构建镜像：按任务查找，并校验它与任务 Dockerfile 一致（Architecture §4.2 第 3 步）。

数据集常常已经把每个任务的环境构建好推到仓库里（例如 TB2 的 `<repo>:<task>-<date>`）。这种任务不需要再构建：
按任务名拼出引用、解析 digest，就变成 link。

**必须校验**：镜像与任务 Dockerfile 脱钩时，训练环境就不是任务声明的那个环境。校验的办法是比对镜像 history 的
**尾部**与 Dockerfile 的指令序列——任务的那几条指令是在基础镜像之上最后加的。

校验的范围要说清楚：它比对的是**指令序列**（WORKDIR / RUN 的命令、COPY 的参数），不是文件内容。同名但内容不同的
COPY 源查不出来，所以清单里同时记下镜像 digest，让用的是哪个镜像可追溯。`# buildkit` 后缀不能用来区分任务层与
基础镜像层——官方的 python 等镜像自己也是 buildkit 构建的。

history 是 Docker 写下的摘要，不是 Dockerfile 原文，几处已知的有损之处必须容忍（否则把一致的镜像误判为不一致）：

- `COPY --from=…` 丢掉 `--from` 标志，只留源与目标；
- `ARG` 会留下一条记录，并给后续 `RUN` 加 `|<n> VAR=value …` 前缀；
- `ENV`、`ARG` 的值里 `$VAR` 已按构建时的值展开（`/app:$PYTHONPATH` → `/app:`）；
- 多阶段构建只有最后一个阶段进入镜像，前面阶段的指令不在 history 里；
- exec 形式的 `CMD` / `ENTRYPOINT` 在 history 里丢掉了元素之间的逗号（`["a","b"]` → `["a" "b"]`）。

这些都放宽；指令种类与顺序、RUN 的命令、COPY 的源与目标仍严格比对。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: history 里 buildkit 给每层加的后缀。
_SUFFIX = re.compile(r"\s*#\s*buildkit\s*$")
_NOP = "#(nop)"
#: Dockerfile 里会在 history 留下一条记录的指令（`FROM` 不留，`ARG` / `LABEL` 等按 nop 跳过）。
_KINDS = ("RUN", "COPY", "ADD", "WORKDIR", "ENV", "ARG", "CMD", "ENTRYPOINT", "USER", "EXPOSE", "VOLUME")


class PrebuiltMismatch(ValueError):
    """预构建镜像与任务 Dockerfile 不一致。构建时以 `invalid` 拒绝该任务，不静默使用。"""


@dataclass(frozen=True)
class PrebuiltSettings:
    """部署配置 `[build.prebuilt]`。

    `reference` 是带 `{task}` 占位的模板，例如
    `registry.example.com/ns/terminal_bench_2:{task}-20251031`；`{task}` 代入任务目录名。
    `verify` 为 false 时跳过指令序列校验（只在镜像与任务确实不同源、且负责人接受时用）。
    """

    reference: str = ""
    verify: bool = True

    def __post_init__(self) -> None:
        if self.reference and "{task}" not in self.reference:
            raise ValueError(f"[build.prebuilt].reference 须含 {{task}} 占位：{self.reference!r}")

    def ref_for(self, task_dir: str) -> str | None:
        """该任务的预构建镜像引用；没有配置时为 None。"""
        return self.reference.format(task=task_dir) if self.reference else None


def normalize(text: str) -> str:
    """指令参数的规范化：去掉续行、把空白串压成单个空格。

    Dockerfile 的 `cmd && \\\\\\n    more` 在 history 里是 `cmd &&     more`（续行消失、缩进留下），所以两边都压一次
    空白才能比。
    """
    joined = re.sub(r"\\\s*\n", " ", text)
    return " ".join(joined.split())


def dockerfile_instructions(dockerfile_text: str) -> list[tuple[str, str]]:
    """Dockerfile **最后一个阶段**的 [(指令, 规范化参数)]，跳过注释、空行与 `FROM`（不在 history 里留记录）。

    多阶段构建只有最后阶段进入镜像，所以每遇到 `FROM` 就重新开始收集。
    """
    out: list[tuple[str, str]] = []
    for raw in _logical_lines(dockerfile_text):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, _, rest = line.partition(" ")
        kind = head.upper()
        if kind == "FROM":
            out.clear()
        elif kind in _KINDS:
            out.append((kind, _canon(kind, rest)))
    return out


def _canon(kind: str, args: str) -> str:
    """参数规范化，并抹掉 history 记不下来的部分（见模块说明）。"""
    text = normalize(args)
    if kind in ("COPY", "ADD"):
        # `--from=…` 等标志在 history 里不保留；`--chown` 之类同理，一律去掉前导标志。
        parts = [p for p in text.split(" ") if not p.startswith("--")]
        return " ".join(parts)
    if kind in ("ENV", "ARG"):
        # 值里的 `$VAR` 在 history 里已展开，比不了：只比变量名。
        return " ".join(p.split("=", 1)[0] for p in text.split(" ") if p)
    if kind in ("CMD", "ENTRYPOINT"):
        # exec 形式在 history 里丢了逗号：按 JSON 数组的元素比，顺带归一两种写法。
        return text.replace(",", " ").replace('" "', '","')
    return text


def _logical_lines(text: str) -> list[str]:
    """按续行合并后的逻辑行。"""
    return re.sub(r"\\\s*\n", " ", text).splitlines()


def history_instructions(history: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """image config 的 history → [(指令, 规范化参数)]，跳过 `#(nop)` 的元数据层。"""
    out: list[tuple[str, str]] = []
    for entry in history:
        created_by = entry.get("created_by")
        if not isinstance(created_by, str) or _NOP in created_by:
            continue
        text = _SUFFIX.sub("", created_by).strip()
        if text.startswith("RUN "):
            out.append(("RUN", _canon("RUN", _run_command(text[len("RUN ") :]))))
            continue
        head, _, rest = text.partition(" ")
        kind = head.upper()
        out.append((kind, _canon(kind, rest)) if kind in _KINDS else ("?", normalize(text)))
    return out


def _run_command(text: str) -> str:
    """`RUN` 记录里真正的命令。

    形式有两种：`/bin/sh -c <cmd>`（shell 形式），以及用了 `ARG` 时的
    `|<n> K=V … /bin/sh -c <cmd>`——前缀在 `/bin/sh -c` **之前**。exec 形式（`["a","b"]`）原样返回。
    """
    marker = "/bin/sh -c "
    head, sep, rest = text.partition(marker)
    if not sep:
        return text
    if head and not re.fullmatch(r"\|\d+\s+.*", head, flags=re.S):
        return text  # `/bin/sh -c` 出现在命令内部而不是开头：别乱切
    return rest


def verify(dockerfile_text: str, history: Sequence[Mapping[str, Any]]) -> None:
    """校验预构建镜像的 history 尾部与任务 Dockerfile 的指令序列一致，不一致抛 `PrebuiltMismatch`。

    只比尾部：前面的层属于基础镜像。Dockerfile 没有可比指令（只有 `FROM`）时视为通过——那种任务的镜像就是基础镜像。
    """
    want = dockerfile_instructions(dockerfile_text)
    if not want:
        return
    got = history_instructions(history)
    tail = got[-len(want) :] if len(got) >= len(want) else got
    if tail != want:
        raise PrebuiltMismatch(
            f"预构建镜像与任务 Dockerfile 的指令序列不一致：\n  Dockerfile：{_fmt(want)}\n  镜像尾部：  {_fmt(tail)}"
        )


def _fmt(items: Sequence[tuple[str, str]]) -> str:
    return " | ".join(f"{kind} {args[:60]}" for kind, args in items) or "（空）"
