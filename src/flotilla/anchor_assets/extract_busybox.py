"""Isolated APK reader; invoke with python -I, outside the download directory.

Reuses images/anchor/build.sh's pinned control -> data -> binary verification.
Only a named regular member is read; no archive paths are extracted to disk.
"""

from __future__ import annotations

import base64
import hashlib
import io
import sys
import tarfile
import zlib
from pathlib import Path


def _member(blob: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        matches = [member for member in archive.getmembers() if member.name == name]
        if len(matches) != 1 or not matches[0].isfile():
            raise ValueError(f"APK must contain exactly one regular {name}")
        stream = archive.extractfile(matches[0])
        assert stream is not None
        return stream.read()


def extract(archive: Path, control_hash: str, binary_hash: str, output: Path) -> None:
    data = archive.read_bytes()
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("APK exceeds 32 MiB")
    streams: list[tuple[bytes, bytes]] = []
    while data:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = decoder.decompress(data, 64 * 1024 * 1024)
        if not decoder.eof:
            raise ValueError("truncated or oversized APK gzip segment")
        used = len(data) - len(decoder.unused_data)
        if used <= 0 or len(streams) >= 3:
            raise ValueError("invalid APK gzip segments")
        streams.append((data[:used], raw))
        data = decoder.unused_data
    if len(streams) not in (2, 3):
        raise ValueError("APK lacks control/data segments")
    (control_gz, control_tar), (data_gz, data_tar) = streams[-2:]
    actual_control = "Q1" + base64.b64encode(hashlib.sha1(control_gz).digest()).decode()
    if actual_control != control_hash:
        raise ValueError("control segment hash does not match APKINDEX")
    info = _member(control_tar, ".PKGINFO").decode()
    datahash = [line.removeprefix("datahash = ") for line in info.splitlines() if line.startswith("datahash = ")]
    if datahash != [hashlib.sha256(data_gz).hexdigest()]:
        raise ValueError("data segment hash does not match .PKGINFO")
    binary = _member(data_tar, "bin/busybox.static")
    if hashlib.sha256(binary).hexdigest() != binary_hash:
        raise ValueError("BusyBox binary SHA256 mismatch")
    with output.open("xb") as stream:
        stream.write(binary)
    output.chmod(0o755)


if __name__ == "__main__":
    apk, want_control, want_binary, destination = sys.argv[1:]
    extract(Path(apk), want_control, want_binary, Path(destination))
