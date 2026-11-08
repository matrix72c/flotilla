"""Nine-member coverage and network controls cannot manufacture isolation success."""

import os
import socket
import ssl
import struct
import sys
import time
from dataclasses import replace
from typing import Any

import pytest

from flotilla import probe_network
from flotilla.capabilities import ExternalTarget, Item
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.platform.base import ExecResult, InstanceHandle, Platform, ProcessSpec
from flotilla.probe import ProbeSettings, _Run
from flotilla.testing.platform import FakePlatform, ManualClock

SETTINGS = ProbeSettings(
    image="r/probe@sha256:" + "1" * 64,
    share_release="r1",
    endpoint="http://os.test",
    evidence_profile="test",
    link_members=9,
    hosts_recheck_s=0,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("control_failure", [False, True])
async def test_nine_members_all_directed_protocol_pairs_and_controls(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch, control_failure: bool
) -> None:
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    run = _Run(platform, clock, anchor, SETTINGS, "p1")
    pairs = set()
    policies = []

    async def exchange(_run: _Run, source: InstanceHandle, host: str, port: int, protocol: str, token: str) -> str:
        if ":" in token:
            pairs.add((protocol, token.split("/", 1)[-1]))
        if token.endswith("restore") and control_failure:
            return "blocked"
        return "blocked" if token.endswith(("deny", "unlinked")) else "received"

    async def policy(_platform: Platform, target: InstanceHandle, **kwargs: Any) -> None:
        policies.append((target.iid, kwargs))

    monkeypatch.setattr(probe_network, "exchange", exchange)
    monkeypatch.setattr(probe_network, "policy", policy)
    tcp, udp, inbound = await clock.run(probe_network.links(run))
    assert tcp.ok and udp.ok
    assert tcp.measurements["members"] == 9
    assert tcp.measurements["outcomes"] == udp.measurements["outcomes"] == {"received": 72}
    assert tcp.measurements["failed_pairs"] == udp.measurements["failed_pairs"] == []
    assert len(pairs) == 9 * 8 * 2
    assert inbound.status == ("error" if control_failure else "passed")
    denied = [kwargs for _, kwargs in policies if kwargs["ingress"] == ()]
    assert denied and all(kwargs["egress"] for kwargs in denied)
    await clock.run(run.cleanup())
    await anchor.close()


@pytest.mark.asyncio
async def test_listener_unavailable_cannot_prove_any_isolation(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    run = _Run(platform, clock, anchor, replace(SETTINGS, link_members=2), "p1")

    async def exchange(*_args: object) -> str:
        return "blocked"

    monkeypatch.setattr(probe_network, "exchange", exchange)
    results = await clock.run(probe_network.links(run))
    assert all(item.status == "error" for item in results)
    await clock.run(run.cleanup())
    await anchor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["blocked", "error", "wrong-content"])
async def test_failed_peer_pairs_retain_bounded_nonsecret_evidence(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    run = _Run(platform, clock, anchor, replace(SETTINGS, link_members=2), "p1")

    async def exchange(_run: _Run, source: InstanceHandle, host: str, port: int, protocol: str, token: str) -> str:
        if token.endswith(":0:1:udp"):
            return outcome
        return "blocked" if token.endswith(("deny", "unlinked")) else "received"

    async def policy(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(probe_network, "exchange", exchange)
    monkeypatch.setattr(probe_network, "policy", policy)
    tcp, udp, inbound = await clock.run(probe_network.links(run))
    assert tcp.status == "passed"  # independent protocol and inbound controls still run
    assert tcp.measurements["outcomes"] == {"received": 2}
    assert udp.status == ("error" if outcome == "error" else "failed")
    assert udp.measurements["outcomes"] == {outcome: 1, "received": 1}
    assert udp.measurements["failed_pairs"] == [{"source": "0", "target": "1", "outcome": outcome}]
    assert "p1" not in udp.model_dump_json()
    assert udp.measurements["samples"][0]["outcomes"] == [outcome] * 3
    assert inbound.status == "passed"
    await clock.run(run.cleanup())
    await anchor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["packet-miss", "command-error", "inbound-leak", "inbound-leak-restore", "disconnect-leak-restore"]
)
async def test_fixed_udp_samples_retain_misses_errors_and_any_deny_leak(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    run = _Run(platform, clock, anchor, replace(SETTINGS, link_members=2), "p1")
    calls: dict[str, list[str]] = {}

    async def exchange(_run: _Run, source: InstanceHandle, host: str, port: int, protocol: str, token: str) -> str:
        if host == "127.0.0.1":
            return "received"
        index, phase = token.split("/", 1)
        calls.setdefault(protocol + phase, []).append(index)
        if protocol == "udp" and index == "0" and phase.endswith(":0:1:udp"):
            return "error" if fault == "command-error" else "blocked"
        if phase.endswith(("deny", "unlinked")):
            leak = (fault.startswith("inbound-leak") and phase.endswith("deny")) or (
                fault == "disconnect-leak-restore" and phase.endswith("unlinked")
            )
            return "received" if protocol == "udp" and index == "1" and leak else "blocked"
        if protocol == "udp" and (
            (fault == "inbound-leak-restore" and phase.endswith("restore"))
            or (fault == "disconnect-leak-restore" and phase.endswith("linked"))
        ):
            return "blocked"
        return "received"

    async def policy(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(probe_network, "exchange", exchange)
    monkeypatch.setattr(probe_network, "policy", policy)
    tcp, udp, inbound = await clock.run(probe_network.links(run))
    assert tcp.ok
    expected = "error" if fault == "command-error" else "failed" if fault == "disconnect-leak-restore" else "passed"
    assert udp.status == expected
    assert inbound.status == ("failed" if fault.startswith("inbound-leak") else "passed")
    assert udp.measurements["samples"][0]["outcomes"] == [
        "error" if fault == "command-error" else "blocked",
        "received",
        "received",
    ]
    assert all(indices == (["0", "1", "2"] if key.startswith("udp") else ["0"]) for key, indices in calls.items())
    assert inbound.measurements["controls"][1]["denied"] == (
        ["blocked", "received", "blocked"] if fault.startswith("inbound-leak") else ["blocked"] * 3
    )
    await clock.run(run.cleanup())
    await anchor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver_controls", [False, True])
@pytest.mark.parametrize("dns_exceptions", [(), ("192.0.2.53:53/tcp", "192.0.2.53:53/udp")])
@pytest.mark.parametrize(
    ("external_item", "dns_item"),
    [
        (Item(status="passed"), Item(status="passed")),
        (Item(status="skipped"), Item(status="passed")),
        (Item(status="error"), Item(status="passed")),
        (Item(status="failed"), Item(status="passed")),
        (Item(status="passed"), Item(status="skipped")),
        (Item(status="passed"), Item(status="error")),
        (Item(status="passed"), Item(status="failed")),
    ],
)
async def test_external_acceptance_allows_measured_dns_but_requires_external_controls(
    platform: FakePlatform,
    clock: ManualClock,
    monkeypatch: pytest.MonkeyPatch,
    resolver_controls: bool,
    dns_exceptions: tuple[str, ...],
    external_item: Item,
    dns_item: Item,
) -> None:
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    targets: tuple[tuple[str, int, str], ...] = (("192.0.2.1", 48080, "tcp"), ("192.0.2.1", 48080, "udp"))
    if resolver_controls:
        targets += (("192.0.2.53", 48080, "tcp"), ("192.0.2.53", 48080, "udp"))
    run = _Run(platform, clock, anchor, replace(SETTINGS, external_targets=targets), "p1")
    unit = await clock.run(run._create(run._spec("unit")))
    passed = Item(status="passed")

    async def links(_run: _Run) -> tuple[Item, Item, Item]:
        return passed, passed, passed

    async def egress(_run: _Run) -> tuple[Item, Item]:
        return external_item, external_item

    async def dns(_run: _Run, _handle: InstanceHandle) -> tuple[Item, tuple[str, ...], tuple[str, ...]]:
        return dns_item, ("192.0.2.53",), dns_exceptions

    monkeypatch.setattr(probe_network, "links", links)
    monkeypatch.setattr(probe_network, "egress", egress)
    monkeypatch.setattr(probe_network, "dns", dns)
    network = await clock.run(probe_network.measure_network(run, unit))
    assert network.network_acceptance.ok is (external_item.ok and dns_item.ok)
    assert network.implicit_egress == dns_exceptions
    assert network.cluster_dns == ("192.0.2.53",)
    expected = ("passed" if external_item.ok else "error") if resolver_controls else "skipped"
    assert network.resolver_non_dns.status == expected
    if network.network_acceptance.ok:
        assert "not complete network disconnection" in network.network_acceptance.evidence
    await clock.run(run.cleanup())
    await anchor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["none", "missing", "tcp-only", "tcp-only-leak", "positive", "deny", "restore", "leak-restore"]
)
async def test_external_echo_controls_still_require_both_protocols_and_policy_transitions(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    protocols = () if fault == "missing" else (("tcp",) if fault.startswith("tcp-only") else ("tcp", "udp"))
    targets = tuple(("192.0.2.1", 48080, protocol) for protocol in protocols)
    run = _Run(platform, clock, anchor, replace(SETTINGS, external_targets=targets), "p1")

    async def exchange(_run: _Run, source: InstanceHandle, host: str, port: int, protocol: str, token: str) -> str:
        if fault == "positive" and token.endswith("external"):
            return "blocked"
        if fault in ("restore", "leak-restore") and token.endswith("restored"):
            return "blocked"
        if token.endswith("denied"):
            return "received" if fault in ("deny", "tcp-only-leak", "leak-restore") else "blocked"
        return "received"

    async def policy(_platform: Platform, target: InstanceHandle, **kwargs: Any) -> None:
        pass

    monkeypatch.setattr(probe_network, "exchange", exchange)
    monkeypatch.setattr(probe_network, "policy", policy)
    none, _allow = await clock.run(probe_network.egress(run))
    assert none.ok is (fault == "none")
    if fault in ("deny", "tcp-only-leak", "leak-restore"):
        assert none.status == "failed"
    await clock.run(run.cleanup())
    await anchor.close()


@pytest.mark.asyncio
async def test_later_any_mode_deny_leak_cannot_enable_network_acceptance(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    run = _Run(platform, clock, anchor, replace(SETTINGS, test_external_any=True), "p1")
    unit = await clock.run(run._create(run._spec("unit")))
    passed = Item(status="passed")
    leak = Item(status="failed", evidence="deny response")

    async def links(_run: _Run) -> tuple[Item, Item, Item]:
        return passed, passed, passed

    async def egress(_run: _Run, *, mode: str = "allowlist") -> tuple[Item, Item]:
        return (leak if mode == "any" else passed), passed

    async def dns(_run: _Run, _handle: InstanceHandle) -> tuple[Item, tuple[str, ...], tuple[str, ...]]:
        return passed, ("192.0.2.53",), ()

    monkeypatch.setattr(probe_network, "links", links)
    monkeypatch.setattr(probe_network, "egress", egress)
    monkeypatch.setattr(probe_network, "dns", dns)
    network = await clock.run(probe_network.measure_network(run, unit))
    assert network.external_none.status == "failed"
    assert not network.network_acceptance.ok
    await clock.run(run.cleanup())
    await anchor.close()


def test_legacy_direct_settings_targets_normalize_to_echo() -> None:
    settings = replace(SETTINGS, external_targets=(("192.0.2.1", 48080, "udp"),))
    assert settings.external_targets == (ExternalTarget("192.0.2.1", 48080, "udp"),)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["allowlist", "any"])
@pytest.mark.parametrize(
    "fault", ["none", "resolve", "positive", "wrong-positive", "deny", "wrong-deny", "restore", "cert"]
)
async def test_standard_services_pin_address_and_require_all_phases(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch, mode: str, fault: str
) -> None:
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    targets = (
        ExternalTarget("https.example.com", 443, "tcp", "https"),
        ExternalTarget("ntp.example.com", 123, "udp", "ntp"),
    )
    run = _Run(platform, clock, anchor, replace(SETTINGS, external_targets=targets), "p1")
    resolutions: list[str] = []
    exchanges: dict[str, list[tuple[str, ...]]] = {"https": [], "ntp": []}
    policies = []

    def execute(_iid: str, proc: ProcessSpec) -> ExecResult:
        script, *args = proc.argv[3:]
        if script == probe_network._RESOLVE:
            resolutions.append(args[0])
            return ExecResult(0, b"bad-address" if fault == "resolve" else b"192.0.2.1", b"")
        assert script == probe_network._STANDARD_CLIENT
        _ip, _port, service, _host = args
        exchanges[service].append(tuple(args))
        phase = len(exchanges[service])
        value = "blocked" if phase == 2 else "received"
        if service == "https":
            if (fault == "positive" and phase == 1) or (fault == "restore" and phase == 3):
                value = "blocked"
            elif fault == "wrong-positive" and phase == 1:
                value = "wrong-content"
            elif fault == "cert":
                value = "error"
            elif fault in ("deny", "wrong-deny") and phase == 2:
                value = "received" if fault == "deny" else "wrong-content"
        return ExecResult(0, value.encode(), b"")

    async def policy(_platform: Platform, target: InstanceHandle, **kwargs: Any) -> None:
        policies.append(kwargs["external"])

    platform.set_exec_handler(execute)
    monkeypatch.setattr(probe_network, "policy", policy)
    none, allow = await clock.run(probe_network.egress(run, mode=mode))
    assert none.ok is (fault == "none")
    assert allow.ok is (fault in ("none", "deny", "wrong-deny"))
    assert none.status == ("passed" if fault == "none" else "failed" if fault in ("deny", "wrong-deny") else "error")
    assert len(resolutions) == len(set(resolutions))  # No DNS retry or re-resolution on deny/restore.
    for service, calls in exchanges.items():
        if calls:
            assert len(set(calls)) == 1
            assert calls[0] == ("192.0.2.1", "443" if service == "https" else "123", service, service + ".example.com")
    if fault == "none":
        assert all(len(calls) == 3 for calls in exchanges.values())
        assert [item["service"] for item in none.measurements["targets"]] == ["https", "ntp"]
        assert all(item["address"] == "192.0.2.1" for item in none.measurements["targets"])
        assert [item.mode for item in policies].count("none") == 2
        if mode == "allowlist":
            assert all(item.cidrs == ("192.0.2.1",) for item in policies if item.mode == "allowlist")
    await clock.run(run.cleanup())
    await anchor.close()


def _run_service_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], service: str, fault: str
) -> tuple[str, bytes]:
    """Execute the exact sandbox script against offline socket/TLS doubles, never the network."""
    sent = []
    peer = []
    response = bytearray()

    class Socket:
        def __enter__(self) -> "Socket":
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def settimeout(self, timeout: float) -> None:
            assert 0 < timeout <= 3

        def connect(self, address: tuple[str, int]) -> None:
            peer.append(address)
            if fault == "blocked":
                raise TimeoutError

        def sendall(self, request: bytes) -> None:
            sent.append(request)
            if service == "https":
                responses = {
                    "none": b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n",
                    "http-error": b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n\r\n",
                    "wrong": b"an echo is not HTTP\r\n\r\n",
                    "status": b"HTTP/1.1 999 Bad\r\n\r\n",
                    "header": b"HTTP/1.1 200 OK\r\nnot a header\r\n\r\n",
                    "short": b"HTTP/1.1 200 OK\r\n",
                    "oversized": b"HTTP/1.1 200 OK\r\nX: " + b"a" * 16384 + b"\r\n\r\n",
                }
                response.extend(responses.get(fault, responses["none"]))
            else:
                data = bytearray(48)
                data[0] = 0x24
                data[1] = 2
                data[24:32] = request[40:48]
                data[40:48] = struct.pack("!II", 4000000000, 123)
                if fault == "mode":
                    data[0] = 0x23
                elif fault == "version":
                    data[0] = 0x04
                elif fault == "unsynced":
                    data[0] |= 0xC0
                elif fault == "stratum":
                    data[1] = 0
                elif fault == "stratum-high":
                    data[1] = 16
                elif fault == "originate":
                    data[24:32] = bytes(8)
                elif fault == "transmit":
                    data[40:48] = bytes(8)
                elif fault == "short":
                    data = data[:47]
                response.extend(data)

        def recv(self, size: int) -> bytes:
            if fault == "stall":
                raise TimeoutError
            # Short TCP reads exercise the header accumulation path.
            size = min(size, 37) if service == "https" else size
            result = bytes(response[:size])
            del response[:size]
            return result

    def wrap(raw: Socket, *, server_hostname: str) -> Socket:
        assert server_hostname == "control.example.com"
        if fault == "cert":
            raise ssl.SSLCertVerificationError("certificate mismatch")
        return raw

    verified_context = ssl.create_default_context()
    monkeypatch.setattr(verified_context, "wrap_socket", wrap)

    def context() -> ssl.SSLContext:
        return verified_context

    def no_dns(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the phase client must not resolve again")

    monkeypatch.setattr(
        sys, "argv", ["client", "192.0.2.1", "443" if service == "https" else "123", service, "control.example.com"]
    )
    monkeypatch.setattr(socket, "socket", lambda *_args: Socket())
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    monkeypatch.setattr(ssl, "create_default_context", context)
    monkeypatch.setattr(time, "time", lambda: 1700000000.25)
    monkeypatch.setattr(
        os, "urandom", lambda n: b"\x12\x34\x56\x78" if n == 4 else pytest.fail("unexpected random size")
    )
    exec(compile(probe_network._STANDARD_CLIENT, "probe-service-client", "exec"), {})
    assert verified_context.check_hostname and verified_context.verify_mode == ssl.CERT_REQUIRED
    assert peer == [("192.0.2.1", 443 if service == "https" else 123)]
    return capsys.readouterr().out.strip(), sent[0] if sent else b""


@pytest.mark.parametrize(
    "fault,expected",
    [
        ("none", "received"),
        ("http-error", "received"),
        ("wrong", "wrong-content"),
        ("status", "wrong-content"),
        ("header", "wrong-content"),
        ("short", "wrong-content"),
        ("oversized", "wrong-content"),
        ("cert", "error"),
        ("blocked", "blocked"),
        ("stall", "wrong-content"),
    ],
)
def test_https_client_requires_verified_tls_and_bounded_http_response(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fault: str, expected: str
) -> None:
    outcome, request = _run_service_client(monkeypatch, capsys, "https", fault)
    assert outcome == expected
    if request:
        assert request == b"HEAD / HTTP/1.1\r\nHost: control.example.com\r\nConnection: close\r\n\r\n"


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "mode",
        "version",
        "unsynced",
        "stratum",
        "stratum-high",
        "originate",
        "transmit",
        "short",
        "blocked",
        "stall",
    ],
)
def test_ntp_client_validates_timestamp_response(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fault: str
) -> None:
    outcome, request = _run_service_client(monkeypatch, capsys, "ntp", fault)
    assert outcome == (
        "received" if fault == "none" else "blocked" if fault in ("blocked", "stall") else "wrong-content"
    )
    if request:
        assert len(request) == 48 and request[0] == 0x23
        assert request[40:44] == struct.pack("!I", 1700000000 + 2208988800)
        assert request[44:48] == b"\x12\x34\x56\x78"
