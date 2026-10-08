"""能力报告：一个部署对 `Platform_Requirements.md` C1–C16 的满足情况（附录 A；Architecture §2.2）。

报告由 `flotilla probe` 写出（`deployments/<部署>.json`），provider `start` 与扫描器读入。数据边界用 Pydantic v2
（`extra="forbid"`）：报告跨机器传递、由人审阅，未知字段或类型不符在读入时报错。

每一项有**来源**：`probed` 由契约测试测得（带测得的证据），`declared` 由部署者书面声明（首版 probe 只自动测单服务
所需的项，其余由部署者填写，§5）。来源只用于报告与审阅，判定规则不因来源而异。

`to_capabilities()` 给出编排核心与扫描器使用的 `platform.base.Capabilities`；报告记录的其余项（C14 其余子项、C16、
首版不要求的项）只供评估，不进入 `Capabilities`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flotilla.platform.base import Capabilities

SCHEMA_VERSION = 1

Source = Literal["probed", "declared"]
ExternalForm = Literal["none", "any", "allowlist"]
Device = Literal["fuse", "kvm"]
#: 安全类能力（Platform_Requirements §2）：为假时 provider `start` 默认拒绝，须在配置的 `accept_insecure` 中逐项接受。
SECURITY_FIELDS = ("inbound_isolation", "exec_auth", "no_auto_mount")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Item(_Model):
    """一项能力的判定。`evidence` 是测得或声明时的说明（命令输出摘要、时长、文档引用），不进入判定。"""

    ok: bool
    source: Source
    evidence: str = ""


class Lifecycle(_Model):
    """C1。`list_visibility_s` 是部署声明的时限、probe 复核（测得的最大可见延迟写进 evidence）。"""

    create_get_delete: Item
    label_list: Item
    renew: Item
    max_label_value_len: Annotated[int, Field(ge=63)]
    list_visibility_s: Annotated[float, Field(gt=0)]
    max_ttl_seconds: Annotated[int, Field(gt=0)] | None


class Exec(_Model):
    """C2、C3。"""

    exec_identity: Item  # C2 第 2 条：argv、uid / gid、cwd、env、超时
    files: Item  # C2 第 3 条：上传、下载、/etc/hosts 可写且不被重写
    placeholder_entry: Item  # C2 第 4 条
    background: Item  # C3


class Network(_Model):
    """C4–C7、C13。"""

    link: Item  # C4 第 1、3、4 条
    link_udp: Item  # C4 第 2 条
    link_max_members: Annotated[int, Field(ge=1)]
    inbound_isolation: Item  # C5（安全）
    external_forms: frozenset[ExternalForm]  # C6
    external_none: Item  # C6 第 1、4 条（必需）
    internal_address: Literal["api", "exec", "none"]  # C7
    implicit_egress: tuple[str, ...]  # C13：部署声明的隐式放行（目标:端口/协议）
    implicit_egress_declared: Item  # C13（必需）：声明完整


class Storage(_Model):
    """C8。"""

    read_only: Item  # 第 1 条（必需）
    shared_volume: Item  # 第 2 条（功能）
    subdir_isolation: Item  # 第 3 条（必需）
    missing_subpath: Item  # 第 4 条（降级）
    no_auto_mount: Item  # 第 5 条（安全）
    dir_management: Item  # 第 6 条（必需）
    volume_file: Item  # 第 7 条（功能）


class Runtime(_Model):
    """C9、C10、C12、C14、C15、C16。"""

    exec_auth: Item  # C9（安全）
    memory_limit: Item  # C10 第 1 条（必需）：内存上限等于给定值
    cpu_limit: Item  # C10 第 2 条（降级）：CPU 上限不低于给定值；实际倍数记在 evidence 与 cpu_limit_ratio
    cpu_limit_ratio: Annotated[float, Field(gt=0)] | None  # CPU 实际上限 / 给定值；读不到时为 None
    diagnostics: Item  # C12（降级）
    privileged_runtime: Item  # C14
    devices: frozenset[Device]  # C14
    image_digest: Item  # C15（必需）
    multi_container: Item  # C16（首版接口不承载，只记录）


class CapabilityReport(_Model):
    schema_: Annotated[int, Field(alias="schema")] = SCHEMA_VERSION
    deployment: str  # 部署名（人读）
    endpoint: str  # 生命周期 API 基址；provider `start` 时与配置比对
    probed_at: datetime
    probe_version: str  # flotilla 版本
    lifecycle: Lifecycle
    exec: Exec
    network: Network
    storage: Storage
    runtime: Runtime
    notes: tuple[str, ...] = ()

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _consistent(self) -> CapabilityReport:
        if "none" not in self.network.external_forms and self.network.external_none.ok:
            raise ValueError("external_none.ok 为真时 external_forms 须含 none")
        if self.network.internal_address == "none" and self.network.link.ok:
            raise ValueError("取不到内部地址（internal_address = none）时 link 不能为真（C7 是互联的前提）")
        return self

    # ───────────────────────────── 判定 ─────────────────────────────

    def missing_required(self) -> list[str]:
        """必需项中不满足的（Platform_Requirements 附录 A）。非空时报告"不可用"，provider `start` 拒绝。"""
        lc, ex, net, st, rt = self.lifecycle, self.exec, self.network, self.storage, self.runtime
        required = {
            "C1 创建 / 查询 / 删除": lc.create_get_delete,
            "C1 按标签列出": lc.label_list,
            "C1 续期": lc.renew,
            "C2 执行": ex.exec_identity,
            "C2 文件": ex.files,
            "C2 占位入口": ex.placeholder_entry,
            "C3 后台进程": ex.background,
            "C6 none": net.external_none,
            "C8 只读挂载": st.read_only,
            "C8 子目录隔离": st.subdir_isolation,
            "C8 目录管理": st.dir_management,
            "C10 内存上限等于给定值": rt.memory_limit,
            "C13 隐式放行声明": net.implicit_egress_declared,
            "C15 按 digest 拉取": rt.image_digest,
        }
        missing = [name for name, item in required.items() if not item.ok]
        if not ({"any", "allowlist"} & net.external_forms):
            missing.append("C6 any 或 allowlist 至少其一")
        return missing

    def security_gaps(self) -> list[str]:
        """为假的安全类能力（`SECURITY_FIELDS` 的子集），须在配置中显式接受。"""
        values = {
            "inbound_isolation": self.network.inbound_isolation.ok,
            "exec_auth": self.runtime.exec_auth.ok,
            "no_auto_mount": self.storage.no_auto_mount.ok,
        }
        return [name for name in SECURITY_FIELDS if not values[name]]

    def to_capabilities(self) -> Capabilities:
        net = self.network
        return Capabilities(
            max_label_value_len=self.lifecycle.max_label_value_len,
            list_visibility_s=self.lifecycle.list_visibility_s,
            max_ttl_seconds=self.lifecycle.max_ttl_seconds,
            link=net.link.ok,
            link_udp=net.link.ok and net.link_udp.ok,
            link_max_members=net.link_max_members,
            inbound_isolation=net.inbound_isolation.ok,
            external_forms=frozenset(net.external_forms),
            implicit_egress=net.implicit_egress,
            internal_address=net.internal_address,
            shared_volume=self.storage.shared_volume.ok,
            volume_file=self.storage.volume_file.ok,
            no_auto_mount=self.storage.no_auto_mount.ok,
            exec_auth=self.runtime.exec_auth.ok,
            privileged_runtime=self.runtime.privileged_runtime.ok,
            devices=frozenset(self.runtime.devices) if self.runtime.privileged_runtime.ok else frozenset(),
        )

    # ───────────────────────────── 读写 ─────────────────────────────

    def dump_json(self) -> str:
        return self.model_dump_json(by_alias=True, indent=2) + "\n"

    @classmethod
    def load_json(cls, text: str | bytes) -> CapabilityReport:
        report = cls.model_validate_json(text)
        if report.schema_ != SCHEMA_VERSION:
            raise ValueError(f"能力报告 schema 版本 {report.schema_} 不受支持（需要 {SCHEMA_VERSION}）")
        return report
