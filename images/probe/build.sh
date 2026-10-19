#!/usr/bin/env bash
# Build and optionally push the image probed by `flotilla probe`. Usage: images/probe/build.sh <image> [--push]
set -euo pipefail

IMAGE="${1:?usage: build.sh <image> [--push]}"
PUSH="${2:-}"
# Set DOCKER="sudo docker" to choose the Docker command.
DOCKER="${DOCKER:-docker}"
here="$(cd "$(dirname "$0")" && pwd)"

# The build container reaches apt through the host proxy. http_proxy and friends are predefined Docker
# build args and are not recorded in the image configuration or history.
proxy_args=()
for v in http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY; do
  if [ -n "${!v:-}" ]; then proxy_args+=(--build-arg "$v=${!v}"); fi
done
$DOCKER build --network host "${proxy_args[@]}" --build-arg "BASE_IMAGE=${BASE_IMAGE:-ubuntu:24.04}" -t "$IMAGE" "$here"
$DOCKER run --rm "$IMAGE" sh -c 'set -e; for t in id pwd stat timeout nc ip cat ls touch grep; do command -v "$t" >/dev/null; done; echo tools-ok'
$DOCKER image inspect "$IMAGE" --format 'size={{.Size}}'
if [ "$PUSH" = "--push" ]; then
  $DOCKER push "$IMAGE"
  $DOCKER image inspect "$IMAGE" --format '{{index .RepoDigests 0}}'
fi
