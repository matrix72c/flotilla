"""What isolation evidence means, and what it refuses to imply.

Two rules carry the weight. A restriction that was not measured is not evidence that it holds --
there is no status that turns "could not check" into a pass. And evidence from an ordinary sandbox
says nothing about the privileged runtime, which is a different isolation boundary, so admitting a
privileged service needs its own measurements.
"""

from __future__ import annotations

import pytest

from flotilla.capabilities import ISOLATION_PROPERTIES, Evidence, IsolationEvidence
from flotilla.testing.config import config
from flotilla.testing.reports import evidence, no, ok


def test_nothing_measured_means_nothing_held() -> None:
    blank = IsolationEvidence()
    assert blank.gaps() == list(ISOLATION_PROPERTIES)
    assert not blank.privileged_isolated()
    assert not blank.cleanup.held
    # The absence of a measurement reads as an absence, not an empty success.
    assert all(not getattr(blank, name).held for name in ISOLATION_PROPERTIES)


def test_a_failed_check_names_itself_as_the_gap() -> None:
    assert evidence(exec_auth=no()).gaps() == ["exec_auth"]
    assert evidence().gaps() == []


@pytest.mark.parametrize("name", ISOLATION_PROPERTIES)
def test_each_property_is_its_own_gap(name: str) -> None:
    assert evidence(**{name: no()}).gaps() == [name]


def test_an_accepted_gap_is_the_only_way_past_it() -> None:
    gap = evidence(no_auto_mount=no())
    with pytest.raises(Exception, match="no_auto_mount"):
        config().check_isolation(gap)
    config(security={"accept_insecure": ["no_auto_mount"]}).check_isolation(gap)


def test_unconfirmed_cleanup_is_refused_and_cannot_be_waived() -> None:
    """A deployment whose resources cannot be confirmed deleted leaks every run."""
    unclean = evidence(cleanup=no())
    with pytest.raises(Exception, match="cleanup"):
        config().check_isolation(unclean)
    with pytest.raises(Exception, match="cleanup"):
        config(security={"accept_insecure": ["no_auto_mount", "exec_auth"]}).check_isolation(unclean)


@pytest.mark.parametrize(
    "check",
    ["privileged_storage", "privileged_network", "privileged_exec_auth", "privileged_no_auto_mount"],
)
def test_privileged_isolation_needs_its_own_measurements(check: str) -> None:
    assert evidence().privileged_isolated()
    assert not evidence(**{check: no()}).privileged_isolated()
    # Ordinary evidence for the same property does not stand in for it.
    assert not evidence(**{check: Evidence(False, "measured on an ordinary sandbox")}).privileged_isolated()


def test_a_waived_property_is_waived_for_the_privileged_runtime_too() -> None:
    """Refusing the privileged runtime alone does not make a deployment without exec_auth safer."""
    gap = evidence(exec_auth=no(), privileged_exec_auth=no())
    assert not gap.privileged_isolated()
    assert gap.privileged_isolated(frozenset({"exec_auth"}))
    # The two required ones have no waiver, because nothing else compensates for them.
    assert not evidence(privileged_storage=no()).privileged_isolated(frozenset({"no_auto_mount", "exec_auth"}))


def test_an_unexercised_egress_form_is_reported_not_assumed() -> None:
    cfg = config(network={"external": ["gateway.example.com"]})
    with pytest.raises(Exception, match="allowlist"):
        cfg.check_isolation(evidence(external_forms=frozenset({"none"})))
    cfg.check_isolation(evidence(external_forms=frozenset({"none", "allowlist"})))


def test_cluster_dns_addresses_are_normalized() -> None:
    assert evidence(cluster_dns=("10.96.0.10",)).cluster_dns == ("10.96.0.10",)
    with pytest.raises(ValueError, match="unicast"):
        IsolationEvidence(cluster_dns=("0.0.0.0",))
    with pytest.raises(ValueError, match="IPv4 or IPv6"):
        IsolationEvidence(cluster_dns=("10.96.0.0/16",))


def test_evidence_reads_as_a_boolean() -> None:
    assert ok() and not no()
