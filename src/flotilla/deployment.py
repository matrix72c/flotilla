"""One consumer entry point for a deployment.

Consumers load a deployment once and ask it for everything platform-side: the platform
credential, clients, release bindings, setup budgets, capacity peaks, release validation,
read-only runtime layer mounts and a portable frozen copy. They never read the
configuration's internal tables (backend sections, timeout names, storage settings).
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from flotilla.backends import BUILTIN
from flotilla.config import FlotillaConfig
from flotilla.environment import read_env_file
from flotilla.native.models import DatasetRow
from flotilla.native.release import ReleaseBindings, platform_key
from flotilla.platform.base import SharedVolume
from flotilla.prepared import PreparedEnvironment
from flotilla.resources import memory
from flotilla.runtime.client import Flotilla
from flotilla.runtime.types import CleanupError, OpenOptions, RuntimeEvent

FROZEN_CONFIG = "deployment.json"
FROZEN_CAPABILITIES = "capabilities.json"
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
class RowValidation:
    """Validation outcome of one dataset row; failures are reported, never raised."""

    record: Any
    passed: bool = False
    task: str | None = None
    environments: list[dict[str, Any]] = field(default_factory=list)
    roles: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


OptionsFactory = Callable[[str, PreparedEnvironment], OpenOptions]


class Deployment:
    def __init__(self, config: FlotillaConfig) -> None:
        self._config = config

    @classmethod
    def load(cls, path: str | Path, *, credentials: str | Path | None = None) -> Deployment:
        """Load and validate a deployment; optionally bind the platform credential from a file."""
        deployment = cls(FlotillaConfig.load(path))
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

    def release_bindings(self) -> ReleaseBindings:
        return self._config.release_bindings()

    def client(self, *, on_event: Callable[[RuntimeEvent], None] | None = None) -> Flotilla:
        return Flotilla(deployment=self._config, on_event=on_event)

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

    def dataset_peak(self, rows: Iterable[DatasetRow]) -> DatasetPeak:
        """Largest single-role demand across rows; a pure local calculation without credentials."""
        bindings = self.release_bindings()
        fleet = self.client()
        peak: dict[str, float] = {}
        records = 0
        for row in rows:
            records += 1
            bound = bindings.resolve(row)
            # A separate verification role releases main before acquiring its own resources.
            for manifest in bound.task.roles.values():
                reservation = fleet.reservation(PreparedEnvironment(manifest))
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
        prepared: PreparedEnvironment,
        layers: RuntimeLayers,
        mount_paths: Mapping[str, str],
        **options: Any,
    ) -> OpenOptions:
        """Open options mounting each layer read-only into the manifest's agent service."""
        current = self._config.storage_settings_for(layers.root).root
        if layers.root_identity != current:
            raise ValueError("runtime layer storage root does not match the deployment")
        if set(mount_paths) != set(layers.keys):
            raise ValueError(f"mount paths must name exactly the runtime layers {sorted(layers.keys)}")
        volumes = tuple(
            SharedVolume(key=layers.keys[name], mount_path=mount_paths[name], read_only=True, root=layers.root)
            for name in sorted(layers.keys)
        )
        return OpenOptions(external_mounts={prepared.manifest().agent_service: volumes}, **options)

    async def validate(
        self, rows: Iterable[Any], *, sandboxes: bool = False, options: OptionsFactory | None = None
    ) -> list[RowValidation]:
        """Validate every row against its release; optionally start each non-verifier role once.

        Rows are raw mappings (for example parsed JSON lines). Each failure is recorded on its
        row; duplicates stay visible. `options` supplies per-role open options in sandbox mode.
        """
        # Keep `import flotilla` light: the attempt lifecycle loads only when validating.
        from flotilla.native.runtime import NativeAttempt

        bindings = self.release_bindings()
        fleet = self.client()
        results: list[RowValidation] = []
        try:
            for raw in rows:
                result = RowValidation(record=raw)
                results.append(result)
                attempt = None
                try:
                    row = DatasetRow.model_validate(raw)
                    result.task = f"{row.release}:{row.task}"
                    bound = bindings.resolve(row)
                    attempt = await NativeAttempt.create(fleet, bound)
                    verification = bound.task.verification
                    verifier = verification.role if verification is not None else None
                    for role, manifest in bound.task.roles.items():
                        definition = PreparedEnvironment(manifest)
                        result.environments.append(asdict(fleet.reservation(definition)))
                        # The isolated verifier only opens through verify() after a run.
                        if not sandboxes or role == verifier:
                            continue
                        handle = await attempt.open(role, options(role, definition) if options else OpenOptions())
                        try:
                            context = manifest.units[manifest.agent_service].context
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

    def freeze(self, directory: str | Path) -> Path:
        """Write a portable copy: config plus its capability report, with absolute external roots.

        Release payloads and runtime layers stay external and immutable, so their local roots
        freeze as absolute paths; the capability report moves alongside the config.
        Returns the path to load the frozen copy from.
        """
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._config.capabilities_path().resolve(strict=True), directory / FROZEN_CAPABILITIES)
        data = self._config.to_mapping()
        backend = self._config.backend
        table = data["opensandbox"] if backend == BUILTIN else data["backends"][backend]
        table["capabilities"] = FROZEN_CAPABILITIES
        target = directory / FROZEN_CONFIG
        target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        return target
