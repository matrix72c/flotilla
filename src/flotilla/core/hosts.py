"""Generate service names from shared networks while preserving unrelated hosts entries."""

from __future__ import annotations

from collections.abc import Mapping

from flotilla.core.plan import TrialPlan

BEGIN = "# BEGIN flotilla"
END = "# END flotilla"


def entries(plan: TrialPlan, unit: str, addresses: Mapping[str, str]) -> list[tuple[str, tuple[str, ...]]]:
    """Hosts entries for a unit: (address, names...), sorted by address then name (deterministic).

    Names come only from networks shared with this unit; services sharing no network are omitted (lookups fall
    through to platform DNS and get NXDOMAIN, matching Compose). A name pointing to the same service on several
    shared networks appears once; a name pointing to several services (replicas) gets one entry per address.
    """
    names: dict[str, set[str]] = {}  # address -> names
    for net in plan.networks.values():
        if unit not in net.members:
            continue
        for host, services in net.names.items():
            for service in services:
                names.setdefault(addresses[service], set()).add(host)

    return [(addr, tuple(sorted(hosts))) for addr, hosts in sorted(names.items())]


def block(lines: list[tuple[str, tuple[str, ...]]]) -> str:
    """Marked hosts block (with trailing newline)."""
    body = "".join(f"{addr}\t{' '.join(hosts)}\n" for addr, hosts in lines)
    return f"{BEGIN}\n{body}{END}\n"


def merge(original: str, new_block: str) -> str:
    """Put `new_block` into the original hosts content: replace an existing marked block, else append it;
    other lines are kept as-is."""
    lines = original.splitlines(keepends=True)
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip("\n") == BEGIN)
        end = next(i for i in range(start, len(lines)) if lines[i].rstrip("\n") == END)
    except StopIteration:
        prefix = original if not original or original.endswith("\n") else original + "\n"
        return prefix + new_block
    return "".join(lines[:start]) + new_block + "".join(lines[end + 1 :])
