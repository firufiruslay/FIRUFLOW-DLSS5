"""Stage the NVIDIA Video Effects runtime from its official wheel.

The upstream project takes these five DLLs from the unmodified `nvidia-vfx`
0.2.0.0 Windows wheel and records the wheel's identity in THIRD_PARTY_NOTICES.md;
this fetches the same wheel from NVIDIA's own index and checks that identity
before anything is extracted. A wheel is a ZIP, so no Python dependency is
installed and nothing but these five files leaves it.

The result is the `runtime/nvidia-vfg/` tree plus the manifest that
package-vendor-release.py audits: one row per file, with size and digest.

This proprietary runtime is not MIT. Its licence lives under licenses/nvidia-vfg
in the published packages, and redistributing it is governed by NVIDIA's terms.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import urllib.request
import zipfile

WHEEL = "nvidia_vfx-0.2.0.0-cp311-cp311-win_amd64.whl"
URL = "https://pypi.nvidia.com/nvidia-vfx/" + WHEEL
SIZE = 435809365
SHA256 = "5aaf6a42bc6b6dbbf52fcb714194c994a6893cbbf7ada38bc2165a1f83e4a6fc"

# The five native dependencies the VFG path uses. The wheel's nine unrelated NPP
# libraries, its Python extension and its CLI are deliberately left behind.
KEEP = (
    "cudart64_12.dll",
    "NVCVImage.dll",
    "nvngxruntime.dll",
    "NVVideoEffects.dll",
    "nvVFXVideoFrameGeneration.dll",
)


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def download(destination):
    if destination.exists() and destination.stat().st_size == SIZE and digest(destination) == SHA256:
        print(f"wheel already staged: {destination}")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {URL}")
    with urllib.request.urlopen(URL, timeout=600) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle, 1 << 20)
    return destination


def verify(wheel):
    size = wheel.stat().st_size
    if size != SIZE:
        raise SystemExit(f"Unexpected wheel size {size}, expected {SIZE}")
    found = digest(wheel)
    if found != SHA256:
        raise SystemExit(f"Unexpected wheel digest {found}, expected {SHA256}")
    print(f"wheel identity verified: {WHEEL} {size} bytes sha256={found}")


def extract(wheel, output):
    """Copy the five runtime DLLs out of the wheel, wherever they sit in it."""
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    with zipfile.ZipFile(wheel) as archive:
        members = {Path(name).name: name for name in archive.namelist() if not name.endswith("/")}
        missing = [name for name in KEEP if name not in members]
        if missing:
            raise SystemExit("The wheel does not carry " + ", ".join(missing))
        for name in KEEP:
            target = output / name
            with archive.open(members[name]) as source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle, 1 << 20)
            rows.append({
                "name": name,
                "path": f"runtime/nvidia-vfg/{name}",
                "size": target.stat().st_size,
                "sha256": digest(target),
            })
            print(f"  {name}  {rows[-1]['size']} bytes")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="Directory to write the five DLLs and the manifest into")
    parser.add_argument("--cache", type=Path, help="Where to keep the downloaded wheel")
    args = parser.parse_args()

    wheel = download(args.cache or (args.output.parent / WHEEL))
    verify(wheel)
    rows = extract(wheel, args.output)
    manifest = {
        "runtime": "nvidia-vfg",
        "source": "official nvidia-vfx 0.2.0.0 wheel / Video Effects SDK 1.3.0",
        "sourceUrl": URL,
        "sourceSha256": SHA256,
        "sourceSize": SIZE,
        "files": rows,
    }
    (args.output / "vfg-runtime-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(f"staged {len(rows)} files in {args.output}")


if __name__ == "__main__":
    main()
