"""Fail the build when a packaged module imports a DLL the package does not carry.

Windows resolves an executable's static imports from its own directory and the
system directories, never from sibling subfolders, so a package that looks
complete can still die with STATUS_DLL_NOT_FOUND (0xC0000135) on a clean machine
that happens to lack what the build runner had installed. This walks every PE
file in an assembled package, reads its import and delay-import tables, and
reports any dependency that is neither beside it nor part of Windows itself.

Reads the headers directly so it runs on any platform with no dependencies: CI
checks the package it just built, and a developer can check an extracted zip.
"""
import argparse
import re
import struct
import sys
from pathlib import Path

# Shipped with Windows 10 21H2 and later, so a package is not expected to carry
# them. Matched case-insensitively against the whole name.
SYSTEM = re.compile(r"""^(
    api-ms-win-.*|ext-ms-win-.*|ntdll|kernel32|kernelbase|user32|gdi32|gdiplus|advapi32|
    ole32|oleaut32|combase|shell32|shcore|shlwapi|comdlg32|comctl32|propsys|
    ws2_32|wsock32|iphlpapi|dnsapi|winhttp|wininet|netapi32|secur32|sspicli|
    crypt32|bcrypt|ncrypt|wintrust|cryptbase|cabinet|
    d3d9|d3d11|d3d12|dxgi|dxguid|d3dcompiler_4[0-9]|dxva2|dcomp|dwrite|dwmapi|uxtheme|
    opengl32|glu32|mf|mfplat|mfreadwrite|mfuuid|evr|dxcore|
    winmm|avrt|mmdevapi|audioses|ksuser|
    version|psapi|dbghelp|imagehlp|setupapi|cfgmgr32|imm32|userenv|powrprof|rpcrt4|
    msvcrt|ucrtbase|hid|bthprops|wtsapi32|winspool\.drv|oleacc|msimg32|
    cryptnet|urlmon|wldap32|authz|mpr|msi|pdh|wevtapi|rstrtmgr|uiautomationcore|
    nvapi64|nvcuda|amdhip64_[0-9]+|amd_fidelityfx_loader_dx12
)$""", re.IGNORECASE | re.VERBOSE)

# Redistributables and third-party runtimes that a portable package must always
# carry itself. A copy in the runner's System32 means the runner has a developer
# toolchain installed, not that a user's machine will have one.
MUST_SHIP = re.compile(r"^(vcruntime|msvcp|concrt|vccorlib|vcomp|qt6|postproc|"
                       r"av(codec|device|filter|format|util)|sw(resample|scale)|"
                       r"dovi|dav1d|zlib|libass|harfbuzz|freetype|fribidi|brotli|libpng|"
                       r"bz2|openh264|libcrypto|libssl|opus|vpx|x26[45]|libwebp)",
                       re.IGNORECASE)


def directories(data, start, count):
    """The (rva, size) pairs of the optional header's data directory."""
    return [struct.unpack_from("<II", data, start + 8 * i) for i in range(count)]


def sections(data, start, count):
    out = []
    for i in range(count):
        name, virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from(
            "<8sIIII", data, start + 40 * i)
        out.append((virtual_address, max(virtual_size, raw_size), raw_pointer))
    return out


def offset_of(rva, table):
    for virtual_address, size, raw_pointer in table:
        if virtual_address <= rva < virtual_address + size:
            return raw_pointer + (rva - virtual_address)
    return None


def read_name(data, offset):
    if offset is None or offset >= len(data):
        return None
    end = data.find(b"\0", offset)
    return data[offset:end].decode("ascii", "replace") if end > offset else None


def imports(path):
    """Every DLL name in the file's import and delay-import tables."""
    data = path.read_bytes()
    if data[:2] != b"MZ":
        return []
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return []
    section_count, = struct.unpack_from("<H", data, pe + 6)
    optional_size, = struct.unpack_from("<H", data, pe + 20)
    optional = pe + 24
    magic, = struct.unpack_from("<H", data, optional)
    if magic not in (0x10B, 0x20B):
        return []
    directory_start = optional + (112 if magic == 0x20B else 96)
    directory_count, = struct.unpack_from("<I", data, directory_start - 4)
    table = sections(data, optional + optional_size, section_count)
    found = []
    # 1 is the import directory, 13 the delay-import directory; the name field
    # sits at +12 of a 20-byte import descriptor and +4 of a 32-byte delay one.
    for index, stride, name_field in ((1, 20, 12), (13, 32, 4)):
        if index >= directory_count:
            continue
        rva, size = directories(data, directory_start, directory_count)[index]
        if not rva or not size:
            continue
        cursor = offset_of(rva, table)
        if cursor is None:
            continue
        while cursor + stride <= len(data):
            descriptor = data[cursor:cursor + stride]
            if not any(descriptor):
                break
            name_rva, = struct.unpack_from("<I", descriptor, name_field)
            name = read_name(data, offset_of(name_rva, table))
            if name:
                found.append(name)
            cursor += stride
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True,
                        help="Assembled package directory, or an extracted zip")
    parser.add_argument("--system-dir", type=Path,
                        help="Windows system directory, to report imports only it satisfies")
    args = parser.parse_args()

    modules = sorted(p for p in args.package.rglob("*")
                     if p.is_file() and p.suffix.lower() in (".exe", ".dll"))
    if not modules:
        raise SystemExit(f"No PE files under {args.package}")

    # The loader searches the directory of the process that loads a module, so a
    # helper in a subfolder may legitimately rely on the package root.
    root = {p.name.lower() for p in args.package.iterdir() if p.is_file()}
    missing, from_system = {}, {}
    for module in modules:
        beside = root | {p.name.lower() for p in module.parent.iterdir() if p.is_file()}
        for name in sorted(set(imports(module))):
            stem = name.lower().removesuffix(".dll")
            if name.lower() in beside:
                continue
            if SYSTEM.match(stem) and not MUST_SHIP.match(stem):
                continue
            if args.system_dir and (args.system_dir / name).is_file() and not MUST_SHIP.match(stem):
                from_system.setdefault(name, []).append(module)
                continue
            missing.setdefault(name, []).append(module)

    for name, users in sorted(from_system.items()):
        print(f"note: {name} resolves only from the system directory "
              f"({len(users)} module(s), e.g. {users[0].name})")
    for name, users in sorted(missing.items()):
        print(f"MISSING {name}: imported by {', '.join(str(u.relative_to(args.package)) for u in users[:4])}"
              + (f" and {len(users) - 4} more" if len(users) > 4 else ""))
    print(f"checked {len(modules)} modules in {args.package.name}")
    if missing:
        raise SystemExit(f"{len(missing)} dependency name(s) are absent from the package")


if __name__ == "__main__":
    main()
