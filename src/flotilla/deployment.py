"""One consumer entry point for a deployment.

Consumers load a deployment once and ask it for everything platform-side: the platform
credential, clients, declared datasets, setup budgets, capacity peaks, dataset validation and
read-only runtime layer mounts. They never read the configuration's internal tables (backend
sections, timeout names, storage settings).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from flotilla.config import FlotillaConfig
from flotilla.core.anchor import Anchor
from flotilla.core.plan import TaskSource, TrialPlan
from flotilla.environment import read_env_file
from flotilla.native.dataset import Dataset, platform_key
from flotilla.platform.base import SharedVolume
from flotilla.resources import memory
from flotilla.runtime.client import Flotilla
from flotilla.runtime.types import CleanupError, OpenOptions, RuntimeEvent

# Phases that bring one environment from creation to started services (seconds, summed worst case).
_SETUP_PHASES = ("create", "address", "wire", "hosts", "prepare", "start_max")


def memory_gib(value: str) -> float:
    """A Kubernetes-style memory quantity in GiB."""
    normalized = memory(value)
    suffix = normalized[-2:] if normalized.endswith("i") else ""
    number = normalized[:-2] if suffix else normalized
    multiplier = 1024 ** ("KMGT".index(suffix[0]) + 1) if suffix else 1
    return float(Decimal(number) * multiplier / 1024**3)


@dataclass(frozen=True)
class RuntimeLayers:
    """The deployment's [agent_runtime] layers resolved to storage keys on one storage root.

    `root_identity` is the backend identity of that root at resolution time, so a layer set
    recorded earlier (for example in a run snapshot) is rejected if the storage moved.
    """

    root: str
    root_identity: str
    keys: Mapping[str, str]


@dataclass(frozen=True)
class DatasetPeak:
    """Independent per-role maxima over a dataset; conservative for mixed tasks."""

    records: int
    services: int = 0
    cpu: float = 0.0
    memory_gib: float = 0.0
    reserved_instances: int = 0
    reserved_cpu: float = 0.0
    reserved_memory_gib: float = 0.0


@dataclass
class TaskValidation:
    """Validation outcome of one task; failures are reported, never raised."""

    task: str
    passed: bool = False
    environments: list[dict[str, Any]] = field(default_factory=list)
    roles: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


OptionsFactory = Callable[[str, TrialPlan], OpenOptions]


class Deployment:
    def __init__(self, config: FlotillaConfig) -> None:
        self._config = config

    @classmethod
    def load(cls, target: str, *, credentials: str | Path | None = None) -> Deployment:
        """Import a deployment spec named `package.module:attribute`; optionally bind its credential.

        A consumer holding the spec already passes it to `Deployment(spec)`. This is for a process
        that only has its name, which is also what `flotilla gc` takes.
        """
        deployment = cls(FlotillaConfig.load(target))
        if credentials is not None:
            deployment.bind_credentials(credentials)
        return deployment

    @property
    def config(self) -> FlotillaConfig:
        """An independent copy, for callers that need a setting not exposed here."""
        return self._config.model_copy(deep=True)

    @property
    def trial_concurrency(self) -> int:
        return self._config.limits.trial_concurrency

    @property
    def instance_budget(self) -> int:
        return self._config.limits.instance_budget

    def bind_credentials(self, path: str | Path) -> None:
        """Bind only this deployment's credential variable from a literal KEY=value file.

        Other lines are ignored, so one private file may also hold the consumer's own keys.
        An existing, different value in the process environment is a conflict, not overwritten.
        """
        name = self._config.backend_settings.credential_env
        values = read_env_file(path)
        value = values.get(name, "")
        if not value:
            raise ValueError(f"credential file {path} does not set {name} (platform credential)")
        existing = os.environ.get(name)
        if existing is not None and existing != value:
            raise ValueError(f"environment variable {name} conflicts with the credential file")
        os.environ[name] = value

    def datasets(self) -> dict[str, Dataset]:
        """The deployment's declared datasets by name, each on `work` or a named input root."""
        return self._config.resolve_datasets()

    def client(self, *, on_event: Callable[[RuntimeEvent], None] | None = None) -> Flotilla:
        return Flotilla(deployment=self._config, on_event=on_event)

    @asynccontextmanager
    async def management_anchor(self) -> AsyncIterator[Anchor]:
        """A temporary Anchor for shared-storage work outside a run: publishing or checking a share release.

        It is the only way to reach shared storage, because nothing else may write there, and it is
        deliberately not a client: a bounded management anchor claims none of the capabilities a task
        needs. It creates no `launches/<id>` directory, so it never becomes a gc candidate.
        """
        from flotilla.platform.clock import SystemClock

        clock = SystemClock()
        platform = self._config.build_platform(clock=clock, caps=self._config.management_capabilities())
        anchor = Anchor(platform, clock, f"mgmt-{uuid.uuid4().hex[:12]}", self._config.anchor_settings())
        try:
            await anchor.start()
            yield anchor
        finally:
            await anchor.close()
            await platform.aclose()

    def run_timeout(self) -> float:
        """Upper bound for one run inside an opened environment."""
        return self._config.runtime.run_timeout_s

    def setup_budget(self) -> float:
        """Worst-case seconds from creating one environment to its started services."""
        timeouts = self._config.timeouts
        return float(sum(getattr(timeouts, name) for name in _SETUP_PHASES))

    def client_overhead(self) -> dict[str, float]:
        """Per-client anchor reservation, in addition to environment reservations."""
        resource = self.client().client_reservation().resources["anchor"]
        return {"cpu": float(resource["cpu"]), "memory_gib": memory_gib(resource["memory"])}

    def dataset_peak(self, sources: Iterable[TaskSource]) -> DatasetPeak:
        """Largest single-role demand across tasks; a local calculation needing only the registry."""
        from flotilla.native.runtime import NativeAttempt

        fleet = self.client()
        peak: dict[str, float] = {}
        records = 0
        for source in sources:
            records += 1
            # A separate verification role releases main before acquiring its own resources.
            for plan in NativeAttempt.resolve(fleet, source).plans.values():
                reservation = fleet.reservation(plan)
                resources = reservation.resources.values()
                cpu = sum(float(r["cpu"]) for r in resources)
                gib = sum(memory_gib(r["memory"]) for r in resources)
                shape = {
                    "services": len(reservation.resources),
                    "cpu": cpu,
                    "memory_gib": gib,
                    "reserved_instances": reservation.instances,
                    "reserved_cpu": cpu * reservation.copies,
                    "reserved_memory_gib": gib * reservation.copies,
                }
                for key, value in shape.items():
                    peak[key] = max(peak.get(key, 0), value)
        if not records:
            raise ValueError("empty dataset")
        return DatasetPeak(
            records=records,
            services=int(peak["services"]),
            cpu=peak["cpu"],
            memory_gib=peak["memory_gib"],
            reserved_instances=int(peak["reserved_instances"]),
            reserved_cpu=peak["reserved_cpu"],
            reserved_memory_gib=peak["reserved_memory_gib"],
        )

    def storage_identity(self, root: str = "work") -> str:
        """Backend identity of the work root or a named read-only input root."""
        return self._config.storage_settings_for(root).root

    def runtime_layers(self) -> RuntimeLayers:
        """The [agent_runtime] layers as storage keys; layer names are not interpreted."""
        runtime = self._config.agent_runtime
        if runtime is None:
            raise ValueError("the deployment declares no [agent_runtime] runtime layers")
        # The runtime is a read-only input: it may live on a separately shared input root.
        root = runtime.input or "work"
        storage = self._config.storage_settings_for(root)
        keys = {name: platform_key(layer.platform_root, storage) for name, layer in runtime.layers.items()}
        return RuntimeLayers(root=root, root_identity=storage.root, keys=keys)

    def runtime_options(
        self,
        plan: TrialPlan,
        layers: RuntimeLayers,
        mount_paths: Mapping[str, str],
        **options: Any,
    ) -> OpenOptions:
        """Open options mounting each layer read-only into the manifest's agent service, in `mount_paths` order."""
        if layers.root_identity != self.storage_identity(layers.root):
            raise ValueError("runtime layer storage root does not match the deployment")
        if set(mount_paths) != set(layers.keys):
            raise ValueError(f"mount paths must name exactly the runtime layers {sorted(layers.keys)}")
        volumes = tuple(
            SharedVolume(key=layers.keys[name], mount_path=mount_paths[name], read_only=True, root=layers.root)
            for name in mount_paths
        )
        return OpenOptions(external_mounts={plan.agent: volumes}, **options)

    async def validate(
        self, sources: Iterable[TaskSource], *, sandboxes: bool = False, options: OptionsFactory | None = None
    ) -> list[TaskValidation]:
        """Validate every task; optionally start each non-verifier role once.

        Each failure is recorded on its task rather than raised, so one malformed task does not
        hide the rest of the dataset. `options` supplies per-role open options in sandbox mode.
        """
        # Keep `import flotilla` light: the attempt lifecycle loads only when validating.
        from flotilla.native.runtime import NativeAttempt

        fleet = self.client()
        results: list[TaskValidation] = []
        try:
            for source in sources:
                result = TaskValidation(task=source.key or str(source.local))
                results.append(result)
                attempt = None
                try:
                    resolved = NativeAttempt.resolve(fleet, source)
                    attempt = await NativeAttempt.create(fleet, resolved)
                    verification = resolved.verification
                    verifier = verification.role if verification is not None else None
                    for role, plan in resolved.plans.items():
                        result.environments.append(asdict(fleet.reservation(plan)))
                        # A separate verifier role only opens through verify() after a run;
                        # a shared verifier is main itself, which must still start here.
                        if not sandboxes or (role == verifier and role != "main"):
                            continue
                        handle = await attempt.open(role, options(role, plan) if options else OpenOptions())
                        try:
                            context = plan.units[plan.agent].context
                            result.roles.append({"role": role, "uid": context.uid, "gid": context.gid})
                            await handle.check()
                        finally:
                            await attempt.release(role)
                    result.passed = True
                except Exception as exc:
                    result.error = f"{type(exc).__name__}: {exc}"
                finally:
                    if attempt is not None:
                        await attempt.close()
        finally:
            report = await fleet.close()
            if not report.clean:
                raise CleanupError(report)
        return results
