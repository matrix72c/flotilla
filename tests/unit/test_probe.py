"""`flotilla probe`（Architecture 第 14 节）：对着假平台验证判定逻辑、声明项合并与清理。

假平台上，锚点的目录操作交给 `SharedTree`；单元上的 shell 命令由 `ProbeShell` 按"合规的部署会怎么回答"作答，
并可逐项改成不合规，检查 probe 把它报为不满足而不是抛错。真实部署的验证方式见后端文档第 8 节。
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from flotilla.capabilities import CapabilityReport, Item
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.platform.base import ErrorCategory, ExecResult, FlotillaError, InstanceHandle, ProcessSpec
from flotilla.platform.fake import FakePlatform, ManualClock, SharedTree
from flotilla.probe import _INBOUND_TOKEN, PROBE_LABEL, Declared, ProbeSettings, _mounted_subdir, probe

SETTINGS = ProbeSettings(
    image="reg/unit@sha256:" + "b" * 64, share_release="r1", endpoint="http://os.test", hosts_recheck_s=5.0
)


def _item(ok: bool) -> Item:
    return Item(ok=ok, source="declared", evidence="部署者声明")


DECLARED = Declared(
    deployment="fake",
    list_visibility_s=30.0,
    link=_item(False),
    link_udp=_item(False),
    inbound_isolation=_item(False),
    external_forms=frozenset({"none", "allowlist"}),
    implicit_egress=("10.96.0.10:53/udp",),
    implicit_egress_declared=_item(True),
    shared_volume=_item(True),
    missing_subpath=_item(False),
    no_auto_mount=_item(False),
    volume_file=_item(False),
    exec_auth=_item(False),
    diagnostics=_item(False),
    privileged_runtime=_item(True),
    multi_container=_item(False),
)


class ProbeShell:
    """单元上的命令：按脚本内容作答。`broken` 中的项改为不合规的回答。"""

    def __init__(self, platform: FakePlatform, tree: SharedTree, anchor: Anchor) -> None:
        self._platform = platform
        self._tree_exec = platform.exec_handler
        self._anchor = anchor
        self.broken: set[str] = set()
        platform.set_exec_handler(self._exec)

    def _exec(self, iid: str, proc: ProcessSpec) -> ExecResult:
        if iid == self._anchor.iid:
            return self._tree_exec(iid, proc)
        script = proc.argv[-1]
        if script.startswith("id -u"):
            out = f"{proc.uid}\n{proc.gid}\n{proc.cwd}\n{proc.env.get('PROBE_VAR', '')}\n"
            return _out(out if "identity" not in self.broken else "0\n0\n/\n\n")
        if script == "sleep 30":
            return ExecResult(exit_code=-1, stdout=b"", stderr=b"", timed_out="timeout" not in self.broken)
        if script.startswith("stat -c"):
            return _out("1000:1000 640\nowned\n")
        if "/sys/fs/cgroup/memory.max" in script:  # cgroup v2：memory.max 与 cpu.max
            memory = "1073741824" if "memory" in self.broken else "536870912"
            cpu = {"cpu-double": "200000 100000", "cpu-low": "50000 100000", "cpu-max": "max 100000"}.get(
                next((b for b in self.broken if b.startswith("cpu-")), ""), "100000 100000"
            )
            return _out(f"{memory}\n{cpu}\n")
        if "nc -z" in script:
            if "notool" in self.broken:
                return _out("NOTOOL\n")
            return _out("self=1\npublic=0\n" if "egress" in self.broken else "self=1\npublic=1\n")
        if _INBOUND_TOKEN in script:  # C5：成员自连总通；外部实例连成员只在未隔离（broken "inbound"）时通
            if "127.0.0.1" in script:
                return _out(f"{_INBOUND_TOKEN}\n")
            return _out(f"{_INBOUND_TOKEN}\n" if "inbound" in self.broken else "")
        if script.startswith("ls -A /probe-ro"):
            listing = "inner\nsibling\n" if "isolation" in self.broken else "inner\n"
            run_id = self._platform.spec(iid).labels[PROBE_LABEL]
            # NFS 挂载形状： 直接挂 server:/export/<子路径>，挂载根为 /。
            source = "srv:/export" if "mount-root" in self.broken else f"srv:/export/launches/Lp/{run_id}/visible"
            return _out(f"{listing}ROOT={source}|/\n")
        return _out("")


def _out(text: str) -> ExecResult:
    return ExecResult(exit_code=0, stdout=text.encode(), stderr=b"")


async def _finish_background(platform: FakePlatform, clock: ManualClock, exit_code: int) -> None:
    """假平台的后台进程只在被显式推进时退出：等 probe 启动 `sleep 3; exit 7` 后，过 3 秒把它推进为已退出。"""
    while True:
        for iid in platform.iids():
            for pid in platform.pids(iid):
                if platform.process_spec(iid, pid).argv[-1] == "sleep 3; exit 7":
                    await clock.sleep(3.0)
                    platform.finish_process(iid, pid, exit_code)
                    return
        await clock.sleep(0.5)


async def run_probe(
    platform: FakePlatform, clock: ManualClock, *, broken: frozenset[str] = frozenset(), exit_code: int = 7
) -> CapabilityReport:
    tree = SharedTree(platform)
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor@sha256:abc"))
    await clock.run(anchor.start())
    await anchor.mkdir("launches/Lp", stage="prepare")
    shell = ProbeShell(platform, tree, anchor)
    shell.broken = set(broken)
    platform.initial_files["/etc/hosts"] = b"127.0.0.1 localhost\n"
    finisher = asyncio.ensure_future(_finish_background(platform, clock, exit_code))
    try:
        return await clock.run(probe(platform, clock, anchor, DECLARED, SETTINGS))
    finally:
        finisher.cancel()
        await asyncio.wait({finisher})
        await anchor.close()


# ───────────────────────────── 合规部署 ─────────────────────────────


@pytest.mark.asyncio
async def test_compliant_deployment_satisfies_required_items(platform: FakePlatform, clock: ManualClock) -> None:
    report = await run_probe(platform, clock)
    assert report.missing_required() == []
    for item in (
        report.lifecycle.create_get_delete,
        report.lifecycle.label_list,
        report.lifecycle.renew,
        report.exec.exec_identity,
        report.exec.files,
        report.exec.placeholder_entry,
        report.exec.background,
        report.network.external_none,
        report.network.inbound_isolation,
        report.storage.read_only,
        report.storage.subdir_isolation,
        report.storage.dir_management,
        report.runtime.memory_limit,
        report.runtime.cpu_limit,
        report.runtime.image_digest,
    ):
        assert item.ok and item.source == "probed", item
    assert report.network.internal_address == "exec"
    assert report.endpoint == "http://os.test" and report.deployment == "fake"


@pytest.mark.asyncio
async def test_declared_items_are_copied_verbatim(platform: FakePlatform, clock: ManualClock) -> None:
    report = await run_probe(platform, clock)
    assert report.network.link == DECLARED.link and report.runtime.exec_auth == DECLARED.exec_auth
    assert report.network.external_forms == DECLARED.external_forms
    assert report.lifecycle.list_visibility_s == 30.0
    assert report.security_gaps() == ["exec_auth", "no_auto_mount"]  # inbound 现在测得满足


@pytest.mark.asyncio
async def test_label_list_latency_is_measured(platform: FakePlatform, clock: ManualClock) -> None:
    # 假平台把 list 的可见时刻放在请求到达后 caps.list_visibility_s（30s，取 C1 允许的最晚时刻）。
    report = await run_probe(platform, clock)
    assert "30.0s 出现在按标签列出的结果中" in report.lifecycle.label_list.evidence


@pytest.mark.asyncio
async def test_cleans_up_every_instance_and_directory(platform: FakePlatform, clock: ManualClock) -> None:
    await run_probe(platform, clock)
    assert [i for i in platform.iids() if PROBE_LABEL in platform.spec(i).labels] == []


# ───────────────────────────── 不合规的回答报为不满足，而不是抛错 ─────────────────────────────


@pytest.mark.parametrize(
    ("broken", "item"),
    [
        ("identity", "exec.exec_identity"),
        ("timeout", "exec.exec_identity"),
        ("memory", "runtime.memory_limit"),
        ("egress", "network.external_none"),
        ("isolation", "storage.subdir_isolation"),
        ("mount-root", "storage.subdir_isolation"),  # 挂载源是根目录而不是所挂子目录
    ],
)
@pytest.mark.asyncio
async def test_non_compliant_answer_reported_as_unsatisfied(
    platform: FakePlatform, clock: ManualClock, broken: str, item: str
) -> None:
    report = await run_probe(platform, clock, broken=frozenset({broken}))
    section, field = item.split(".")
    assert getattr(getattr(report, section), field).ok is False
    assert report.missing_required()  # 这些都是必需项


@pytest.mark.asyncio
async def test_inbound_not_isolated_is_security_gap(platform: FakePlatform, clock: ManualClock) -> None:
    # 外部实例能连进成员：入站隔离测得不满足，进安全缺口（provider 默认拒绝该部署）。
    report = await run_probe(platform, clock, broken=frozenset({"inbound"}))
    assert report.network.inbound_isolation.ok is False
    assert report.network.inbound_isolation.source == "probed"
    assert "inbound_isolation" in report.security_gaps()


@pytest.mark.asyncio
async def test_inbound_falls_back_to_declared_without_platform_cidrs(
    platform: FakePlatform, clock: ManualClock
) -> None:
    # 构造不出开放出站的外部实例（external=any 需 platform_cidrs）：本项不测，用声明值，不建任何实例。
    from flotilla.core.anchor import Anchor, AnchorSettings
    from flotilla.platform.base import ErrorCategory, FlotillaError
    from flotilla.probe import _Run

    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor@sha256:abc"))
    await clock.run(anchor.start())
    run = _Run(platform, clock, anchor, SETTINGS, run_id="pX")
    platform.inject_fault(
        "create",
        FlotillaError("any 需 platform_cidrs", stage="create", category=ErrorCategory.INVALID, retryable=False),
    )
    declared = DECLARED.inbound_isolation
    result = await clock.run(run.inbound(declared))
    assert result == declared
    assert [i for i in platform.iids() if PROBE_LABEL in platform.spec(i).labels] == []
    await anchor.close()


@pytest.mark.asyncio
async def test_wrong_background_exit_code_is_unsatisfied(platform: FakePlatform, clock: ManualClock) -> None:
    report = await run_probe(platform, clock, exit_code=0)
    assert not report.exec.background.ok and "期望 7" in report.exec.background.evidence


@pytest.mark.asyncio
async def test_hosts_rewritten_by_platform_is_unsatisfied(platform: FakePlatform, clock: ManualClock) -> None:
    real_read = platform.read_file
    reads = 0

    async def flaky_read(handle: InstanceHandle, path: str) -> bytes:
        nonlocal reads
        data = await real_read(handle, path)
        if path == "/etc/hosts":
            reads += 1
            if reads == 3:  # 第三次读（隔一段时间再读）时平台已把它改回原样
                return b"127.0.0.1 localhost\n"
        return data

    platform.read_file = flaky_read  # type: ignore[method-assign]
    report = await run_probe(platform, clock)
    assert not report.exec.files.ok and "被改写" in report.exec.files.evidence


@pytest.mark.asyncio
async def test_platform_error_aborts_probe_and_still_cleans_up(platform: FakePlatform, clock: ManualClock) -> None:
    # "测试没跑成"不能写成"能力不满足"：出错时整个 probe 抛出，已建实例仍被删除。故障打在 start_process 上——
    # 锚点自己的后台续期也调 renew，会先吃掉打在 renew 上的故障。
    error = FlotillaError("503", stage="run", category=ErrorCategory.TRANSIENT, retryable=True)
    platform.inject_fault("start_process", error)
    with pytest.raises(FlotillaError):
        await run_probe(platform, clock)
    assert [i for i in platform.iids() if PROBE_LABEL in platform.spec(i).labels] == []


@pytest.mark.asyncio
async def test_tag_image_does_not_verify_digest_pull(platform: FakePlatform, clock: ManualClock) -> None:
    # C15 要求按 digest 拉取：以 tag 引用的镜像创建成功不能当作满足。
    tagged = ProbeSettings(image="reg/unit:latest", share_release="r1", endpoint="http://os.test", hosts_recheck_s=5.0)
    tree = SharedTree(platform)
    anchor = Anchor(platform, clock, "Lt", AnchorSettings(image="anchor@sha256:abc"))
    await clock.run(anchor.start())
    await anchor.mkdir("launches/Lt", stage="prepare")
    ProbeShell(platform, tree, anchor)
    platform.initial_files["/etc/hosts"] = b"127.0.0.1 localhost\n"
    finisher = asyncio.ensure_future(_finish_background(platform, clock, 7))
    try:
        report = await clock.run(probe(platform, clock, anchor, DECLARED, tagged))
    finally:
        finisher.cancel()
        await asyncio.wait({finisher})
        await anchor.close()
    assert report.exec.placeholder_entry.ok and not report.runtime.image_digest.ok
    assert "C15 未验证" in report.runtime.image_digest.evidence


@pytest.mark.asyncio
async def test_missing_network_tool_aborts_instead_of_reporting_unreachable(
    platform: FakePlatform, clock: ManualClock
) -> None:
    # 工具缺失时 "连不上" 不能当作 "none 策略生效"：是测试本身出错。
    with pytest.raises(FlotillaError, match="nc -z"):
        await run_probe(platform, clock, broken=frozenset({"notool"}))
    assert [i for i in platform.iids() if PROBE_LABEL in platform.spec(i).labels] == []


@pytest.mark.asyncio
async def test_label_list_beyond_declared_limit_is_unsatisfied(platform: FakePlatform, clock: ManualClock) -> None:
    platform.set_latency("list", 45.0)  # 每次列出都比声明的 30s 时限还慢
    report = await run_probe(platform, clock)
    assert not report.lifecycle.label_list.ok


@pytest.mark.parametrize(
    "change",
    [
        {"external_forms": frozenset({"allowlist"})},  # probe 总会测 none
        {"list_visibility_s": 0},
        {"link_max_members": 0},
        {"exec_auth": Item(ok=True, source="probed")},  # 声明文件里的项只能是 declared
    ],
)
def test_declared_file_validated_before_probing(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Declared.model_validate({**DECLARED.model_dump(), **change})


@pytest.mark.parametrize(
    ("entry", "ok"),
    [
        ("srv:/export/launches/L/p1/visible|/", True),  # NFS：子路径在挂载源里
        ("/dev/sda1|/data/launches/L/p1/visible", True),  # bind / K8s subPath：子路径在挂载根里
        ("srv:/export/launches/L|/p1/visible", True),
        ("srv:/export|/", False),  # 挂了共享根目录本身
        ("srv:/export/launches/L/p1/visible-other|/", False),
        ("|", False),
    ],
)
def test_mounted_subdir_reads_source_and_root(entry: str, ok: bool) -> None:
    assert _mounted_subdir(entry, "p1/visible") is ok


@pytest.mark.parametrize(
    ("broken", "ok", "ratio"),
    [
        ("cpu-double", True, 2.0),  # 上限是给定值的 2 倍——满足（不低于），倍数照实记录
        ("cpu-max", True, None),  # 不限
        ("cpu-low", False, 0.5),  # 低于给定值：不满足
    ],
)
@pytest.mark.asyncio
async def test_cpu_limit_at_least_requested_and_ratio_recorded(
    platform: FakePlatform, clock: ManualClock, broken: str, ok: bool, ratio: float | None
) -> None:
    report = await run_probe(platform, clock, broken=frozenset({broken}))
    assert report.runtime.cpu_limit.ok is ok and report.runtime.cpu_limit_ratio == ratio
    assert report.missing_required() == []  # CPU 是降级项，不影响"可用"
