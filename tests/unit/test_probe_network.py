"""Network isolation controls cannot manufacture their own success.

Every check here allows the traffic, denies it, then allows it again. A single blocked exchange
proves nothing, so a missing positive control or a missing restore leaves the restriction
undemonstrated rather than passing -- and anything that arrives during the deny step is a leak no
matter what the other samples did.
"""

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
from flotilla.capabilities import Evidence, ExternalTarget
from flotilla.core.anchor import Anchor, AnchorSettings
from flotilla.platform.base import InstanceHandle, Platform
from flotilla.probe import ProbeSettings, _Run
from flotilla.testing.platform import FakePlatform, ManualClock

SETTINGS = ProbeSettings(
    image="r/probe@sha256:" + "1" * 64,
    share_release="r1",
    endpoint="http://os.test",
    link_members=9,
    hosts_recheck_s=0,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["none", "listener", "leak-tcp", "leak-udp", "no-positive", "no-restore"])
async def test_inbound_isolation_needs_allow_deny_and_restore(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    run = _Run(platform, clock, anchor, replace(SETTINGS, link_members=2), "p1")

    async def exchange(_run: _Run, source: InstanceHandle, host: str, port: int, protocol: str, token: str) -> str:
        if host == "127.0.0.1":
            # The listener positive control: without it nothing downstream means anything.
            return "blocked" if fault == "listener" else "received"
        if token.endswith("deny"):
            if fault == f"leak-{protocol}":
                return "received"
            return "blocked"
        if token.endswith("allow") and fault == "no-positive":
            return "blocked"
        if token.endswith("restore") and fault == "no-restore":
            return "blocked"
        return "received"

    async def policy(*args: object, **kwargs: object) -> None:
        pass

    monkeypatch.setattr(probe_network, "exchange", exchange)
    monkeypatch.setattr(probe_network, "policy", policy)
    inbound = await clock.run(probe_network.inbound_isolation(run))
    assert inbound.held is (fault == "none")
    if fault == "listener":
        assert "positive control" in inbound.observed
    if fault.startswith("leak-"):
        assert "stayed reachable" in inbound.observed
    await clock.run(run.cleanup())
    await anchor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["none", "missing", "tcp-only", "tcp-only-leak", "positive", "deny", "restore", "leak-restore"]
)
async def test_egress_requires_both_protocols_and_every_policy_transition(
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
    none, _worked = await clock.run(probe_network.egress(run))
    assert none.held is (fault == "none")
    if fault in ("deny", "tcp-only-leak", "leak-restore"):
        assert "stayed reachable under a deny policy" in none.observed
    if fault == "tcp-only":
        assert "both TCP and UDP" in none.observed
    if fault == "missing":
        assert "not configured" in none.observed
    await clock.run(run.cleanup())
    await anchor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("egress_held", [True, False])
@pytest.mark.parametrize("dns_held", [True, False])
async def test_acceptance_needs_both_egress_enforcement_and_measured_dns(
    platform: FakePlatform,
    clock: ManualClock,
    monkeypatch: pytest.MonkeyPatch,
    egress_held: bool,
    dns_held: bool,
) -> None:
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    run = _Run(platform, clock, anchor, SETTINGS, "p1")
    unit = await clock.run(run._create(run._spec("unit")))
    implicit = ("192.0.2.53:53/tcp", "192.0.2.53:53/udp")

    async def inbound(_run: _Run) -> Evidence:
        return Evidence(True, "test")

    async def egress(_run: _Run, *, mode: str = "allowlist") -> tuple[Evidence, bool]:
        return Evidence(egress_held, "test"), True

    async def resolvers_of(_run: _Run, _handle: InstanceHandle) -> tuple[Evidence, tuple[str, ...], tuple[str, ...]]:
        return Evidence(dns_held, "test"), ("192.0.2.53",), implicit

    monkeypatch.setattr(probe_network, "inbound_isolation", inbound)
    monkeypatch.setattr(probe_network, "egress", egress)
    monkeypatch.setattr(probe_network, "resolvers_of", resolvers_of)
    network = await clock.run(probe_network.measure_isolation(run, unit))
    assert network.network_acceptance.held is (egress_held and dns_held)
    # Cluster DNS answering under a deny policy is recorded, not treated as a leak.
    assert network.implicit_egress == implicit
    assert network.cluster_dns == ("192.0.2.53",)
    if network.network_acceptance.held:
        assert "not complete network disconnection" in network.network_acceptance.observed
    await clock.run(run.cleanup())
    await anchor.close()


@pytest.mark.asyncio
async def test_a_leak_under_any_mode_cannot_be_hidden_by_the_allowlist_controls(
    platform: FakePlatform, clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    anchor = Anchor(platform, clock, "Lp", AnchorSettings(image="anchor"))
    await clock.run(anchor.start())
    run = _Run(platform, clock, anchor, replace(SETTINGS, test_external_any=True), "p1")
    unit = await clock.run(run._create(run._spec("unit")))

    async def inbound(_run: _Run) -> Evidence:
        return Evidence(True, "test")

    async def egress(_run: _Run, *, mode: str = "allowlist") -> tuple[Evidence, bool]:
        if mode == "any":
            return Evidence(False, "an external target stayed reachable under a deny policy over ['tcp']"), True
        return Evidence(True, "test"), True

    async def resolvers_of(_run: _Run, _handle: InstanceHandle) -> tuple[Evidence, tuple[str, ...], tuple[str, ...]]:
        return Evidence(True, "test"), ("192.0.2.53",), ()

    monkeypatch.setattr(probe_network, "inbound_isolation", inbound)
    monkeypatch.setattr(probe_network, "egress", egress)
    monkeypatch.setattr(probe_network, "resolvers_of", resolvers_of)
    network = await clock.run(probe_network.measure_isolation(run, unit))
    assert not network.external_none.held
    assert not network.network_acceptance.held
    await clock.run(run.cleanup())
    await anchor.close()


def test_legacy_direct_settings_targets_normalize_to_echo() -> None:
    settings = replace(SETTINGS, external_targets=(("192.0.2.1", 48080, "udp"),))
    assert settings.external_targets == (ExternalTarget("192.0.2.1", 48080, "udp"),)


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
