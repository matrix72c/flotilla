"""Compile service topology and explicit external policies."""

from __future__ import annotations

from flotilla.core.plan import TrialPlan
from flotilla.platform.base import ExternalPolicy, Topology

_NONE = ExternalPolicy(mode="none")


def topology(plan: TrialPlan) -> Topology | None:
    """This trial's topology; None when `link` has fewer than two members (`link` is not called and the
    initial isolation is final).

    Members are units in at least one network; empty networks are left out of the topology.
    """
    networks = {name: net.members for name, net in plan.networks.items() if net.members}
    if len(link_members(plan)) < 2:
        return None
    return Topology(networks=networks)


def link_members(plan: TrialPlan) -> frozenset[str]:
    """Members of `link`: units in at least one network (`network_mode: none` units are in no network)."""
    return frozenset().union(*(net.members for net in plan.networks.values()))


def external_policy(plan: TrialPlan, unit: str, caller: ExternalPolicy) -> ExternalPolicy:
    """A unit's external policy: the caller's policy `caller` for units in `external_units`, `none` for the
    rest (internal-only networks, `network_mode: none`).

    `external_units` comes from the manifest; re-check against networks here so translation cannot let out a
    unit that is only on internal networks.
    """
    if unit not in plan.external_units:
        return _NONE
    if not any(unit in net.members and not net.internal for net in plan.networks.values()):
        raise ValueError(f"{unit} is in external_units but not on any non-internal network")
    return caller
