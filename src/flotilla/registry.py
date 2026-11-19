"""Read image configs straight from a registry, by digest, over HTTP.

A sandbox is created from one image and an argv, and the platform does not read the image's
config to find that argv: omitting it leaves the sandbox running with nothing to talk to. So
flotilla has to supply `Entrypoint`, `Cmd`, `WorkingDir` and `User` itself, and the only place
they exist is the image config in the registry.

Two reads per image, both small: the manifest, then the config blob it names. No layer is
fetched, nothing is pulled, and no Docker daemon or CLI is involved -- `docker buildx imagetools
inspect` used to do this and needed a working Docker installation to read a few kilobytes of
JSON.

Only digests are accepted. A digest names one config forever, which is what makes the cache
below correct with no expiry: an entry can never go stale, so there is no version to track and
no invalidation to get wrong. Resolving a tag would also mean two services naming the same tag
could land on different images within one run.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx

#: `repo@sha256:<64 hex>`, the only form accepted.
PINNED = re.compile(r"(?P<name>[^\s@]+)@(?P<digest>sha256:[0-9a-f]{64})")

#: Manifest media types that are read. A list or index names per-platform manifests; `linux/amd64`
#: is selected from it, since that is what the platform runs.
_MANIFEST = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
_INDEX = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)

_ACCEPT = ", ".join((*_MANIFEST, *_INDEX))

#: Reading a config blob is a single small GET, but a registry behind a gateway can be slow to
#: authenticate, so this is generous rather than tight.
TIMEOUT_S = 60.0


class RegistryError(ValueError):
    """An image config could not be read: a bad reference, an unreachable registry, a refused read."""


class ImageConfigs:
    """Image configs by digest, cached in memory and optionally on disk.

    Safe to share between threads: concurrent tasks routinely name the same base image, and the
    first reader does the work while the rest wait on it rather than issuing the same request.
    """

    def __init__(
        self,
        *,
        credentials: Mapping[str, tuple[str, str]] | None = None,
        store: Path | None = None,
        client: httpx.Client | None = None,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        """`credentials` maps a registry host to `(username, password)`, used only to obtain a token.

        They are never written to `store`, which holds image configs alone.
        """
        self._credentials = dict(credentials or {})
        self._store = store
        self._timeout_s = timeout_s
        self._client = client
        self._owned = client is None
        self._memory: dict[str, Mapping[str, Any]] = {}
        #: One token per repository, since a registry scopes a token to the repository it was asked for.
        self._tokens: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        self._loading: dict[str, threading.Event] = {}

    def __enter__(self) -> ImageConfigs:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owned and self._client is not None:
            self._client.close()
            self._client = None

    def prewarm(self, pinned: Iterable[str]) -> None:
        """Read every config now, so a later caller finds them all in memory."""
        for reference in dict.fromkeys(pinned):
            self.image_config(reference)

    def image_config(self, pinned: str) -> Mapping[str, Any]:
        """The `config` section of `pinned`'s image config (`Entrypoint`, `Cmd`, `User`, ...)."""
        name, digest = _parse(pinned)
        while True:
            with self._lock:
                cached = self._memory.get(digest)
                if cached is not None:
                    return cached
                waiting = self._loading.get(digest)
                if waiting is None:
                    done = threading.Event()
                    self._loading[digest] = done
                    break
            # Another thread is reading this digest; wait for it rather than repeat the request.
            waiting.wait()
        try:
            config = self._load(name, digest)
            with self._lock:
                self._memory[digest] = config
            return config
        finally:
            with self._lock:
                del self._loading[digest]
            done.set()

    def _load(self, name: str, digest: str) -> Mapping[str, Any]:
        path = None if self._store is None else self._store / f"{digest.replace(':', '-')}.json"
        if path is not None and path.is_file():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RegistryError(f"cached image config {path.name} is unreadable: {exc}") from exc
            if not isinstance(stored, dict):
                raise RegistryError(f"cached image config {path.name} is not an object")
            return stored
        config = self._fetch(name, digest)
        if path is not None:
            _store(path, config)
        return config

    def _fetch(self, name: str, digest: str) -> Mapping[str, Any]:
        host, repository = _split(name)
        manifest = self._json(host, repository, f"manifests/{digest}", accept=_ACCEPT)
        if str(manifest.get("mediaType") or "") in _INDEX or "manifests" in manifest:
            child = _amd64(manifest, name)
            manifest = self._json(host, repository, f"manifests/{child}", accept=_ACCEPT)
        blob = manifest.get("config")
        if not isinstance(blob, Mapping) or not isinstance(blob.get("digest"), str):
            raise RegistryError(f"manifest of {name}@{digest} names no config blob")
        image = self._json(host, repository, f"blobs/{blob['digest']}", accept="*/*")
        if image.get("architecture", "amd64") != "amd64" or image.get("os", "linux") != "linux":
            raise RegistryError(f"{name}@{digest} is not a linux/amd64 image")
        config = image.get("config")
        if not isinstance(config, Mapping):
            raise RegistryError(f"image config of {name}@{digest} has no config section")
        return dict(config)

    def _json(self, host: str, repository: str, path: str, *, accept: str) -> Mapping[str, Any]:
        response = self._get(host, repository, path, accept=accept)
        try:
            value = response.json()
        except ValueError as exc:
            raise RegistryError(f"{host}/v2/{repository}/{path} did not return JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise RegistryError(f"{host}/v2/{repository}/{path} did not return an object")
        return value

    def _get(self, host: str, repository: str, path: str, *, accept: str) -> httpx.Response:
        """One registry read, authenticating on demand.

        A blob read is answered with a redirect to storage on a different host often enough that
        it is the normal case, and the token must not travel there: a signed storage URL carries
        its own authorization, and an unexpected `Authorization` header makes some backends answer
        400 rather than ignore it -- a failure that reads like a malformed request, not a wrong
        credential.
        """
        url = f"https://{host}/v2/{repository}/{path}"
        headers = {"Accept": accept}
        token = self._tokens.get((host, repository))
        for attempt in (1, 2):
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                response = self._http().get(url, headers=headers)
            except httpx.HTTPError as exc:
                raise RegistryError(f"cannot read {url}: {exc}") from exc
            if response.status_code == 401 and attempt == 1:
                token = self._authenticate(host, repository, response)
                continue
            if response.status_code >= 400:
                raise RegistryError(f"{url} returned HTTP {response.status_code}")
            return response
        raise RegistryError(f"{url} refused the credentials offered for {host}")

    def _authenticate(self, host: str, repository: str, challenge: httpx.Response) -> str:
        """Exchange the registry's `WWW-Authenticate` challenge for a bearer token.

        The realm comes from the challenge, never from a guess: a registry can delegate to a
        different host, and its own conventional `/service/token` endpoint may answer every
        request with an anonymous token -- which then fails on the next read and looks like a
        wrong password rather than the wrong endpoint.
        """
        header = challenge.headers.get("WWW-Authenticate", "")
        if not header.lower().startswith("bearer "):
            raise RegistryError(f"{host} asked for authentication flotilla cannot provide: {header[:80]!r}")
        fields = dict(re.findall(r'(\w+)="([^"]*)"', header))
        realm = fields.pop("realm", "")
        if not realm.startswith("https://"):
            raise RegistryError(f"{host} named no https token realm: {header[:80]!r}")
        auth = self._credentials.get(host)
        try:
            response = self._http().get(realm, params=fields, auth=auth)
        except httpx.HTTPError as exc:
            raise RegistryError(f"cannot reach the token realm of {host}: {exc}") from exc
        if response.status_code >= 400:
            # Deliberately without the response body: a token endpoint may echo the request.
            raise RegistryError(f"the token realm of {host} returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RegistryError(f"the token realm of {host} did not return JSON: {exc}") from exc
        token = payload.get("token") or payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise RegistryError(f"the token realm of {host} returned no token")
        self._tokens[(host, repository)] = token
        return token

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout_s, follow_redirects=True)
        return self._client


def _store(path: Path, config: Mapping[str, Any]) -> None:
    """Write the config where a later run finds it, without a reader ever seeing a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        temporary.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise RegistryError(f"cannot cache the image config at {path}: {exc}") from exc


def _parse(pinned: str) -> tuple[str, str]:
    match = PINNED.fullmatch(pinned.strip())
    if match is None:
        raise RegistryError(
            f"image reference must be repository@sha256:<digest>, not {pinned!r}: "
            "a tag can move between runs, so it cannot name the image a trial ran"
        )
    return match.group("name"), match.group("digest")


def _split(name: str) -> tuple[str, str]:
    """`registry/repository` -> host, repository path. An explicit registry host is required."""
    host, _, repository = name.partition("/")
    if not repository or ("." not in host and ":" not in host and host != "localhost"):
        raise RegistryError(f"image reference needs an explicit registry host: {name!r}")
    return host, repository


def _amd64(index: Mapping[str, Any], name: str) -> str:
    """The `linux/amd64` manifest digest from a multi-platform index."""
    for entry in index.get("manifests") or ():
        if not isinstance(entry, Mapping):
            continue
        platform = entry.get("platform")
        platform = platform if isinstance(platform, Mapping) else {}
        if platform.get("os") == "linux" and platform.get("architecture") == "amd64":
            digest = entry.get("digest")
            if isinstance(digest, str):
                return digest
    raise RegistryError(f"{name} publishes no linux/amd64 manifest")
