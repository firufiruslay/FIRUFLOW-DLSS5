"""Turn one CI product stage plus one GPU runtime into a package and a Setup EXE.

Upstream splits an audited product stage per GPU runtime with
scripts/package-vendor-release.py and then builds the installer with
scripts/package-windows-installer.py. Those two expect the author's full
dependency tree: FidelityFX, the experimental NGX runtime, the NR weights and a
release-runtime-manifest.json covering every one of them. This repository has
none of that, so this script does the same two jobs for the pieces that exist,
keeps the identity audit (every runtime file carries size and digest), and
drives upstream's own scripts/installer/Veyra.iss so the installer is theirs,
not a reimplementation, with this fork's wordmark on their wizard panel.

What it does not do: the vendor signature audit (it needs PowerShell and a
signed payload) and the portable update trust chain beyond what the build emits.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
PRODUCT = "FIRUFLOW-DLSS5"
# PortableLayout.h: the launcher is found by this name and the marker is parsed
# byte for byte, so neither follows the product's brand.
LAUNCHER = "Veyra.exe"
PLAYER = "veyra_qml_ui.exe"
WORKER = "veyra_update_worker.exe"
MARKER = "VEYRA_PORTABLE_V1\n{flavor}\n"


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compose_branding(directory):
    """Upstream's wizard panel geometry, carrying this fork's wordmark.

    The author's own compose_branding paints the Veyra NR wordmark, which is the
    one thing the installer must not show; the dusk backdrop and the sun are the
    style this fork keeps.
    """
    from PIL import Image, ImageOps
    art = ROOT / "qml/Veyra"
    panel = ImageOps.fit(Image.open(art / "backdrop-dusk-strong.png").convert("RGBA"),
                         (492, 1050), centering=(0.62, 0.52))
    sun = Image.open(art / "intro-sun.png").convert("RGBA")
    sun.thumbnail((326, 326), Image.Resampling.LANCZOS)
    sun.putalpha(sun.getchannel("A").point(lambda value: int(value * 0.57)))
    panel.alpha_composite(sun, ((panel.width - sun.width) // 2, 164))
    wordmark = Image.open(ROOT / "assets/firuflow-wordmark.png").convert("RGBA")
    wordmark.thumbnail((372, 372), Image.Resampling.LANCZOS)
    panel.alpha_composite(wordmark, ((panel.width - wordmark.width) // 2, 400))
    destination = directory / "brand-panel.png"
    panel.convert("RGB").save(destination)

    mark = Image.open(art / "logo-dusk.png").convert("RGBA")
    mark.thumbnail((156, 116), Image.Resampling.LANCZOS)
    icon = Image.new("RGBA", (174, 174), (0, 0, 0, 0))
    icon.alpha_composite(mark, ((icon.width - mark.width) // 2, (icon.height - mark.height) // 2))
    small = directory / "brand-icon.png"
    icon.save(small)
    return destination, small


def verify_imports(package):
    """Refuse to ship a package whose modules import a DLL it does not carry."""
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    subprocess.run([sys.executable, str(ROOT / "scripts/ci/check-package-imports.py"),
                    "--package", str(package), "--system-dir", str(system)], check=True)


def assemble(stage, runtime, output, flavor, version):
    """Copy the product stage, add the runtime, and record every file."""
    package = output / f"{PRODUCT}-{version}-{flavor}-win64-portable"
    if package.exists():
        raise SystemExit(f"{package} already exists")
    shutil.copytree(stage, package)
    (package / "CI-BUILD.txt").unlink(missing_ok=True)

    manifest = json.loads((runtime / "vfg-runtime-manifest.json").read_text(encoding="utf8"))
    for row in manifest["files"]:
        source = runtime / row["name"]
        if source.stat().st_size != row["size"] or digest(source) != row["sha256"]:
            raise SystemExit("Runtime identity mismatch: " + row["name"])
        target = package / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    shutil.copyfile(runtime / "vfg-runtime-manifest.json",
                    package / "runtime/nvidia-vfg/vfg-runtime-manifest.json")

    files = sorted(f for f in package.rglob("*") if f.is_file())
    rows = [{"path": f.relative_to(package).as_posix(), "size": f.stat().st_size, "sha256": digest(f)}
            for f in files]
    (package / "package-manifest.json").write_text(json.dumps({
        "product": PRODUCT, "version": version, "flavor": flavor,
        "runtime": manifest["source"], "files": rows,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(f"assembled {len(rows)} files in {package.name}")
    return package


def archive(package):
    # The name carries dots (2.2.1), so with_suffix would eat the version.
    destination = package.parent / (package.name + ".zip")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in sorted(p for p in package.rglob("*") if p.is_file()):
            zf.write(f, f"{package.name}/{f.relative_to(package).as_posix()}")
    line = f"{digest(destination)}  {destination.name}\n"
    (package.parent / "SHA256SUMS.txt").write_text(line, encoding="utf8")
    print(f"packaged {destination.name} ({destination.stat().st_size} bytes)")
    return destination


def build_setup(args, package, version, flavor):
    """Drive upstream's Veyra.iss with the install layout its Pascal code expects."""
    installer = load_module("windows_installer", "scripts/package-windows-installer.py")
    with tempfile.TemporaryDirectory(prefix="iscc-") as directory_string:
        directory = Path(directory_string)
        output = directory / "output"
        output.mkdir()
        panel, icon = compose_branding(directory)
        (directory / "portable-package.txt").write_bytes(MARKER.format(flavor=flavor).encode("ascii"))
        (directory / "veyra-installation.txt").write_bytes(f"FIRUFLOW.Win64.{flavor}".encode("ascii"))

        version_dir = rf"\versions\{version}"
        required = {"veyra_portable_launcher.exe": args.build / "veyra_portable_launcher.exe",
                    "portable-update-trust.json": args.build / "portable-update-trust.json",
                    WORKER: args.build / WORKER}
        missing = [name for name, path in required.items() if not path.is_file()]
        if missing:
            raise SystemExit("The build did not produce " + ", ".join(missing))
        entries = [
            installer.installer_file(required["veyra_portable_launcher.exe"], "", LAUNCHER),
            installer.installer_file(required["portable-update-trust.json"], ""),
            installer.installer_file(directory / "portable-package.txt", ""),
            installer.installer_file(directory / "veyra-installation.txt", ""),
            installer.installer_file(required[WORKER], version_dir),
        ]
        # Inno Setup ships its own licence next to ISCC; older layouts do not.
        inno_license = args.iscc.parent / "license.txt"
        if inno_license.is_file():
            entries.append(installer.installer_file(inno_license, version_dir + r"\licenses",
                                                    "INNO-SETUP.txt"))
        for f in sorted(p for p in package.rglob("*") if p.is_file()):
            parent = f.relative_to(package).as_posix().rpartition("/")[0]
            folder = version_dir + ("\\" + parent.replace("/", "\\") if parent else "")
            entries.append(installer.installer_file(f, folder))
        file_list = directory / "files.iss"
        file_list.write_text("\n".join(entries) + "\n", encoding="utf-8-sig")

        defines = dict(Flavor=flavor, AppVersion=version, OutputDir=output, PayloadList=file_list,
                       AppIcon=ROOT / "assets/veyra.ico",
                       BrandBackground=ROOT / "qml/Veyra/backdrop-dusk.png",
                       BrandPanel=panel, BrandIcon=icon,
                       # Inno Setup 6 is what Chocolatey ships; see Veyra.iss.
                       LegacyWizard="1")
        command = [str(args.iscc), "/Qp"]
        command.extend(f"/D{key}={str(value).replace('/', os.sep)}" for key, value in defines.items())
        command.append(str(ROOT / "scripts/installer/Veyra.iss"))
        subprocess.run(command, check=True, env={**os.environ, "TEMP": str(directory), "TMP": str(directory)})

        produced = next(output.glob("*.exe"))
        setup = args.output / f"{PRODUCT}-{version}-{flavor}-win64-setup.exe"
        shutil.copyfile(produced, setup)
    with (args.output / "SHA256SUMS.txt").open("a", encoding="utf8") as sums:
        sums.write(f"{digest(setup)}  {setup.name}\n")
    print(f"built {setup.name} ({setup.stat().st_size} bytes)")
    return setup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True, help="The CI product stage")
    parser.add_argument("--runtime", type=Path, required=True, help="Staged GPU runtime with its manifest")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--flavor", default="NVIDIA", choices=("NVIDIA", "AMD"))
    parser.add_argument("--build", type=Path, help="Build directory, for the launcher and update worker")
    parser.add_argument("--iscc", type=Path, help="ISCC.exe; without it only the portable package is built")
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    package = assemble(args.stage, args.runtime, args.output, args.flavor, args.version)
    verify_imports(package)
    archive(package)
    if args.iscc:
        if not args.build:
            raise SystemExit("--iscc needs --build for the launcher and the update worker")
        build_setup(args, package, args.version, args.flavor)
    shutil.rmtree(package)


if __name__ == "__main__":
    main()
