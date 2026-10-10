"""预构建镜像的查找与一致性校验（§4.2 第 3 步）。

history 是 Docker 写下的摘要而非 Dockerfile 原文，有几处有损；这里既覆盖"有损但一致应通过"，
也覆盖"真不一致必须拒绝"——放宽不能放宽到失去意义。
"""

from __future__ import annotations

from typing import Any

import pytest

from flotilla.build.prebuilt import (
    PrebuiltMismatch,
    PrebuiltSettings,
    dockerfile_instructions,
    verify,
)


def _hist(*created_by: str) -> list[dict[str, Any]]:
    return [{"created_by": c} for c in created_by]


# ───────────────────────────── 引用模板 ─────────────────────────────


def test_ref_for_substitutes_task() -> None:
    s = PrebuiltSettings(reference="r.io/ns/tb2:{task}-20251031")
    assert s.ref_for("write-compressor") == "r.io/ns/tb2:write-compressor-20251031"
    assert PrebuiltSettings().ref_for("x") is None  # 未配置


def test_reference_must_contain_placeholder() -> None:
    with pytest.raises(ValueError, match="task"):
        PrebuiltSettings(reference="r.io/ns/tb2:fixed")


# ───────────────────────────── Dockerfile 解析 ─────────────────────────────


def test_instructions_skip_comments_from_and_earlier_stages() -> None:
    df = """
    # comment with FROM nope:1
    FROM base:1 AS build
    RUN make
    FROM runtime:2
    WORKDIR /app
    COPY a.txt /app
    """
    # 多阶段只取最后一个阶段；FROM 本身不进（history 里没有它）。
    assert dockerfile_instructions(df) == [("WORKDIR", "/app"), ("COPY", "a.txt /app")]


def test_instructions_join_continuations() -> None:
    df = "FROM b\nRUN apt-get update && \\\n    apt-get install -y gcc\n"
    assert dockerfile_instructions(df) == [("RUN", "apt-get update && apt-get install -y gcc")]


# ───────────────────────────── history 的有损之处（一致，应通过）─────────────────────────────


def test_verify_accepts_run_and_workdir() -> None:
    df = "FROM b\nWORKDIR /app\nRUN gcc -O3 decomp.c -o /app/decomp\n"
    verify(df, _hist("WORKDIR /app", "RUN /bin/sh -c gcc -O3 decomp.c -o /app/decomp # buildkit"))


def test_verify_ignores_base_image_layers_before_the_tail() -> None:
    df = "FROM b\nWORKDIR /app\n"
    # 基础镜像自己也可能是 buildkit 构建的，前面的层一律不参与比对。
    verify(df, _hist("RUN /bin/sh -c base-stuff # buildkit", "ENV PYTHON_VERSION=3.13", "WORKDIR /app"))


def test_verify_tolerates_copy_from_flag() -> None:
    df = "FROM b\nCOPY --from=ghcr.io/astral-sh/uv:0.8.14 /uv /uvx /bin/\n"
    verify(df, _hist("COPY /uv /uvx /bin/ # buildkit"))


def test_verify_tolerates_arg_prefix_on_run() -> None:
    df = 'FROM b\nARG BN_URL=https://example.com/d.csv\nRUN curl -fsSL "${BN_URL}" -o d.csv\n'
    verify(
        df,
        _hist(
            "ARG BN_URL=https://example.com/d.csv",
            'RUN |1 BN_URL=https://example.com/d.csv /bin/sh -c curl -fsSL "${BN_URL}" -o d.csv # buildkit',
        ),
    )


def test_verify_tolerates_expanded_env_value() -> None:
    df = "FROM b\nENV PYTHONPATH=/app:$PYTHONPATH\n"
    verify(df, _hist("ENV PYTHONPATH=/app:"))  # $VAR 已展开成空


def test_verify_tolerates_exec_form_without_commas() -> None:
    df = 'FROM b\nCMD ["supervisord","-c","/etc/supervisord.conf"]\n'
    verify(df, _hist('CMD ["supervisord" "-c" "/etc/supervisord.conf"]'))


def test_verify_passes_when_dockerfile_only_has_from() -> None:
    verify("FROM base:1\n", _hist("RUN /bin/sh -c anything # buildkit"))


# ───────────────────────────── 真不一致（必须拒绝）─────────────────────────────

REAL = "FROM b\nWORKDIR /app\nCOPY decomp.c /app\nRUN gcc -O3 decomp.c -o /app/decomp\nCOPY data.txt /app\n"
HIST = _hist(
    "WORKDIR /app",
    "COPY decomp.c /app # buildkit",
    "RUN /bin/sh -c gcc -O3 decomp.c -o /app/decomp # buildkit",
    "COPY data.txt /app # buildkit",
)


def test_verify_accepts_the_matching_dockerfile() -> None:
    verify(REAL, HIST)


@pytest.mark.parametrize(
    ("what", "dockerfile"),
    [
        ("COPY 源改名", REAL.replace("COPY decomp.c /app", "COPY decomp_evil.c /app")),
        ("RUN 参数变化", REAL.replace("gcc -O3", "gcc -O0")),
        ("少一条指令", REAL.replace("COPY data.txt /app", "")),
        ("多一条指令", REAL + "RUN echo backdoor > /tmp/x\n"),
        ("WORKDIR 变化", REAL.replace("WORKDIR /app", "WORKDIR /srv")),
        (
            "指令顺序交换",
            "FROM b\nCOPY decomp.c /app\nWORKDIR /app\nRUN gcc -O3 decomp.c -o /app/decomp\nCOPY data.txt /app\n",
        ),
        ("完全是另一个任务", "FROM b\nRUN apt-get install -y r-base\n"),
    ],
)
def test_verify_rejects_real_differences(what: str, dockerfile: str) -> None:
    with pytest.raises(PrebuiltMismatch):
        verify(dockerfile, HIST)


def test_mismatch_message_shows_both_sides() -> None:
    with pytest.raises(PrebuiltMismatch) as exc:
        verify(REAL.replace("gcc -O3", "gcc -O0"), HIST)
    message = str(exc.value)
    assert "Dockerfile" in message and "镜像尾部" in message and "gcc -O0" in message
