"""Downloads static ffmpeg + ffprobe for the build platform into ./bin (pinned, SHA-256 checked)."""
import hashlib
import io
import os
import platform
import stat
import sys
import urllib.request
import zipfile

BUILDS = {
    # Windows x64: gyan.dev "release essentials" build (GPL), checksum published by gyan.dev
    ("Windows", "AMD64"): [("https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip",
                            "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba",
                            {"ffmpeg-9.0.2-essentials_build/bin/ffmpeg.exe": "ffmpeg.exe",
                             "ffmpeg-9.0.2-essentials_build/bin/ffprobe.exe": "ffprobe.exe"})],
    # macOS Apple Silicon: static builds by Martin Riedl (ffmpeg.martin-riedl.de)
    ("Darwin", "arm64"): [("https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2/ffmpeg.zip",
                           "c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924", {"ffmpeg": "ffmpeg"}),
                          ("https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2/ffprobe.zip",
                           "fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6", {"ffprobe": "ffprobe"})],
    # macOS Intel: same builder, amd64
    ("Darwin", "x86_64"): [("https://ffmpeg.martin-riedl.de/download/macos/amd64/1789931006_9.0.2/ffmpeg.zip",
                            "7c6b4125b191cbf773832dc51f424cf2b6bb7da43007d1e066f95909e47cacd4", {"ffmpeg": "ffmpeg"}),
                           ("https://ffmpeg.martin-riedl.de/download/macos/amd64/1789931006_9.0.2/ffprobe.zip",
                            "2322438ed2f6319a691291b247d09c69dcaa3a982460d1f269a7e1af335cfdfd", {"ffprobe": "ffprobe"})],
}


def main(dest="bin"):
    key = (platform.system(), platform.machine())
    if key not in BUILDS:
        sys.exit(f"no pinned ffmpeg build for {key}")
    os.makedirs(dest, exist_ok=True)
    for url, digest, members in BUILDS[key]:
        print("download", url, flush=True)
        req = urllib.request.Request(url, headers={"User-Agent": "vidaudcont-build"})
        data = urllib.request.urlopen(req, timeout=600).read()
        got = hashlib.sha256(data).hexdigest()
        if got != digest:
            sys.exit(f"checksum mismatch for {url}: {got}")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for member, name in members.items():
                out = os.path.join(dest, name)
                with z.open(member) as src, open(out, "wb") as f:
                    f.write(src.read())
                os.chmod(out, os.stat(out).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                print("  ->", out)
    print("ffmpeg ready")


if __name__ == "__main__":
    main(*sys.argv[1:])
