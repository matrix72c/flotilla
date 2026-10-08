"""一个 trial 的输入：编排核心看到的任务形状（Architecture §3.5–3.7、§4.5、第 6、7 节）。

清单（`flotilla.manifest`）到 `TrialPlan` 的翻译在 core 之外完成——运行时参数代入、agent 叠加、`request.env_vars`
合并都在那里，core 只看到最终值。这样 core 只依赖 `platform.base`（§16.2），也不随清单 schema 的版本演进。

全部是不可变值；`validate()` 检查跨字段的一致性（引用的单元与网络存在、`depends_on` 无环等），
由 `TrialPlan` 的构造者在交给 core 之前调用。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from flotilla.platform.base import Resources

Condition = Literal["service_started", "service_healthy", "service_completed_successfully"]
RestartPolicy = Literal["no", "always", "unless-stopped", "on-failure"]


@dataclass(frozen=True)
class ExecContext:
    """一个服务的执行上下文（§3.5）：业务进程、健康检查与使用方 `execute` 都从它构造 `ProcessSpec`。"""

    env: Mapping[str, str]  # 镜像 ENV → env_file → environment，已代入运行时参数
    uid: int
    gid: int
    cwd: str


@dataclass(frozen=True)
class Healthcheck:
    """Docker 语义的健康检查（§3.6）。`test` 已归一为 argv：`CMD` 原样、`CMD-SHELL` 为 `["/bin/sh","-c",…]`。"""

    test: tuple[str, ...]
    interval_s: float = 30.0
    timeout_s: float = 30.0
    retries: int = 3
    start_period_s: float = 0.0
    start_interval_s: float = 5.0

    def __post_init__(self) -> None:
        if not self.test:
            raise ValueError("healthcheck.test 不能为空（NONE / disable 时不设 healthcheck）")
        if self.interval_s <= 0 or self.timeout_s <= 0 or self.retries < 1 or self.start_interval_s <= 0:
            raise ValueError("healthcheck 的间隔与超时须 > 0，retries 须 >= 1")
        if self.start_period_s < 0:
            raise ValueError("start_period_s 不能为负")


@dataclass(frozen=True)
class Mount:
    """单元的一个共享存储挂载（§4.4、§7）。

    `scope="task"`：`key` 相对已发布的任务文件目录（`tasks/<build_key>/…`），所有 trial 共用，只读。
    `scope="trial"`：`key` 相对本 trial 的目录（`launches/<launch>/<trial>/…`），由 prepare 阶段准备。
    share（§3.3）由编排核心给每个单元统一挂载，不在这里。
    """

    scope: Literal["task", "trial"]
    key: str
    target: str
    read_only: bool


@dataclass(frozen=True)
class Unit:
    """一个 sandbox：Compose 的一个服务（或一个副本）。"""

    image: str
    resources: Resources
    context: ExecContext
    command: tuple[str, ...] | None  # 业务进程 argv；None 表示没有业务进程（只有客户端执行的独立 sandbox）
    healthcheck: Healthcheck | None = None
    restart: RestartPolicy = "no"
    mounts: tuple[Mount, ...] = ()
    privileged: bool = False
    devices: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Network:
    """一个 Compose 网络（§6.2）。`names`：名字 → 服务，生成每个单元的 hosts（§3.7）。"""

    internal: bool
    members: frozenset[str]
    names: Mapping[str, frozenset[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Dependency:
    condition: Condition
    required: bool = True


@dataclass(frozen=True)
class TrialVolume:
    """本 trial 的一个共享卷目录（§7.1 第三行、§4.4 有写者的多引用 bind 源组）。

    prepare 阶段在 `launches/<launch>/<trial>/<key>` 创建它：有 `seed` 时把任务文件中的 tar 解包进去，
    有 `copy_from` 时从任务文件复制（保留属主与权限），都没有时建空目录并按 `owner` chown。
    """

    key: str
    owner: tuple[int, int] | None = None
    seed: str | None = None  # 相对任务文件目录的 tar
    copy_from: str | None = None  # 相对任务文件目录的源目录

    def __post_init__(self) -> None:
        if self.seed is not None and self.copy_from is not None:
            raise ValueError("seed 与 copy_from 不能同时给出")


@dataclass(frozen=True)
class TrialPlan:
    """一个 trial 的全部输入。单元名即 Compose 服务名（副本另起名），也是 `link` 的成员名。"""

    task: str  # 任务 slug，写入 `flotilla/task` 标签（截断）
    task_files: str | None  # 已发布的任务文件目录（相对共享根目录）；没有任务文件时为 None
    units: Mapping[str, Unit]
    networks: Mapping[str, Network] = field(default_factory=dict)
    depends_on: Mapping[str, Mapping[str, Dependency]] = field(default_factory=dict)
    extra_hosts: Mapping[str, Mapping[str, str]] = field(default_factory=dict)  # 单元 → 名字 → 地址
    volumes: tuple[TrialVolume, ...] = ()
    external_units: frozenset[str] = frozenset()  # 按调用方策略获得外部访问的单元（§6.4）

    def validate(self) -> None:
        """检查跨字段的一致性；不一致时抛 `ValueError`（清单或翻译有误，不是平台问题）。"""
        units = set(self.units)
        if not units:
            raise ValueError("trial 至少要有一个单元")
        for name, net in self.networks.items():
            _require_subset(net.members, units, f"网络 {name} 的成员")
            for host, targets in net.names.items():
                _require_subset(targets, net.members, f"网络 {name} 中名字 {host} 指向的服务")
        _require_subset(self.external_units, units, "external_units")
        _require_subset(set(self.extra_hosts), units, "extra_hosts 的单元")
        _require_subset(set(self.depends_on), units, "depends_on 的单元")
        for dependent, deps in self.depends_on.items():
            # required: false 的依赖可以缺席（例如 profile 未选中），不阻塞（§5.3）；必需的依赖必须存在。
            _require_subset({d for d, dep in deps.items() if dep.required}, units, f"{dependent} 的依赖")
            if dependent in deps:
                raise ValueError(f"{dependent} 依赖自己")
            for dep, d in deps.items():
                if dep not in self.units:
                    continue
                # Compose 对这两种条件的被依赖者有前提，不满足时 `docker compose up` 直接报错。
                if d.condition == "service_healthy" and self.units[dep].healthcheck is None:
                    raise ValueError(f"{dependent} 以 service_healthy 依赖 {dep}，但 {dep} 没有健康检查")
                if d.condition == "service_completed_successfully" and self.units[dep].command is None:
                    raise ValueError(f"{dependent} 等待 {dep} 完成，但 {dep} 没有业务进程")
        _require_acyclic(self.depends_on)
        if self.task_files is not None:
            _require_key(self.task_files, "task_files", allow_empty=False)
        keys = [v.key for v in self.volumes]
        for key in keys:
            _require_key(key, "trial 卷的 key", allow_empty=False)
        if len(keys) != len(set(keys)):
            raise ValueError("trial 卷的 key 重复")
        for name, unit in self.units.items():
            for m in unit.mounts:
                _require_key(m.key, f"{name} 的挂载 {m.target}", allow_empty=False)
                if m.scope == "trial" and not any(m.key == k or m.key.startswith(k + "/") for k in keys):
                    raise ValueError(f"{name} 挂载了未声明的 trial 卷 {m.key}")
                if m.scope == "task":
                    if self.task_files is None:
                        raise ValueError(f"{name} 挂载任务文件，但任务没有已发布的任务文件")
                    if not m.read_only:
                        raise ValueError(f"{name} 以读写挂载任务文件 {m.key}：任务文件只读、所有 trial 共用（§4.4）")
            if unit.restart != "no" and unit.command is None:
                raise ValueError(f"{name} 没有业务进程，却声明了 restart 策略")
            if unit.healthcheck is not None and unit.command is None:
                raise ValueError(f"{name} 没有业务进程，却声明了健康检查")
        if any(v.seed is not None or v.copy_from is not None for v in self.volumes) and self.task_files is None:
            raise ValueError("trial 卷需要任务文件中的种子或源，但任务没有已发布的任务文件")


def _require_key(key: str, what: str, *, allow_empty: bool) -> None:
    """相对共享根目录的路径：不含空段、`.`、`..`，不以 `/` 开头（§7.2）。空串即共享根目录本身，这里一律拒绝。"""
    if not key:
        if allow_empty:
            return
        raise ValueError(f"{what}为空：不能挂载或操作共享根目录本身")
    if any(p in ("", ".", "..") for p in key.split("/")):
        raise ValueError(f"{what}不是合法的相对路径：{key!r}")


def _require_acyclic(depends_on: Mapping[str, Mapping[str, Dependency]]) -> None:
    state: dict[str, int] = {}  # 1 = 访问中，2 = 完成

    def visit(node: str, path: list[str]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            cycle = [*path[path.index(node) :], node]
            raise ValueError(f"depends_on 成环：{' → '.join(cycle)}")
        state[node] = 1
        for dep in sorted(depends_on.get(node, {})):
            visit(dep, [*path, node])
        state[node] = 2

    for node in sorted(depends_on):
        visit(node, [])


def _require_subset(items: frozenset[str] | set[str], universe: set[str] | frozenset[str], what: str) -> None:
    unknown = set(items) - set(universe)
    if unknown:
        raise ValueError(f"{what}引用了不存在的单元或服务：{sorted(unknown)}")
