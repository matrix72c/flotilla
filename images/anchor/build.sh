#!/usr/bin/env bash
# Build and optionally push the anchor image. Usage: images/anchor/build.sh <image> [--push]
set -euo pipefail

IMAGE="${1:?usage: build.sh <image> [--push]}"
PUSH="${2:-}"
# Any Alpine mirror works: the package is verified against the pinned hashes below. Alpine stable
# branches keep only the latest package revision, so a newer busybox revision requires updating the pins.
ALPINE="${ALPINE_MIRROR:-https://dl-cdn.alpinelinux.org/alpine}/v3.20/main/x86_64"
APK="busybox-static-1.36.1-r31.apk"
APK_CONTROL_Q1="Q1hZXWQJ7C5Eu/FFlBqrTGUqcycsM="  # C: field of this package in APKINDEX (SHA-1 of the control segment)
BUSYBOX_SHA256="6d4ae568988ee24beb9dac4afdac4df67f90bbaed6ca47628da35ff5eb632a4c"
# Set DOCKER="sudo docker" to choose the Docker command.
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
while pos < len(data):  # .apk = concatenated signature, control, and data gzip streams
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    raw = d.decompress(data[pos:])
    used = len(data) - pos - len(d.unused_data)
    streams.append((data[pos:pos + used], raw))
    pos += used
(control_gz, control_tar), (data_gz, data_tar) = streams[-2], streams[-1]
if "Q1" + base64.b64encode(hashlib.sha1(control_gz).digest()).decode() != want_control:
    sys.exit("control segment hash does not match APKINDEX")
info = tarfile.open(fileobj=io.BytesIO(control_tar)).extractfile(".PKGINFO").read().decode()
datahash = next(l.split(" = ", 1)[1] for l in info.splitlines() if l.startswith("datahash"))
if hashlib.sha256(data_gz).hexdigest() != datahash:
    sys.exit("data segment hash does not match .PKGINFO")
member = tarfile.open(fileobj=io.BytesIO(data_tar)).extractfile("bin/busybox.static")
open(out, "wb").write(member.read())
PY
echo "$BUSYBOX_SHA256  $ctx/busybox" | sha256sum -c --quiet
rm "$ctx/$APK"

# rootfs: /.flotilla/bin/busybox; applet links in /bin (the entry wrapper needs /bin/sh, bootstrap.sh uses
# mkdir, touch, dirname, tr, and id via PATH; the resident entrypoint is /bin/sleep); /tmp 1777; and the
# user database required by execd identity routes.
root="$ctx/rootfs"
install -D -m 0755 "$ctx/busybox" "$root/.flotilla/bin/busybox"
rm "$ctx/busybox"
mkdir -p "$root/bin" "$root/tmp" "$root/etc"
chmod 1777 "$root/tmp"
# current execd looks up supplementary groups for the uid; without passwd/group it rejects uploads as an
# identity resolution failure (including uid 0).
printf 'root:x:0:0:root:/:/bin/sh\n' > "$root/etc/passwd"
printf 'root:x:0:\n' > "$root/etc/group"
chmod 0644 "$root/etc/passwd" "$root/etc/group"
for applet in sh sleep mkdir rm ls stat chown chmod tar cp mv touch dirname tr id cat env true; do
  ln -s /.flotilla/bin/busybox "$root/bin/$applet"
done
cp "$here/Dockerfile" "$ctx/"

$DOCKER build --pull=false -t "$IMAGE" "$ctx"
$DOCKER run --rm --entrypoint /bin/sh "$IMAGE" -c \
  'set -e; for a in sh env ip chown chmod mkdir rm ls stat tar cp sleep; do /.flotilla/bin/busybox --list | /.flotilla/bin/busybox grep -qx "$a"; done; echo applets-ok'
$DOCKER image inspect "$IMAGE" --format 'size={{.Size}}'
if [ "$PUSH" = "--push" ]; then
  $DOCKER push "$IMAGE"
  $DOCKER image inspect "$IMAGE" --format '{{index .RepoDigests 0}}'
fi
