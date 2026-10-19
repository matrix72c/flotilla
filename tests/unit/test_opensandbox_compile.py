"""Pure compilation and HTTP layer of the OpenSandbox backend: networkPolicy, host / pvc volumes, error mapping
and retries.

See docs/backends/opensandbox.md.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable

import httpx
import pytest

from flotilla.platform.base import ErrorCategory, ExternalPolicy, FlotillaError, SharedVolume
from flotilla.platform.opensandbox.http import Http, RetryPolicy, error_fields, map_status
from flotilla.platform.opensandbox.network import NetworkSettings, compile_policy
from flotilla.platform.opensandbox.storage import StorageSettings
from flotilla.platform.opensandbox.volumes import compile_host_volumes, compile_pvc_volumes
from tests.support.platform import ManualClock

NET = NetworkSettings(sandbox_cidrs=("10.0.0.0/16",), platform_cidrs=("192.168.0.0/24", "169.254.169.254/32"))
NONE = ExternalPolicy(mode="none")
ANY = ExternalPolicy(mode="any")


def _targets(policy: dict[str, object], action: str) -> list[str]:
    rules = policy["egress"]
    assert isinstance(rules, list)
    return [r["target"] for r in rules if r["action"] == action]


# ───────────────────────────── networkPolicy ─────────────────────────────


def test_none_at_create_is_deny_all() -> None:
    assert compile_policy(NONE, [], NET) == {"defaultAction": "deny", "egress": []}


def test_narrow_form_allows_only_peers() -> None:
    policy = compile_policy(NONE, ["10.0.0.7", "10.0.0.3", "10.0.0.3"], NET)
    assert policy == {
        "defaultAction": "deny",
        "egress": [{"action": "allow", "target": "10.0.0.3/32"}, {"action": "allow", "target": "10.0.0.7/32"}],
    }


def test_allowlist_hosts_and_cidrs_minus_sandbox() -> None:
    ext = ExternalPolicy(mode="allowlist", hosts=("gw.example.com",), cidrs=("10.0.0.0/8",))
    policy = compile_policy(ext, ["10.0.0.5"], NET)
    assert policy["defaultAction"] == "deny"
    assert _targets(policy, "deny") == []  # a single policy never mixes allow and deny
    allowed = _targets(policy, "allow")
    assert "10.0.0.5/32" in allowed and "gw.example.com" in allowed
    sandbox = ipaddress.ip_network("10.0.0.0/16")
    cidrs = [ipaddress.ip_network(t) for t in allowed if t[0].isdigit() and t != "10.0.0.5/32"]
    # External CIDR minus sandbox ranges: external rules cannot admit instances outside the group; the rest
    # still covers the remainder of 10/8.
    assert all(not n.overlaps(sandbox) for n in cidrs)
    assert sum(n.num_addresses for n in cidrs) == 2**24 - 2**16


def test_allowlist_rejects_any_internet_form() -> None:
    with pytest.raises(FlotillaError) as exc:
        compile_policy(ExternalPolicy(mode="allowlist", cidrs=("0.0.0.0/0",)), [], NET)
    assert exc.value.category is ErrorCategory.INVALID and "any" in str(exc.value)


def test_any_form_denies_sandbox_minus_peers_and_platform() -> None:
    policy = compile_policy(ANY, ["10.0.1.1"], NET)
    assert policy["defaultAction"] == "allow"
    assert _targets(policy, "allow") == []
    denied = [ipaddress.ip_network(t) for t in _targets(policy, "deny")]
    peer = ipaddress.ip_address("10.0.1.1")
    assert not any(peer in n for n in denied)  # peer carved out
    assert any(ipaddress.ip_address("10.0.1.2") in n for n in denied)  # all other sandbox addresses denied
    assert any(ipaddress.ip_address("169.254.169.254") in n for n in denied)
    assert any(ipaddress.ip_address("192.168.0.9") in n for n in denied)
    sandbox_part = sum(n.num_addresses for n in denied if n.overlaps(ipaddress.ip_network("10.0.0.0/16")))
    assert sandbox_part == 2**16 - 1


def test_any_at_create_denies_whole_sandbox_range() -> None:
    policy = compile_policy(ANY, [], NET)
    assert "10.0.0.0/16" in _targets(policy, "deny")


def test_any_requires_platform_cidrs() -> None:
    with pytest.raises(FlotillaError):
        compile_policy(ANY, [], NetworkSettings(sandbox_cidrs=("10.0.0.0/16",)))


def test_rule_limit_enforced_at_compile_time() -> None:
    small = NetworkSettings(sandbox_cidrs=("10.0.0.0/16",), platform_cidrs=("192.168.0.0/24",), max_rules=5)
    peers = [f"10.0.{i}.{i}" for i in range(1, 10)]
    with pytest.raises(FlotillaError) as exc:
        compile_policy(ANY, peers, small)
    assert exc.value.category is ErrorCategory.INVALID
    # The documented estimate: a /16 range with 10 members (9 peers) is far below the default limit.
    assert len(compile_policy(ANY, peers, NET)["egress"]) < 200


def test_bad_settings_rejected() -> None:
    with pytest.raises(ValueError):
        NetworkSettings(sandbox_cidrs=("not-a-cidr",))
    with pytest.raises(ValueError, match="sandbox_cidrs"):
        NetworkSettings()


def test_sandbox_targets_keep_external_ip_semantics_and_deduplicate_peers() -> None:
    settings = NetworkSettings(link="sandbox", sandbox_cidrs=("10.0.0.0/16",))
    ext = ExternalPolicy(mode="allowlist", hosts=("model.example.com",), cidrs=("10.0.0.9/32",))
    assert compile_policy(ext, ["sb-b", "sb-a", "sb-b"], settings) == {
        "defaultAction": "deny",
        "egress": [
            {"action": "allow", "target": "sandbox:sb-a"},
            {"action": "allow", "target": "sandbox:sb-b"},
            {"action": "allow", "target": "model.example.com"},
            {"action": "allow", "target": "10.0.0.9/32"},
        ],
    }
    assert compile_policy(NONE, [], settings) == {"defaultAction": "deny", "egress": []}


@pytest.mark.parametrize("peer", ["any", "project", "sandbox:sb-a", "10.0.0.1", "*", "", "sb/a"])
def test_sandbox_targets_reject_non_exact_ids(peer: str) -> None:
    with pytest.raises(FlotillaError, match="requires concrete instance IDs"):
        compile_policy(NONE, [peer], NetworkSettings(link="sandbox"))


def test_sandbox_link_cannot_silently_relax_isolation_or_rule_limit() -> None:
    with pytest.raises(FlotillaError, match="external = any"):
        compile_policy(ANY, ["sb-a"], NetworkSettings(link="sandbox"))
    with pytest.raises(FlotillaError, match="networkPolicy rule count 2 exceeds the deployment limit 1"):
        compile_policy(NONE, ["sb-a", "sb-b"], NetworkSettings(link="sandbox", max_rules=1))


# ───────────────────────────── host / pvc volumes (section 6) ─────────────────────────────

VOLS = [
    SharedVolume(key="share/releases/r1", mount_path="/.flotilla", read_only=True),
    SharedVolume(key="launches/L/t/static", mount_path="/srv", read_only=False),
    SharedVolume(key="", mount_path="/flotilla-root", read_only=False),
]


def test_host_volumes_compile_in_order() -> None:
    host = {"path": "/mnt/shared"}
    assert compile_host_volumes(VOLS, "/mnt/shared") == [
        {"name": "v0", "host": host, "mountPath": "/.flotilla", "readOnly": True, "subPath": "share/releases/r1"},
        {"name": "v1", "host": host, "mountPath": "/srv", "readOnly": False, "subPath": "launches/L/t/static"},
        {"name": "v2", "host": host, "mountPath": "/flotilla-root", "readOnly": False},
    ]


def test_pvc_volumes_prefix_root_subpath() -> None:
    # One volume per reference with unique names; upstream merges the same claim into one pod volume, and
    # readOnly applies per mount.
    pvc = {"claimName": "shared-root", "createIfNotExists": False}
    assert compile_pvc_volumes(VOLS, "shared-root", "opt/flotilla") == [
        {
            "name": "v0",
            "pvc": pvc,
            "mountPath": "/.flotilla",
            "readOnly": True,
            "subPath": "opt/flotilla/share/releases/r1",
        },
        {
            "name": "v1",
            "pvc": pvc,
            "mountPath": "/srv",
            "readOnly": False,
            "subPath": "opt/flotilla/launches/L/t/static",
        },
        # Anchor: the shared root itself, not the claim root.
        {"name": "v2", "pvc": pvc, "mountPath": "/flotilla-root", "readOnly": False, "subPath": "opt/flotilla"},
    ]


def test_pvc_volumes_at_claim_root() -> None:
    out = compile_pvc_volumes(VOLS, "shared-root", "")
    assert [v.get("subPath") for v in out] == ["share/releases/r1", "launches/L/t/static", None]


@pytest.mark.parametrize(
    ("vol", "host"),
    [
        (SharedVolume(key="a/../b", mount_path="/x", read_only=True), "/mnt"),
        (SharedVolume(key="a", mount_path="rel", read_only=True), "/mnt"),
        (SharedVolume(key="a", mount_path="/x", read_only=True), "relative"),
    ],
)
def test_host_volumes_reject_bad_paths(vol: SharedVolume, host: str) -> None:
    with pytest.raises(ValueError):
        compile_host_volumes([vol], host)


@pytest.mark.parametrize(
    ("vol", "claim", "root"),
    [
        (SharedVolume(key="a/../b", mount_path="/x", read_only=True), "c", ""),
        (SharedVolume(key="a", mount_path="rel", read_only=True), "c", ""),
        (SharedVolume(key="a", mount_path="/x", read_only=True), "Bad_Claim", ""),
        (SharedVolume(key="a", mount_path="/x", read_only=True), "", ""),
        (SharedVolume(key="a", mount_path="/x", read_only=True), "c", "/abs"),
        (SharedVolume(key="a", mount_path="/x", read_only=True), "c", "opt/../x"),
        (SharedVolume(key="a", mount_path="/x", read_only=True), "c", "opt/"),
    ],
)
def test_pvc_volumes_reject_bad_paths(vol: SharedVolume, claim: str, root: str) -> None:
    with pytest.raises(ValueError):
        compile_pvc_volumes([vol], claim, root)


# ───────────────────────────── Storage settings (section 6) ─────────────────────────────


def test_storage_settings_compiles_host_volumes() -> None:
    vol = SharedVolume(key="k", mount_path="/m", read_only=True)
    settings = StorageSettings(host_path="/mnt")
    assert settings.compile([vol])[0]["host"] == {"path": "/mnt"}
    assert settings.root == "host:/mnt"


def test_storage_settings_compiles_pvc_volumes() -> None:
    vol = SharedVolume(key="k", mount_path="/m", read_only=True)
    settings = StorageSettings(volumes="pvc", claim_name="data", root_subpath="opt/f")
    (entry,) = settings.compile([vol])
    assert entry["pvc"] == {"claimName": "data", "createIfNotExists": False}
    assert entry["subPath"] == "opt/f/k"
    assert settings.root == "pvc:data/opt/f"
    assert StorageSettings(volumes="pvc", claim_name="data").root == "pvc:data"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # host form missing host_path
        {"host_path": "relative"},
        {"host_path": "/mnt", "claim_name": "data"},  # host form does not take pvc fields
        {"volumes": "pvc"},  # pvc form missing claim_name
        {"volumes": "pvc", "claim_name": "data", "host_path": "/mnt"},
        {"volumes": "nfs", "host_path": "/mnt"},
    ],
)
def test_storage_settings_rejects_bad_combinations(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        StorageSettings(**kwargs)  # type: ignore[arg-type]


# ───────────────────────────── HTTP ─────────────────────────────


@pytest.mark.parametrize(
    ("body", "fields"),
    [
        ({"code": "QUOTA_EXCEEDED", "message": "quota"}, ("QUOTA_EXCEEDED", "quota")),  # upstream: flat
        ({"detail": {"code": "QUOTA_EXCEEDED", "message": "quota"}}, ("QUOTA_EXCEEDED", "quota")),  # FastAPI: wrapped
        ({"detail": "Not Found"}, (None, "Not Found")),  # FastAPI default string detail
        ({"detail": [{"loc": ["body"], "msg": "x"}]}, (None, None)),  # 422 validation error: don't guess
        ({"code": 3, "message": ""}, (None, None)),  # non-string / empty string not treated as a field
        (["not", "a", "mapping"], (None, None)),
        (None, (None, None)),
    ],
)
def test_error_fields_accepts_flat_and_wrapped(body: object, fields: tuple[str | None, str | None]) -> None:
    assert error_fields(body) == fields


@pytest.mark.parametrize(
    ("status", "code", "category", "retryable"),
    [
        (429, None, ErrorCategory.RATE_LIMITED, True),
        (403, "QUOTA_EXCEEDED", ErrorCategory.CAPACITY, True),
        (403, "FORBIDDEN", ErrorCategory.INVALID, False),
        (401, None, ErrorCategory.INVALID, False),
        (400, None, ErrorCategory.INVALID, False),
        (413, None, ErrorCategory.INVALID, False),
        (404, None, ErrorCategory.NOT_FOUND, False),
        (500, None, ErrorCategory.TRANSIENT, True),
        (503, None, ErrorCategory.TRANSIENT, True),
    ],
)
def test_map_status(status: int, code: str | None, category: ErrorCategory, retryable: bool) -> None:
    assert map_status(status, code) == (category, retryable)


def _http(handler: Callable[[httpx.Request], httpx.Response], clock: ManualClock) -> Http:
    client = httpx.AsyncClient(base_url="http://os.test/v1", transport=httpx.MockTransport(handler))
    return Http(client, clock, RetryPolicy(attempts=4, initial_s=1.0, cap_s=4.0))


@pytest.mark.asyncio
async def test_idempotent_request_retries_transient_then_succeeds(clock: ManualClock) -> None:
    calls: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(clock.monotonic())
        if len(calls) < 3:
            return httpx.Response(503, json={"code": "UNAVAILABLE", "message": "busy"})
        return httpx.Response(200, json={"ok": True})

    http = _http(handler, clock)
    assert await clock.run(http.request("GET", "/sandboxes/x", stage="run", idempotent=True)) == {"ok": True}
    assert calls == [0.0, 1.0, 3.0]  # exponential backoff: 1s, 2s


@pytest.mark.asyncio
async def test_retry_after_is_honoured(clock: ManualClock) -> None:
    calls: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(clock.monotonic())
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(204)

    assert await clock.run(_http(handler, clock).request("DELETE", "/x", stage="run", idempotent=True)) is None
    assert calls == [0.0, 7.0]


@pytest.mark.asyncio
async def test_non_idempotent_request_is_not_retried(clock: ManualClock) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_http(handler, clock).request("POST", "/sandboxes", stage="create", idempotent=False))
    assert calls == 1 and exc.value.category is ErrorCategory.TRANSIENT and exc.value.stage == "create"


@pytest.mark.asyncio
async def test_definite_error_is_not_retried(clock: ManualClock) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": "no such sandbox"})

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_http(handler, clock).request("GET", "/sandboxes/x", stage="run", idempotent=True))
    assert calls == 1 and exc.value.category is ErrorCategory.NOT_FOUND
    assert "no such sandbox" not in str(exc.value)


@pytest.mark.asyncio
async def test_connection_error_is_transient_and_exhausts(clock: ManualClock) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("refused")

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_http(handler, clock).request("GET", "/x", stage="run", idempotent=True))
    assert calls == 4 and exc.value.category is ErrorCategory.TRANSIENT and exc.value.retryable


@pytest.mark.asyncio
async def test_credentials_never_in_error_message(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "UNAUTHORIZED", "message": "bad key"})

    client = httpx.AsyncClient(
        base_url="http://os.test/v1",
        transport=httpx.MockTransport(handler),
        headers={"OPEN-SANDBOX-API-KEY": "ak:SECRET"},
    )
    with pytest.raises(FlotillaError) as exc:
        await clock.run(Http(client, clock).request("GET", "/sandboxes", stage="run", idempotent=True))
    assert "SECRET" not in str(exc.value)


@pytest.mark.asyncio
async def test_wrapped_quota_error_maps_to_capacity(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": {"code": "QUOTA_EXCEEDED", "message": "project quota"}})

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_http(handler, clock).request("POST", "/sandboxes", stage="create", idempotent=False))
    assert exc.value.category is ErrorCategory.CAPACITY and exc.value.retryable
    assert "QUOTA_EXCEEDED" in str(exc.value) and "project quota" not in str(exc.value)


@pytest.mark.asyncio
async def test_non_json_error_body_falls_back_to_text(clock: ManualClock) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(FlotillaError) as exc:
        await clock.run(_http(handler, clock).request("POST", "/x", stage="run", idempotent=False))
    assert exc.value.category is ErrorCategory.TRANSIENT and "bad gateway" not in str(exc.value)
