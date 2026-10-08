#!/usr/bin/env bash
# 构建并推送锚点镜像。用法：images/anchor/build.sh <image> [--push]
set -euo pipefail

IMAGE="${1:?用法：build.sh <image> [--push]}"
PUSH="${2:-}"
ALPINE="https://mirrors.tuna.tsinghua.edu.cn/alpine/v3.20/main/x86_64"
APK="busybox-static-1.36.1-r31.apk"
APK_CONTROL_Q1="Q1hZXWQJ7C5Eu/FFlBqrTGUqcycsM="  # APKINDEX 中该包的 C: 字段（control 段的 sha1）
BUSYBOX_SHA256="6d4ae568988ee24beb9dac4afdac4df67f90bbaed6ca47628da35ff5eb632a4c"
# 可用 DOCKER="sudo docker" 指定 Docker 命令。
DOCKER="${DOCKER:-docker}"

here="$(cd "$(dirname "$0")" && pwd)"
ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT

curl -fsSL -o "$ctx/$APK" "$ALPINE/$APK"
python3 -I - "$ctx/$APK" "$APK_CONTROL_Q1" "$ctx/busybox" <<'PY'
import base64, hashlib, io, sys, tarfile, zlib
apk, want_control, out = sys.argv[1:]
data = open(apk, "rb").read()
streams, pos = [], 0
while pos < len(data):  # .apk = 签名、control、data 三段 gzip 首尾相接
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    raw = d.decompress(data[pos:])
    used = len(data) - pos - len(d.unused_data)
    streams.append((data[pos:pos + used], raw))
    pos += used
(control_gz, control_tar), (data_gz, data_tar) = streams[-2], streams[-1]
if "Q1" + base64.b64encode(hashlib.sha1(control_gz).digest()).decode() != want_control:
    sys.exit("control 段哈希与 APKINDEX 不符")
info = tarfile.open(fileobj=io.BytesIO(control_tar)).extractfile(".PKGINFO").read().decode()
datahash = next(l.split(" = ", 1)[1] for l in info.splitlines() if l.startswith("datahash"))
if hashlib.sha256(data_gz).hexdigest() != datahash:
    sys.exit("data 段哈希与 .PKGINFO 不符")
member = tarfile.open(fileobj=io.BytesIO(data_tar)).extractfile("bin/busybox.static")
open(out, "wb").write(member.read())
PY
echo "$BUSYBOX_SHA256  $ctx/busybox" | sha256sum -c --quiet
rm "$ctx/$APK"

# rootfs：/.flotilla/bin/busybox；/bin 下的 applet 链接（入口包装要 /bin/sh，bootstrap.sh 经 PATH 用
# mkdir、touch、dirname、tr、id；常驻入口 /bin/sleep）；/tmp 1777。
root="$ctx/rootfs"
install -D -m 0755 "$ctx/busybox" "$root/.flotilla/bin/busybox"
rm "$ctx/busybox"
mkdir -p "$root/bin" "$root/tmp"
chmod 1777 "$root/tmp"
for applet in sh sleep mkdir rm ls chown chmod tar cp mv touch dirname tr id cat env true; do
  ln -s /.flotilla/bin/busybox "$root/bin/$applet"
done
cp "$here/Dockerfile" "$ctx/"

$DOCKER build --pull=false -t "$IMAGE" "$ctx"
$DOCKER run --rm --entrypoint /bin/sh "$IMAGE" -c \
  'set -e; for a in sh env ip chown chmod mkdir rm ls tar cp sleep; do /.flotilla/bin/busybox --list | /.flotilla/bin/busybox grep -qx "$a"; done; echo applets-ok'
$DOCKER image inspect "$IMAGE" --format 'size={{.Size}}'
if [ "$PUSH" = "--push" ]; then
  $DOCKER push "$IMAGE"
  $DOCKER image inspect "$IMAGE" --format '{{index .RepoDigests 0}}'
fi
