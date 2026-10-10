"""`FlotillaConfig`：所有运行期配置的唯一入口（Architecture §16.3）。

配置可以写成 TOML 文件，也可以由使用方（xtuner 配置）直接给 dict；数据边界用 Pydantic v2（`extra="forbid"`），
未知字段在读入时报错。后端由出现的后端段落决定（首版只有 `[opensandbox]`）。平台凭证只从环境变量读取
（`credential_env` 只是变量名），不出现在配置、清单与日志中（原则 8）。

本模块只做**不调用平台**的校验与翻译：

- 读入时的结构校验（字段、类型、取值范围）与本地一致性（`grace` / `gc_window` 等时序、`[network] external` 的写法）；
- `check_against(report)`：与能力报告比对（endpoint、必需项、外部策略形式、安全类缺口与 `accept_insecure`、
  `G + list_visibility_s < W`、`runtime_params` 落在 `[network] external` 内）；
- 翻译为 core 与后端的设置值（`AnchorSettings`、`ReaperSettings`、`TrialSettings`、`MonitorSettings`、
  OpenSandbox 的 `NetworkSettings` / `StorageSettings` / `ExecdSettings`）。

需要平台的启动校验（共享根目录存在、share 发布的 `RELEASE.json` sha256 一致）由 provider `start` 经锚点完成。
"""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from flotilla.capabilities import SECURITY_FIELDS, CapabilityReport
from flotilla.core.anchor import AnchorSettings
from flotilla.core.reaper import ReaperSettings
from flotilla.core.runtime import MonitorSettings
from flotilla.core.trial import TrialSettings
from flotilla.platform.base import ExternalPolicy, Resources
from flotilla.platform.opensandbox import ExecdSettings, NetworkSettings, StorageSettings

#: 锚点续期失败后的重试间隔（`AnchorSettings.retry_interval_s`），续期间隔不能比它短。
ANCHOR_RETRY_S = AnchorSettings(image="x").retry_interval_s
PositiveFloat = Annotated[float, Field(gt=0)]
PositiveInt = Annotated[int, Field(gt=0)]
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ConfigError(ValueError):
    """配置与部署不符，provider `start` 拒绝（§16.3）。消息列出全部问题，而不只是第一个。"""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("配置校验失败：\n- " + "\n- ".join(problems))
        self.problems = problems


class _Model(BaseModel):
    # hide_input_in_errors：校验错误不回显输入值——写错位置的凭证（例如未知字段 credential = "ak:sk"）
    # 不能经错误消息进入终端或日志（原则 8）。错误里仍有字段路径与原因。
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


# ───────────────────────────── [opensandbox] ─────────────────────────────


class OpenSandboxNetwork(_Model):
    link: Literal["cidr"] = "cidr"
    sandbox_cidrs: tuple[str, ...] = ()
    platform_cidrs: tuple[str, ...] = ()
    max_rules: PositiveInt = 4096

    @field_validator("sandbox_cidrs", "platform_cidrs")
    @classmethod
    def _cidrs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for cidr in value:
            ipaddress.ip_network(cidr, strict=False)
        return value


class OpenSandboxExecd(_Model):
    protocol: Literal["current", "legacy"] = "current"
    wrapper: str = ExecdSettings().wrapper


class OpenSandbox(_Model):
    endpoint: str
    credential_env: str
    capabilities: str  # 能力报告路径（`flotilla probe` 的产物）；相对路径相对配置文件所在目录
    extensions: dict[str, Any] = {}
    privileged_extensions: dict[str, Any] = {}
    network: OpenSandboxNetwork = OpenSandboxNetwork()
    execd: OpenSandboxExecd = OpenSandboxExecd()
    create_fields: dict[str, Any] = {}

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"endpoint 须为 http(s) URL：{value!r}")
        return value.rstrip("/")

    @field_validator("credential_env")
    @classmethod
    def _credential_env(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            # 不回显值：这里最常见的错误恰恰是把凭证本身填了进来。
            raise ValueError("credential_env 须为环境变量名（[A-Za-z_][A-Za-z0-9_]*），凭证本身不写进配置")
        return value


# ───────────────────────────── 通用段落 ─────────────────────────────


class Storage(_Model):
    volumes: Literal["host", "pvc"] = "host"
    host_path: str = ""
    claim_name: str = ""
    root_subpath: str = ""

    @model_validator(mode="after")
    def _settings(self) -> Storage:
        self.settings()  # 抛 ValueError
        return self

    def settings(self) -> StorageSettings:
        return StorageSettings(
            volumes=self.volumes, host_path=self.host_path, claim_name=self.claim_name, root_subpath=self.root_subpath
        )


class Share(_Model):
    release: str
    release_sha256: str

    @field_validator("release")
    @classmethod
    def _release(cls, value: str) -> str:
        if not _RELEASE.fullmatch(value):
            raise ValueError(f"share.release 须为单个路径段（字母数字与 ._-）：{value!r}")
        return value

    @field_validator("release_sha256")
    @classmethod
    def _sha(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("share.release_sha256 须为 64 位小写十六进制")
        return value


class Network(_Model):
    """`external`：`"any"`，或窄列表（IP / CIDR / 域名）。窄列表编译为 `allowlist`；空列表即 `none`（§6.4）。"""

    external: Literal["any"] | tuple[str, ...] = ()

    @field_validator("external")
    @classmethod
    def _narrow(cls, value: str | tuple[str, ...]) -> str | tuple[str, ...]:
        if isinstance(value, str):
            return value
        for target in value:
            if target.lower() == "any":
                raise ValueError('任意外网写作 external = "any"（字符串），不是列表中的一项')
            net = _as_network(target)
            if net is not None and net.prefixlen == 0:
                raise ValueError(f'窄列表中出现任意外网写法 {target}：请改用 external = "any"（§6.4）')
            if net is None and not _is_hostname(target):
                raise ValueError(f"external 的目标须为 IP / CIDR 或域名：{target!r}")
        return value

    def policy(self) -> ExternalPolicy:
        if self.external == "any":
            return ExternalPolicy(mode="any")
        if not self.external:
            return ExternalPolicy(mode="none")
        cidrs = tuple(t for t in self.external if _as_network(t) is not None)
        hosts = tuple(t for t in self.external if _as_network(t) is None)
        return ExternalPolicy(mode="allowlist", hosts=hosts, cidrs=cidrs)


class Security(_Model):
    accept_insecure: frozenset[Literal["inbound_isolation", "exec_auth", "no_auto_mount"]] = frozenset()


class AgentOverlay(_Model):
    env: dict[str, str] = {}
    services: tuple[str, ...] | None = None


class Limits(_Model):
    create_rate: PositiveFloat = 5.0
    create_concurrency: PositiveInt = 64
    trial_concurrency: PositiveInt = 512
    reap_concurrency: PositiveInt = 16
    exec_rate: PositiveFloat = 500.0
    exec_timeout: PositiveFloat = 3600.0


class Timeouts(_Model):
    create: PositiveFloat = 600.0
    address: PositiveFloat = 30.0
    wire: PositiveFloat = 60.0
    hosts: PositiveFloat = 30.0
    prepare: PositiveFloat = 600.0
    start_max: PositiveFloat = 1800.0
    close: PositiveFloat = 20.0


class Process(_Model):
    poll_interval: PositiveFloat = 2.0


class Reaper(_Model):
    anchor_image: str = ""  # 锚点镜像（repo@sha256:…，自带 /.flotilla/bin/busybox）；provider 使用前必填
    anchor_ttl: PositiveInt = 600
    renew_interval: PositiveFloat = 30.0
    grace: PositiveFloat = 90.0  # G
    gc_window: PositiveFloat = 180.0  # W
    gc_interval: PositiveFloat = 600.0
    trial_ttl: PositiveInt = 6 * 3600  # 每个实例的平台 TTL（§5.6），长 trial 续期

    @model_validator(mode="after")
    def _timing(self) -> Reaper:
        # 续期间隔"明显小于" G：至少留两次续期机会（§5.6）。G + list_visibility_s < W 要用能力报告，在 check_against。
        ok = self.renew_interval >= ANCHOR_RETRY_S and self.renew_interval * 2 <= self.grace < self.anchor_ttl
        if not ok:
            raise ValueError(
                f"须满足 renew_interval ≥ {ANCHOR_RETRY_S}、renew_interval × 2 ≤ grace < anchor_ttl（§5.6）"
            )
        if self.gc_window <= self.grace:
            raise ValueError("gc_window（W）须大于 grace（G）（§5.6）")
        return self


class Resource(_Model):
    cpu: str
    memory: str


def _default_resources() -> dict[str, Resource]:
    """§16.3 的占位值，按 M0 压测与抽样校准。"""
    return {
        "database": Resource(cpu="1", memory="2Gi"),
        "cache": Resource(cpu="0.5", memory="512Mi"),
        "app": Resource(cpu="1", memory="2Gi"),
        "agent": Resource(cpu="2", memory="4Gi"),
    }


class ResourceDefaults(_Model):
    defaults: dict[str, Resource] = Field(default_factory=_default_resources)


# ───────────────────────────── FlotillaConfig ─────────────────────────────


class FlotillaConfig(_Model):
    opensandbox: OpenSandbox
    storage: Storage
    share: Share
    network: Network = Network()
    security: Security = Security()
    runtime_params: dict[str, str] = {}
    agent_overlay: AgentOverlay = AgentOverlay()
    limits: Limits = Limits()
    timeouts: Timeouts = Timeouts()
    process: Process = Process()
    reaper: Reaper = Reaper()
    resources: ResourceDefaults = ResourceDefaults()

    #: 配置文件所在目录（`load` 时设置），用于解析相对路径；不出现在 TOML 中。
    base_dir: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _local(self) -> FlotillaConfig:
        problems = []
        net = self.opensandbox.network
        narrow = () if self.network.external == "any" else self.network.external
        if net.link == "cidr" and not net.sandbox_cidrs:
            problems.append("opensandbox.network.link = cidr 需要 sandbox_cidrs（后端文档第 1 节）")
        if self.network.external == "any" and not net.platform_cidrs:
            problems.append("network.external = any 需要 opensandbox.network.platform_cidrs（后端文档 4.4 节）")
        if any(_as_network(t) for t in narrow) and not net.sandbox_cidrs:
            problems.append("network.external 含 CIDR 时需要 opensandbox.network.sandbox_cidrs（后端文档第 1 节）")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    # ───────────────────────────── 读入 ─────────────────────────────

    @classmethod
    def load(cls, path: str | Path) -> FlotillaConfig:
        path = Path(path)
        data = tomllib.loads(path.read_text())
        return cls.model_validate({**data, "base_dir": path.resolve().parent})

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, base_dir: Path | None = None) -> FlotillaConfig:
        return cls.model_validate({**data, "base_dir": base_dir})

    def capabilities_path(self) -> Path:
        path = Path(self.opensandbox.capabilities)
        if not path.is_absolute() and self.base_dir is not None:
            path = self.base_dir / path
        return path

    def load_report(self) -> CapabilityReport:
        return CapabilityReport.load_json(self.capabilities_path().read_bytes())

    def credential(self, environ: Mapping[str, str] | None = None) -> str:
        """从环境变量读平台凭证；缺失时报错，消息里只有变量名。"""
        env = os.environ if environ is None else environ
        value = env.get(self.opensandbox.credential_env, "")
        if not value:
            raise ConfigError([f"环境变量 {self.opensandbox.credential_env} 未设置（平台凭证）"])
        return value

    # ───────────────────────────── 与能力报告比对（§16.3）─────────────────────────────

    def check_against(self, report: CapabilityReport) -> None:
        """不调用平台的启动校验。不满足时抛 `ConfigError`，列出全部问题。"""
        problems: list[str] = []
        if report.endpoint.rstrip("/") != self.opensandbox.endpoint:
            problems.append(f"能力报告的 endpoint {report.endpoint} 与配置 {self.opensandbox.endpoint} 不符")
        problems += [f"部署不满足必需项：{name}" for name in report.missing_required()]

        form = self.network.policy().mode
        if form not in report.network.external_forms:
            problems.append(f"network.external 的形式 {form} 不在部署支持的 {sorted(report.network.external_forms)} 中")

        gaps = set(report.security_gaps())
        for name in sorted(gaps - self.security.accept_insecure):
            problems.append(f"部署缺少安全类能力 {name}，须在 security.accept_insecure 中显式接受")
        for name in sorted(self.security.accept_insecure - gaps):
            problems.append(f"security.accept_insecure 中的 {name} 部署已满足，应移除")

        visibility = report.lifecycle.list_visibility_s
        if not self.reaper.grace + visibility < self.reaper.gc_window:
            problems.append(
                f"须满足 grace + list_visibility_s < gc_window（{self.reaper.grace} + {visibility} ≥ "
                f"{self.reaper.gc_window}，§5.6）"
            )
        max_ttl = report.lifecycle.max_ttl_seconds
        if max_ttl is not None and self.reaper.anchor_ttl > max_ttl:
            problems.append(f"reaper.anchor_ttl {self.reaper.anchor_ttl} 超过部署的 TTL 上限 {max_ttl}")

        problems += self._runtime_param_problems()
        if problems:
            raise ConfigError(problems)

    def _runtime_param_problems(self) -> list[str]:
        """运行时参数指向的地址须能从 sandbox 访问（§10）：窄列表时在 external 内，any 时不落在平台网段内。"""
        problems = []
        net = self.opensandbox.network
        for name, value in sorted(self.runtime_params.items()):
            host = _host_of(value)
            if host is None:
                continue  # 不像地址的参数（例如模型名）不涉及网络
            if self.network.external == "any":
                if _in_cidrs(host, net.platform_cidrs):
                    problems.append(f"runtime_params.{name} 指向平台基础设施 {host}，external = any 下不可达")
                elif _in_cidrs(host, net.sandbox_cidrs):
                    problems.append(f"runtime_params.{name} 指向 sandbox 网段 {host}，external = any 下不可达")
            elif not _allowed(host, self.network.external):
                problems.append(f"runtime_params.{name} 的主机 {host} 不在 network.external 中")
        return problems

    # ───────────────────────────── 翻译 ─────────────────────────────

    def anchor_settings(self) -> AnchorSettings:
        if not self.reaper.anchor_image:
            raise ConfigError(["reaper.anchor_image 未设置"])
        return AnchorSettings(
            image=self.reaper.anchor_image,
            ttl_s=self.reaper.anchor_ttl,
            renew_interval_s=self.reaper.renew_interval,
            grace_s=self.reaper.grace,
        )

    def reaper_settings(self) -> ReaperSettings:
        return ReaperSettings(
            create_rate=self.limits.create_rate,
            create_concurrency=self.limits.create_concurrency,
            create_request_timeout_s=self.timeouts.create,
            close_timeout_s=self.timeouts.close,
        )

    def trial_settings(self, *, extra_labels: Mapping[str, str] | None = None) -> TrialSettings:
        t = self.timeouts
        return TrialSettings(
            share_release=self.share.release,
            external=self.network.policy(),
            ttl_s=self.reaper.trial_ttl,
            create_timeout_s=t.create,
            address_timeout_s=t.address,
            wire_timeout_s=t.wire,
            hosts_timeout_s=t.hosts,
            prepare_timeout_s=t.prepare,
            start_max_s=t.start_max,
            process_poll_s=self.process.poll_interval,
            extra_labels=dict(extra_labels or {}),
        )

    def monitor_settings(self) -> MonitorSettings:
        return MonitorSettings(ttl_s=self.reaper.trial_ttl)

    def network_settings(self) -> NetworkSettings:
        n = self.opensandbox.network
        return NetworkSettings(sandbox_cidrs=n.sandbox_cidrs, platform_cidrs=n.platform_cidrs, max_rules=n.max_rules)

    def storage_settings(self) -> StorageSettings:
        return self.storage.settings()

    def execd_settings(self) -> ExecdSettings:
        e = self.opensandbox.execd
        return ExecdSettings(protocol=e.protocol, wrapper=e.wrapper)

    def default_resources(self, kind: str) -> Resources:
        r = self.resources.defaults[kind]
        return Resources(cpu=r.cpu, memory=r.memory)


# ───────────────────────────── 地址判断 ─────────────────────────────


def _as_network(target: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    try:
        return ipaddress.ip_network(target, strict=False)
    except ValueError:
        return None


_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def _is_hostname(target: str) -> bool:
    """域名（可带一个前导 `*.` 通配）：每段为合法 DNS label，至少两段，顶级域不是纯数字（排除 `1.2.3` 之类的
    残缺 IP），通配之后至少还有两段（`*.com` 会放行整个顶级域）。"""
    name = target.removeprefix("*.")
    labels = name.split(".")
    if len(name) > 253 or len(labels) < 2 or labels[-1].isdigit():
        return False
    return all(_LABEL.fullmatch(label) for label in labels)


def _host_of(value: str) -> str | None:
    """运行时参数里的主机：`scheme://host…` 或不带 scheme 的 `host[:port][/path]`；都不像时返回 None。"""
    host = urlsplit(value if "://" in value else f"//{value}").hostname
    if host is None:
        return None
    return host if _as_network(host) is not None or _is_hostname(host) else None


def _in_cidrs(host: str, cidrs: tuple[str, ...]) -> bool:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False  # 域名：解析到哪里由部署者保证（§6.4）
    return any(addr in ipaddress.ip_network(c, strict=False) for c in cidrs)


def _allowed(host: str, targets: tuple[str, ...]) -> bool:
    """`host`（IP 或域名）是否被窄列表放行：IP 落在某个 CIDR 内，或域名精确 / 通配匹配。"""
    if _as_network(host) is not None:
        return _in_cidrs(host, tuple(t for t in targets if _as_network(t) is not None))
    host = host.lower()
    for target in targets:
        t = target.lower()
        if t == host or (t.startswith("*.") and host.endswith(t[1:])):
            return True
    return False


__all__ = ["SECURITY_FIELDS", "ConfigError", "FlotillaConfig"]
