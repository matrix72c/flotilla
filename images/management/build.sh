#!/usr/bin/env bash
# Build only. Registry publication and deployment promotion are separate actions.
set -euo pipefail
image="${1:?usage: build.sh IMAGE PYTHON_DIGEST DOCKER_ENGINE_DIGEST ANCHOR_DIGEST}"
python_image="${2:?missing pinned Python 3.12 image}"
docker_image="${3:?missing pinned Docker Engine DinD image}"
anchor_image="${4:?missing pinned anchor image}"
for ref in "$python_image" "$docker_image" "$anchor_image"; do
    [[ "$ref" =~ @sha256:[0-9a-f]{64}$ ]] || { printf 'Base images must use repo@sha256 references\n' >&2; exit 2; }
done
here="$(cd -- "$(dirname -- "$0")" && pwd)"
root="$(cd -- "$here/../.." && pwd)"
sha="$(git -C "$root" rev-parse HEAD)"
version="0+g${sha}"
if [[ -n "$(git -C "$root" status --porcelain)" ]]; then version+=".dirty"; fi
# A local candidate can include working changes; its version explicitly says dirty.
version="${FLOTILLA_VERSION:-$version}"
context="$(mktemp -d)"
download="$(mktemp -d)"
trap 'rm -rf -- "$context" "$download"' EXIT
if [[ -n "${COMPOSE_BINARY:-}" ]]; then
    cp -- "$COMPOSE_BINARY" "$download/docker-compose"
else
    curl -fL --retry 3 -o "$download/docker-compose" \
        https://github.com/docker/compose/releases/download/v2.39.4/docker-compose-linux-x86_64
fi
printf '%s  %s\n' 7af95166a730b87e172d4fc9aefea8725d3c6c7327d59149267b452114ddb7d4 \
    "$download/docker-compose" | sha256sum -c -
cp "$download/docker-compose" "$context/docker-compose"
cp "$here/Dockerfile" "$root/pyproject.toml" "$root/uv.lock" "$root/README.md" "$root/LICENSE" "$context/"
# Exclude caches before reading: development containers may own their bytecode.
# Never copy local environment files into the compiler image.
tar -C "$root" --exclude='__pycache__' --exclude='*.pyc' --exclude='.env' --exclude='.env.*' -cf - src \
    | tar -C "$context" -xf -
read -r -a docker <<< "${DOCKER:-docker}"
"${docker[@]}" build --pull=false \
    --build-arg "PYTHON_IMAGE=$python_image" \
    --build-arg "DOCKER_ENGINE_IMAGE=$docker_image" \
    --build-arg "ANCHOR_IMAGE=$anchor_image" \
    --build-arg "FLOTILLA_VERSION=$version" \
    --label "org.opencontainers.image.revision=$sha" \
    --label "org.opencontainers.image.version=$version" \
    -t "$image" "$context"
# No workspace, credentials, network or Docker daemon in the self-containment check.
"${docker[@]}" run --rm --runtime=runc --network none --entrypoint /bin/sh "$image" -ec \
    'python -I -c "import flotilla.integrations.workspace, harbor"; docker compose version --short; docker buildx version; dockerd --version; containerd --version; test -x /.flotilla/bin/busybox'
# A real Kata deployment must separately verify automatic daemon startup and docker info.
# No daemon is started and no host Docker socket is mounted by this build check.
