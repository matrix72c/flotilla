"""Reading an image config from a registry: references, auth, caching, and what is refused."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from flotilla.registry import ImageConfigs, RegistryError

HOST = "registry.example.com"
NAME = f"{HOST}/ns/app"
DIGEST = "sha256:" + "a" * 64
BLOB = "sha256:" + "b" * 64
CHILD = "sha256:" + "c" * 64

CONFIG = {"Entrypoint": ["/entry"], "Cmd": ["serve"], "User": "0:0", "WorkingDir": "/app"}
IMAGE = {"architecture": "amd64", "os": "linux", "config": CONFIG}
MANIFEST = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "config": {"digest": BLOB}}
INDEX = {
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {"digest": "sha256:" + "d" * 64, "platform": {"os": "linux", "architecture": "arm64"}},
        {"digest": CHILD, "platform": {"os": "linux", "architecture": "amd64"}},
    ],
}


def transport(routes: dict[str, Any], *, record: list[str] | None = None) -> httpx.MockTransport:
    """Serve `routes` by path; anything else is a 404, which surfaces as a `RegistryError`."""

    def handle(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request.url.path)
        value = routes.get(request.url.path)
        if value is None:
            return httpx.Response(404, json={"errors": ["not found"]})
        if callable(value):
            response: httpx.Response = value(request)
            return response
        return httpx.Response(200, json=value)

    return httpx.MockTransport(handle)


def configs(routes: dict[str, Any], *, record: list[str] | None = None, **kwargs: Any) -> ImageConfigs:
    client = httpx.Client(transport=transport(routes, record=record), follow_redirects=True)
    return ImageConfigs(client=client, **kwargs)


ROUTES = {
    f"/v2/ns/app/manifests/{DIGEST}": MANIFEST,
    f"/v2/ns/app/blobs/{BLOB}": IMAGE,
}


def test_reads_the_config_section_of_the_image_config() -> None:
    with configs(ROUTES) as images:
        assert images.image_config(f"{NAME}@{DIGEST}") == CONFIG


def test_a_multi_platform_index_resolves_to_the_amd64_manifest() -> None:
    routes = {
        f"/v2/ns/app/manifests/{DIGEST}": INDEX,
        f"/v2/ns/app/manifests/{CHILD}": MANIFEST,
        f"/v2/ns/app/blobs/{BLOB}": IMAGE,
    }
    with configs(routes) as images:
        assert images.image_config(f"{NAME}@{DIGEST}") == CONFIG


def test_an_index_without_amd64_is_refused() -> None:
    index = {**INDEX, "manifests": INDEX["manifests"][:1]}
    with configs({f"/v2/ns/app/manifests/{DIGEST}": index}) as images, pytest.raises(RegistryError, match="amd64"):
        images.image_config(f"{NAME}@{DIGEST}")


def test_a_non_amd64_image_is_refused() -> None:
    routes = {**ROUTES, f"/v2/ns/app/blobs/{BLOB}": {**IMAGE, "architecture": "arm64"}}
    with configs(routes) as images, pytest.raises(RegistryError, match="linux/amd64"):
        images.image_config(f"{NAME}@{DIGEST}")


@pytest.mark.parametrize(
    "reference",
    [
        "registry.example.com/ns/app:latest",
        "registry.example.com/ns/app",
        f"registry.example.com/ns/app@md5:{'a' * 32}",
        f"registry.example.com/ns/app@sha256:{'a' * 63}",
        f"app@{DIGEST}",  # no registry host, so there is nothing to read from
    ],
)
def test_only_a_digest_pinned_reference_with_a_host_is_accepted(reference: str) -> None:
    """A tag can move between runs, so it cannot name the image a trial ran."""
    with configs(ROUTES) as images, pytest.raises(RegistryError):
        images.image_config(reference)


def test_one_read_per_digest_however_many_callers_ask() -> None:
    seen: list[str] = []
    with configs(ROUTES, record=seen) as images:
        for _ in range(3):
            assert images.image_config(f"{NAME}@{DIGEST}") == CONFIG
    assert seen == [f"/v2/ns/app/manifests/{DIGEST}", f"/v2/ns/app/blobs/{BLOB}"]


def test_a_stored_config_is_read_back_without_the_registry(tmp_path: Path) -> None:
    """A digest names one config forever, so a cached entry can never be stale."""
    with configs(ROUTES, store=tmp_path) as images:
        assert images.image_config(f"{NAME}@{DIGEST}") == CONFIG
    stored = tmp_path / f"{DIGEST.replace(':', '-')}.json"
    assert json.loads(stored.read_text()) == CONFIG

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a cached digest must not be fetched again: {request.url}")

    with configs({f"/v2/ns/app/manifests/{DIGEST}": refuse}, store=tmp_path) as images:
        assert images.image_config(f"{NAME}@{DIGEST}") == CONFIG


def test_a_challenge_is_answered_at_the_realm_the_registry_names() -> None:
    """The realm comes from the challenge: a registry may delegate to a different host, and its own
    conventional token endpoint can hand out anonymous tokens that fail on the next read."""
    issued: list[httpx.Request] = []

    def manifest(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != "Bearer real-token":
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="https://auth.example.com/token",service="svc",'
                    'scope="repository:ns/app:pull"'
                },
            )
        return httpx.Response(200, json=MANIFEST)

    def token(request: httpx.Request) -> httpx.Response:
        issued.append(request)
        return httpx.Response(200, json={"token": "real-token"})

    routes = {
        f"/v2/ns/app/manifests/{DIGEST}": manifest,
        f"/v2/ns/app/blobs/{BLOB}": IMAGE,
        "/token": token,
    }
    with configs(routes, credentials={HOST: ("user", "secret")}) as images:
        assert images.image_config(f"{NAME}@{DIGEST}") == CONFIG
    assert len(issued) == 1
    assert issued[0].url.host == "auth.example.com"
    assert dict(issued[0].url.params) == {"service": "svc", "scope": "repository:ns/app:pull"}
    assert issued[0].headers.get("Authorization", "").startswith("Basic ")


def test_a_realm_that_is_not_https_is_refused() -> None:
    def manifest(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer realm="http://auth.example.com/token"'})

    with (
        configs({f"/v2/ns/app/manifests/{DIGEST}": manifest}) as images,
        pytest.raises(RegistryError, match="https token realm"),
    ):
        images.image_config(f"{NAME}@{DIGEST}")


def test_a_realm_that_hands_back_no_token_is_refused() -> None:
    def manifest(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer realm="https://auth.example.com/token"'})

    routes = {f"/v2/ns/app/manifests/{DIGEST}": manifest, "/token": {"expires_in": 300}}
    with configs(routes) as images, pytest.raises(RegistryError, match="no token"):
        images.image_config(f"{NAME}@{DIGEST}")


def test_a_second_401_is_reported_rather_than_retried_forever() -> None:
    def manifest(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"WWW-Authenticate": 'Bearer realm="https://auth.example.com/token"'})

    routes = {f"/v2/ns/app/manifests/{DIGEST}": manifest, "/token": {"token": "wrong"}}
    with configs(routes) as images, pytest.raises(RegistryError, match=r"HTTP 401|refused the credentials"):
        images.image_config(f"{NAME}@{DIGEST}")


def test_a_manifest_without_a_config_blob_is_refused() -> None:
    routes = {f"/v2/ns/app/manifests/{DIGEST}": {"mediaType": MANIFEST["mediaType"], "layers": []}}
    with configs(routes) as images, pytest.raises(RegistryError, match="no config blob"):
        images.image_config(f"{NAME}@{DIGEST}")


def test_an_unreachable_registry_is_a_registry_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    client = httpx.Client(transport=httpx.MockTransport(fail))
    with ImageConfigs(client=client) as images, pytest.raises(RegistryError, match="cannot read"):
        images.image_config(f"{NAME}@{DIGEST}")
