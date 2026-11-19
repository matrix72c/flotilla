"""Measured isolation evidence. Everything else a deployment can do is assumed until it refuses.

A functional capability fails loudly: ask for a device the platform has no runtime for, a TTL
beyond its limit, or a tmpfs it cannot mount, and create returns an error. Measuring those up
front bought a nicer error message for the price of a separate report format to maintain.

Isolation fails quietly. A deployment that ignores an egress policy, mounts storage a sandbox was
not given, or serves the exec channel without checking the token does not say so -- it returns
success and the restriction simply is not there. That is what this measures, and the only reason
anything is measured at all.

The result is values in memory: seven booleans and the evidence behind each. There is no report
file, no schema and no version, because nothing reads it back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast

from flotilla.platform.base import Device as Device
from flotilla.platform.network import cluster_dns_address

#: An isolation property a deployment may lack. `security.accept_insecure` names the ones a
#: deployment accepts without; the rest must be measured to hold before anything runs.
IsolationProperty = Literal[
    "external_none",
    "network_acceptance",
    "inbound_isolation",
    "read_only",
    "subdir_isolation",
    "no_auto_mount",
    "exec_auth",
]
ISOLATION_PROPERTIES: tuple[IsolationProperty, ...] = (
    "external_none",
    "network_acceptance",
    "inbound_isolation",
    "read_only",
    "subdir_isolation",
    "no_auto_mount",
    "exec_auth",
)
ExternalForm = Literal["none", "any", "allowlist"]


@dataclass(frozen=True)
class ExternalTarget:
    """A content-bearing external control, separate from workload egress policy."""

    host: str
    port: int
    protocol: Literal["tcp", "udp"] = "tcp"
    service: Literal["echo", "https", "ntp"] = "echo"

    def __post_init__(self) -> None:
        if not self.host or any(char.isspace() for char in self.host):
            raise ValueError("probe target host must be nonempty and contain no whitespace")
        if not 1 <= self.port <= 65535 or self.protocol not in ("tcp", "udp"):
            raise ValueError("invalid probe target port or protocol")
        if self.service not in ("echo", "https", "ntp"):
            raise ValueError("unknown probe target service")
        if self.service == "https" and self.protocol != "tcp":
            raise ValueError("https probe service requires tcp")
        if self.service == "ntp" and self.protocol != "udp":
            raise ValueError("ntp probe service requires udp")


@dataclass(frozen=True)
class ProbeSettings:
    image: str
    share_release: str
    endpoint: str
    privileged_image: str = ""
    external_targets: tuple[ExternalTarget | tuple[str, int, str], ...] = ()
    dns_name: str = ""
    create_timeout_s: float = 300.0
    ttl_s: int = 1800
    anchor_ttl_s: int = 600
    list_visibility_s: float = 30.0
    total_timeout_s: float = 3600.0
    cleanup_timeout_s: float = 180.0
    test_external_any: bool = False

    def __post_init__(self) -> None:
        import math

        if not self.image:
            raise ValueError("probe image must be a pinned reference")
        for name in ("create_timeout_s", "total_timeout_s", "cleanup_timeout_s", "list_visibility_s"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        targets = tuple(
            t if isinstance(t, ExternalTarget) else ExternalTarget(t[0], t[1], cast(Literal["tcp", "udp"], t[2]))
            for t in self.external_targets
        )
        object.__setattr__(self, "external_targets", targets)

    @property
    def targets(self) -> tuple[ExternalTarget, ...]:
        """Typed controls; the frozen instance normalizes legacy inputs in __post_init__."""
        return cast(tuple[ExternalTarget, ...], self.external_targets)


@dataclass(frozen=True)
class Evidence:
    """One isolation check: whether the restriction held, and what was observed."""

    held: bool
    observed: str = ""

    def __bool__(self) -> bool:
        return self.held


#: A check that could not run. Not evidence that the restriction holds.
UNMEASURED = Evidence(False, "not measured")


#: The privileged-runtime counterparts of the ordinary checks, in measurement order.
PRIVILEGED_PROPERTIES = (
    "privileged_storage",
    "privileged_no_auto_mount",
    "privileged_exec_auth",
    "privileged_network",
    "privileged_inbound_isolation",
)


@dataclass(frozen=True)
class IsolationEvidence:
    """What a probe run established about a deployment's isolation, in memory.

    Each field is one restriction that fails silently when absent, so absence has to be observed
    rather than inferred. `privileged_*` repeats the ordinary checks inside the privileged runtime,
    because a restriction holding for an ordinary sandbox says nothing about one that does not.
    """

    #: Egress: a policy of "no external access" is actually enforced.
    external_none: Evidence = UNMEASURED
    #: The platform accepted the network policy it was sent, and cluster DNS behaves as measured.
    network_acceptance: Evidence = UNMEASURED
    #: A sandbox cannot be reached from outside its own trial.
    inbound_isolation: Evidence = UNMEASURED
    #: A read-only mount really is read-only.
    read_only: Evidence = UNMEASURED
    #: A sandbox given one subdirectory of shared storage cannot read its siblings.
    subdir_isolation: Evidence = UNMEASURED
    #: Storage the sandbox was not given is not mounted into it anyway.
    no_auto_mount: Evidence = UNMEASURED
    #: The exec channel checks its token instead of serving anyone who connects.
    exec_auth: Evidence = UNMEASURED
    #: Deleting what the probe created was confirmed, not assumed.
    cleanup: Evidence = UNMEASURED

    #: The same restrictions, measured again inside the privileged runtime. Named by
    #: `PRIVILEGED_PROPERTIES`, in the order the privileged phase measures them.
    privileged_storage: Evidence = UNMEASURED
    privileged_no_auto_mount: Evidence = UNMEASURED
    privileged_exec_auth: Evidence = UNMEASURED
    privileged_network: Evidence = UNMEASURED
    privileged_inbound_isolation: Evidence = UNMEASURED

    #: Observations, not a capability: which egress forms were exercised, what reached out
    #: implicitly, and which resolvers the platform uses.
    external_forms: frozenset[ExternalForm] = frozenset()
    implicit_egress: tuple[str, ...] = ()
    cluster_dns: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cluster_dns", tuple(cluster_dns_address(ip) for ip in self.cluster_dns))

    def gaps(self) -> list[IsolationProperty]:
        """Isolation properties this deployment was not shown to have."""
        return [name for name in ISOLATION_PROPERTIES if not getattr(self, name).held]

    def privileged_isolated(self, accepted: frozenset[str] = frozenset()) -> bool:
        """Whether the privileged runtime isolates well enough to admit a `privileged` service.

        An accepted gap counts as accepted here too: a deployment the operator already runs
        without one of these does not become safer by refusing the privileged runtime alone.
        """
        required = (("privileged_storage", ""), ("privileged_network", ""))
        optional = (
            ("privileged_no_auto_mount", "no_auto_mount"),
            ("privileged_exec_auth", "exec_auth"),
            ("privileged_inbound_isolation", "inbound_isolation"),
        )
        return all(getattr(self, name).held for name, _ in required) and all(
            getattr(self, name).held or waiver in accepted for name, waiver in optional
        )
