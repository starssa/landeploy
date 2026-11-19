#!/usr/bin/env python3
"""Put the iPXE network bootloaders and wimboot into ./bootfiles.

Looks for copies installed by your distro first (Arch: `pacman -S ipxe`,
Debian/Ubuntu: `apt install ipxe`, Fedora: `dnf install ipxe-bootimgs-x86`),
then downloads the official builds.
"""
import shutil
import sys
import urllib.request
from pathlib import Path

DEST = Path(__file__).resolve().parent / "bootfiles"

WANTED = {
    "undionly.kpxe": {   # legacy BIOS PXE
        "local": ["/usr/lib/ipxe/undionly.kpxe", "/usr/share/ipxe/undionly.kpxe", "/usr/share/ipxe/bios/undionly.kpxe"],
        "urls": ["https://boot.ipxe.org/undionly.kpxe", "https://boot.ipxe.org/x86_64-pcbios/undionly.kpxe"],
    },
    "snponly.efi": {     # UEFI x64, uses the firmware's own NIC driver (most compatible)
        "local": ["/usr/lib/ipxe/snponly.efi", "/usr/share/ipxe/ipxe-snponly-x86_64.efi",
                  "/usr/share/ipxe/efi/snponly.efi", "/usr/share/ipxe/x86_64/snponly.efi"],
        "urls": ["https://boot.ipxe.org/x86_64-efi/snponly.efi", "https://boot.ipxe.org/snponly.efi"],
    },
    "ipxe.efi": {        # UEFI x64 with iPXE's own NIC drivers (fallback, also used for UEFI HTTP boot)
        "local": ["/usr/lib/ipxe/ipxe.efi", "/usr/share/ipxe/ipxe-x86_64.efi", "/usr/share/ipxe/efi/ipxe.efi"],
        "urls": ["https://boot.ipxe.org/ipxe.efi", "https://boot.ipxe.org/x86_64-efi/ipxe.efi"],
    },
    "snponly-arm64.efi": {
        "local": ["/usr/share/ipxe/arm64-efi/snponly.efi"],
        "urls": ["https://boot.ipxe.org/arm64-efi/snponly.efi"],
        "optional": True,
    },
    "wimboot": {         # boots Windows PE (boot.wim) over HTTP
        "local": ["/usr/share/wimboot/wimboot", "/usr/lib/wimboot/wimboot"],
        "urls": ["https://github.com/ipxe/wimboot/releases/latest/download/wimboot"],
    },
}


def fetch(dest=DEST, log=print):
    """Copy/download every boot file into dest. Returns the list of required files still missing."""
    dest = Path(dest)
    dest.mkdir(exist_ok=True)
    missing = []
    for name, src in WANTED.items():
        out = dest / name
        if out.is_file() and out.stat().st_size > 0:
            log(f"ok       {name}")
            continue
        got = False
        for p in src["local"]:
            if Path(p).is_file():
                shutil.copy(p, out)
                log(f"copied   {name}  <- {p}")
                got = True
                break
        for url in [] if got else src["urls"]:
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    data = r.read()
                if len(data) < 10_000 or data[:1] == b"<":
                    raise ValueError("not a binary")
                out.write_bytes(data)
                log(f"download {name}  <- {url}")
                got = True
                break
            except Exception as e:
                log(f"  {url}: {e}")
        if not got and not src.get("optional"):
            missing.append(name)
    return _either_efi(missing, dest)


def _either_efi(missing, dest):
    """Only one UEFI binary is needed: snponly.efi or ipxe.efi."""
    has = [n for n in ("snponly.efi", "ipxe.efi") if (Path(dest) / n).is_file()]
    return [m for m in missing if not (has and m in ("snponly.efi", "ipxe.efi"))]


def missing_files(dest=DEST):
    return _either_efi([n for n, s in WANTED.items() if not s.get("optional") and not (Path(dest) / n).is_file()], dest)


def main():
    missing = fetch()
    if missing:
        print("\nStill missing:", ", ".join(missing))
        print("Download them by hand from https://ipxe.org/download (or build: make bin/undionly.kpxe "
              "bin-x86_64-efi/snponly.efi) and put them in", DEST)
        sys.exit(1)
    print("\nAll boot files ready in", DEST)


if __name__ == "__main__":
    main()
