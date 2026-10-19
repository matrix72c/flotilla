"""Acquire, wire, and start one environment under an owned lifecycle."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from flotilla.core import hosts, labels, network, process, volumes
from flotilla.core.anchor import Anchor
from flotilla.core.errors import at_stage
from flotilla.core.plan import TrialPlan
from flotilla.core.reaper import Reaper
from flotilla.core.waiting import Expired, run_all, wait_running, within
from flotilla.platform.base import (
    Clock,
    ErrorCategory,
    ExternalPolicy,
    FlotillaError,
    InstanceHandle,
    InstanceSpec,
    Platform,
    Stage,
)

log = logging.getLogger(__name__)

ENTRYPOINT = (process.BUSYBOX, "sleep", "2147483647")  # placeholder entrypoint
HOSTS_PATH = "/etc/hosts"


@dataclass(frozen=True)
class TrialSettings:
    """`[timeouts]` and the deployment parameters used by a trial."""

    share_release: str  # pinned share release
    external: ExternalPolicy  # caller egress policy `[network] external`
    ttl_s: int = 6 * 3600  # platform TTL per instance; capped at the deployment max_ttl_seconds
    create_timeout_s: float = 600.0
    address_timeout_s: float = 30.0
    wire_timeout_s: float = 60.0
    hosts_timeout_s: float = 30.0
    prepare_timeout_s: float = 600.0
    start_slack_s: float = 120.0
    start_max_s: float = 1800.0
    process_poll_s: float = 2.0
    extra_labels: Mapping[str, str] = field(default_factory=dict)  # client request.labels (merged with an x- prefix)


@dataclass(frozen=True)
class Ready:
    """A ready trial: per-unit handles and internal addresses, main process IDs, stage durations (s), recreations."""

    handles: Mapping[str, InstanceHandle]
    addresses: Mapping[str, str]
    pids: Mapping[str, str]
    exit_codes: Mapping[str, int]
    timings: Mapping[str, float]
    recreated: frozenset[str]


class Trial:
    """Startup of one trial. The caller has called `reaper.track(trial_id)`; afterwards (success, failure, or
    cancellation) it must call `reaper.release`.

    The constructor validates the plan and computes every unit's `InstanceSpec`: label, egress, and volume key errors
    raise `ValueError` before any platform call (a manifest or translation bug, not a platform issue).
    """

    def __init__(
        self,
        platform: Platform,
        clock: Clock,
        anchor: Anchor,
        reaper: Reaper,
        plan: TrialPlan,
        trial_id: str,
        settings: TrialSettings,
    ) -> None:
        plan.validate()
        if settings.ttl_s <= 0:
            raise ValueError("ttl_s must be > 0")
        max_ttl = platform.caps.max_ttl_seconds
        self._ttl = min(settings.ttl_s, max_ttl) if max_ttl is not None else settings.ttl_s
        self._platform = platform
        self._clock = clock
        self._anchor = anchor
        self._reaper = reaper
        self._plan = plan
        self._trial_id = trial_id
        self._settings = settings
        self._handles: dict[str, InstanceHandle] = {}
        self._timings: dict[str, float] = {}
        self._recreated: set[str] = set()
        # Units that must be linked share one placement group; recreated units reuse it.
        self._grouped = network.link_members(plan) if network.topology(plan) is not None else frozenset()
        self._group = str(uuid.uuid4()) if self._grouped else None
        self._specs = {unit: self._spec(unit) for unit in sorted(plan.units)}

    @property
    def handles(self) -> Mapping[str, InstanceHandle]:
        """Units created so far (for failure diagnostics)."""
        return dict(self._handles)

    async def start(self) -> Ready:
        s = self._settings
        if self._plan.volumes:
            await self._stage("prepare", s.prepare_timeout_s, self._prepare)
        await self._stage("create", s.create_timeout_s, self._create_all)
        addresses = await self._stage("address", s.address_timeout_s, self._addresses)
        topo = network.topology(self._plan)
        if topo is not None:
            members = {u: self._handles[u] for u in network.link_members(self._plan)}
            await self._stage("wire", s.wire_timeout_s, lambda: self._platform.link(members, topo))
        await self._stage("hosts", s.hosts_timeout_s, lambda: self._write_hosts(addresses))
        start_timeout = process.start_timeout(self._plan, slack_s=s.start_slack_s, cap_s=s.start_max_s)
        started = await self._stage(
            "start",
            start_timeout,
            lambda: process.start_all(
                self._platform, self._clock, self._plan, self._handles, poll_interval_s=s.process_poll_s
            ),
        )
        return Ready(
            handles=dict(self._handles),
            addresses=addresses,
            pids=started.pids,
            exit_codes=started.exit_codes,
            timings=dict(self._timings),
            recreated=frozenset(self._recreated),
        )

    @property
    def timings(self) -> dict[str, float]:
        """Completed stage durations, including failed stages."""
        return dict(self._timings)

    async def _stage[T](self, stage: Stage, timeout: float, run: Callable[[], Awaitable[T]]) -> T:
        """Run under the `stage` timeout; timeouts raise `transient` for the stage, platform errors move onto it."""
        began = self._clock.monotonic()
        try:
            return await within(self._clock, timeout, run())
        except Expired as exc:
            raise FlotillaError(
                f"{stage} stage exceeded {timeout}s",
                stage=stage,
                category=ErrorCategory.TRANSIENT,
                retryable=True,
            ) from exc
        except FlotillaError as exc:
            raise at_stage(exc, stage) from exc
        finally:
            self._timings[stage] = self._clock.monotonic() - began

    async def _prepare(self) -> None:
        """Prepare this trial's shared volume dirs via the anchor. Dirs are registered before creation so partial ones
        are reaped."""
        base = volumes.trial_key(self._anchor.launch_id, self._trial_id)
        self._reaper.add_dir(self._trial_id, base)
        await self._anchor.mkdir(base, stage="prepare")
        for vol in self._plan.volumes:
            key = f"{base}/{vol.key}"
            if vol.copy_from is not None:
                assert self._plan.task_files is not None
                await self._anchor.copy_tree(f"{self._plan.task_files}/{vol.copy_from}", key, stage="prepare")
                continue
            await self._anchor.mkdir(key, stage="prepare")
            if vol.seed is not None:
                assert self._plan.task_files is not None
                await self._anchor.unpack(f"{self._plan.task_files}/{vol.seed}", key, stage="prepare")
            elif vol.owner is not None:
                await self._anchor.chown(key, *vol.owner, stage="prepare")

    async def _create_all(self) -> None:
        await run_all(self._create_unit(u) for u in sorted(self._plan.units))

    async def _create_unit(self, unit: str) -> None:
        try:
            await self._create_once(unit)
        except FlotillaError as exc:
            # Recreation is refused once the launch is deactivated (lost); only retry failures a new instance may fix.
            if not exc.retryable or exc.category is ErrorCategory.LOST:
                raise
            log.warning("trial %s unit %s create failed, deleting and recreating once: %s", self._trial_id, unit, exc)
            old = self._handles.pop(unit, None)
            if old is not None:
                self._reaper.discard(self._trial_id, old.iid)
            self._recreated.add(unit)
            await self._create_once(unit)

    async def _create_once(self, unit: str) -> None:
        handle = await self._reaper.create(self._trial_id, unit, self._specs[unit])
        self._handles[unit] = handle
        await wait_running(self._platform, self._clock, handle.iid, stage="create")

    def _spec(self, unit: str) -> InstanceSpec:
        u = self._plan.units[unit]
        return InstanceSpec(
            image=u.image,
            entrypoint=ENTRYPOINT,
            labels=self._labels(unit),
            timeout_seconds=self._ttl,
            resources=u.resources,
            volumes=volumes.unit_volumes(
                self._plan,
                unit,
                share_release=self._settings.share_release,
                launch_id=self._anchor.launch_id,
                trial_id=self._trial_id,
            ),
            external=network.external_policy(self._plan, unit, self._settings.external),
            privileged=u.privileged,
            group=self._group if unit in self._grouped else None,
        )

    def _labels(self, unit: str) -> dict[str, str]:
        """Instance labels: values used for listing are validated verbatim, task and client labels are sanitized
        (`labels` module).

        Client keys are merged with an `x-` prefix and cannot override flotilla keys.
        """
        limit = self._platform.caps.max_label_value_len
        out = {f"x-{k}": labels.sanitize_value(v, limit) for k, v in self._settings.extra_labels.items()}
        out.update(
            {
                labels.LAUNCH: labels.require_value(labels.LAUNCH, self._anchor.launch_id, limit),
                labels.TRIAL: labels.require_value(labels.TRIAL, self._trial_id, limit),
                labels.UNIT: labels.require_value(labels.UNIT, unit, limit),
                labels.TASK: labels.sanitize_value(self._plan.task, limit),
                labels.ROLE: labels.ROLE_UNIT,
            }
        )
        return out

    async def _addresses(self) -> dict[str, str]:
        names = sorted(self._plan.units)
        found = await run_all(self._platform.internal_address(self._handles[u]) for u in names)
        return dict(zip(names, found, strict=True))

    async def _write_hosts(self, addresses: Mapping[str, str]) -> None:
        await run_all(self._write_unit_hosts(u, addresses) for u in sorted(self._plan.units))

    async def _write_unit_hosts(self, unit: str, addresses: Mapping[str, str]) -> None:
        """Read the original hosts, replace the marked block, write, and verify by reading back."""
        handle = self._handles[unit]
        original = (await self._platform.read_file(handle, HOSTS_PATH)).decode()
        content = hosts.merge(original, hosts.block(hosts.entries(self._plan, unit, addresses))).encode()
        await self._platform.write_file(handle, HOSTS_PATH, content, mode=0o644, uid=0, gid=0)
        if await self._platform.read_file(handle, HOSTS_PATH) != content:
            raise FlotillaError(
                f"{unit}: /etc/hosts read-back differs from what was written (the platform may rewrite it at runtime)",
                stage="hosts",
                category=ErrorCategory.INVALID,
                retryable=False,
                service=unit,
            )
