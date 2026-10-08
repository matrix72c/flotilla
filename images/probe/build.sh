#!/usr/bin/env bash
# 构建并推送 probe 用的被测单元镜像。用法：images/probe/build.sh <image> [--push]
set -euo pipefail

IMAGE="${1:?用法：build.sh <image> [--push]}"
PUSH="${2:-}"
# 可用 DOCKER="sudo docker" 指定 Docker 命令。
DOCKER="${DOCKER:-docker}"
here="$(cd "$(dirname "$0")" && pwd)"

# 构建容器经宿主的代理访问 apt 源。http_proxy 等是 docker 预定义的 build arg，不写入镜像的配置与历史。
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
