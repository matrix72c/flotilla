"""The in-memory prepared environment boundary for `fleet.open`.

One prepared environment per role, regenerated whenever the task or flotilla's
preparation rules change. The persistent interchange is the native release, not
this type; see `flotilla.native`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from flotilla.manifest import Manifest

ROLES = ("main", "verifier")


@dataclass(frozen=True)
class PreparedEnvironment:
    """One prepared environment: digest-pinned images and published task files, ready to open."""

    _manifest: Manifest = field(repr=False)
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        manifest = self._manifest.model_copy(deep=True)
        object.__setattr__(self, "_manifest", manifest)
        object.__setattr__(self, "digest", hashlib.sha256(manifest.dump_json().encode()).hexdigest())
        if manifest.agent_service not in manifest.units:
            raise ValueError("manifest agent_service does not exist")
        if any(f.kind == "reject" for f in manifest.findings):
            raise ValueError("manifest contains rejected findings")

    def manifest(self) -> Manifest:
        return self._manifest.model_copy(deep=True)
