"""OS library: extract ISOs, detect what they are, and write the iPXE script
that boots each one over HTTP."""
import logging
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("library")


# ---------------------------------------------------------------- file helpers
def find_ci(root, rel):
    """Case-insensitive path lookup inside root (Windows ISOs mix BOOT/boot/Boot)."""
    cur = Path(root)
    for part in [p for p in str(rel).replace("\\", "/").split("/") if p]:
        if part in (".", "..") or not cur.is_dir():
            return None
        exact = cur / part
        if exact.exists():
            cur = exact
            continue
        match = [c for c in cur.iterdir() if c.name.lower() == part.lower()]
        if not match:
            return None
        cur = match[0]
    return cur


def _rel(root, *cands):
    for c in cands:
        p = find_ci(root, c)
        if p and p.is_file():
            return p.relative_to(root).as_posix()
    return None


def _glob(root, reldir, pattern):
    d = find_ci(root, reldir)
    if d and d.is_dir():
        hits = sorted(x for x in d.iterdir() if x.is_file() and re.fullmatch(pattern, x.name, re.I))
        if hits:
            return hits[0].relative_to(root).as_posix()
    return None


# ---------------------------------------------------------------- extraction
def _safe_join(dest, name):
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return Path(dest).joinpath(*parts) if parts else None


def extract_iso(iso, dest):
    """Extract an ISO with 7-Zip if installed, else bsdtar, else pure-Python pycdlib."""
    iso, dest = Path(iso), Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for exe, args in (("7z", ["x", "-y", f"-o{dest}", str(iso)]),
                      ("7zz", ["x", "-y", f"-o{dest}", str(iso)]),
                      ("bsdtar", ["-xf", str(iso), "-C", str(dest)])):
        if shutil.which(exe):
            r = subprocess.run([exe] + args, capture_output=True, text=True)
            if r.returncode == 0 and any(dest.iterdir()):
                return exe
            log.warning("%s failed on %s: %s", exe, iso.name, (r.stderr or r.stdout)[-300:])
    return _extract_pycdlib(iso, dest)


def _extract_pycdlib(iso, dest):
    try:
        import pycdlib
    except ImportError:
        raise RuntimeError("No ISO extractor found: install 7-Zip (7z) or `pip install pycdlib`.")
    cd = pycdlib.PyCdlib()
    cd.open(str(iso))
    try:
        if cd.has_udf():
            kw = "udf_path"
        elif cd.has_rock_ridge():
            kw = "rr_path"
        elif cd.has_joliet():
            kw = "joliet_path"
        else:
            kw = "iso_path"
        for dirname, _dirs, files in cd.walk(**{kw: "/"}):
            for f in files:
                name = f.split(";")[0] if kw == "iso_path" else f
                out = _safe_join(dest, f"{dirname}/{name}")
                if not out:
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                src = (dirname.rstrip("/") + "/" + f)
                cd.get_file_from_iso(str(out), **{kw: src})
    finally:
        cd.close()
    return "pycdlib"


# ---------------------------------------------------------------- detection
def detect(root):
    """Return boot info for an extracted ISO tree."""
    root = Path(root)
    wim = _rel(root, "sources/boot.wim")
    if wim:
        return {"type": "windows", "boot_wim": wim,
                "bcd": _rel(root, "boot/bcd"), "sdi": _rel(root, "boot/boot.sdi"),
                "bootmgr": _rel(root, "bootmgr")}
    k = _rel(root, "arch/boot/x86_64/vmlinuz-linux")
    if k:
        return {"type": "arch", "kernel": k, "initrd": _rel(root, "arch/boot/x86_64/initramfs-linux.img")}
    k = _rel(root, "casper/vmlinuz") or _glob(root, "casper", r"vmlinuz.*")
    if k:
        return {"type": "ubuntu", "kernel": k,
                "initrd": _rel(root, "casper/initrd", "casper/initrd.lz", "casper/initrd.gz")
                or _glob(root, "casper", r"initrd.*")}
    k = _rel(root, "images/pxeboot/vmlinuz")
    if k:
        return {"type": "fedora", "kernel": k, "initrd": _rel(root, "images/pxeboot/initrd.img"),
                "live": _rel(root, "LiveOS/squashfs.img")}
    k = _glob(root, "live", r"vmlinuz.*")
    sq = _rel(root, "live/filesystem.squashfs")
    if k and sq:
        return {"type": "debian-live", "kernel": k, "initrd": _glob(root, "live", r"initrd.*"), "squashfs": sq}
    return {"type": "generic"}


TYPE_LABELS = {"windows": "Windows Setup (WinPE + SMB)", "arch": "Arch Linux", "ubuntu": "Ubuntu / casper",
               "fedora": "Fedora / RHEL family", "debian-live": "Debian live", "generic": "Raw ISO (sanboot)"}


# ---------------------------------------------------------------- iPXE scripts
def boot_script(entry, base, mac):
    """iPXE script that boots one library entry. base = http://ip:port"""
    eid, info = entry["id"], entry.get("boot", {})
    files = f"{base}/files/{eid}"
    iso = f"{base}/isos/{entry['iso']}"
    extra = (entry.get("extra_args") or "").strip()
    t = info.get("type", "generic")
    head = f"#!ipxe\necho LANDeploy: starting {clean(entry['name'])}\n"

    def linux(args):
        return (head + f"kernel {files}/{info['kernel']} initrd=initrd.magic {args} {extra}\n"
                f"initrd --name initrd.magic {files}/{info['initrd']}\nboot\n")

    if t == "windows":
        lines = [head, f"kernel {base}/bootfiles/wimboot gui\n",
                 f"initrd {base}/winpe/{eid}/winpeshl.ini winpeshl.ini\n",
                 f"initrd {base}/winpe/{eid}/install.cmd install.cmd\n"]
        if info.get("bootmgr"):
            lines.append(f"iseq ${{platform}} pcbios && initrd {files}/{info['bootmgr']} bootmgr ||\n")
        lines += [f"initrd {files}/{info['bcd']} BCD\n", f"initrd {files}/{info['sdi']} boot.sdi\n",
                  f"initrd {files}/{info['boot_wim']} boot.wim\n", "boot\n"]
        return "".join(lines)
    if t == "arch":
        return linux(f"archisobasedir=arch archiso_http_srv={files}/ ip=dhcp net.ifnames=0 BOOTIF=01-${{netX/mac:hexhyp}}")
    if t == "ubuntu":
        auto = f" autoinstall ds=nocloud-net;s={base}/autoinstall/{eid}/" if entry.get("autoinstall") else ""
        return linux(f"boot=casper ip=dhcp url={iso} cloud-config-url=/dev/null{auto} ---")
    if t == "fedora":
        if info.get("live"):
            return linux(f"root=live:{files}/{info['live']} rd.live.image ip=dhcp")
        ks = f" inst.ks={base}/kickstart/{eid}.cfg" if entry.get("kickstart") else ""
        return linux(f"inst.repo={files}/ ip=dhcp{ks}")
    if t == "debian-live":
        return linux(f"boot=live fetch={files}/{info['squashfs']} ip=dhcp")
    # generic: let iPXE map the whole ISO as a SAN disk over HTTP (works best on BIOS / hybrid ISOs)
    return head + f"sanboot --no-describe {iso}\n"


def winpe_files(entry, settings, server_ip):
    share = settings.get("smb_share", "landeploy")
    user, pw = settings.get("smb_user") or "guest", settings.get("smb_pass") or ""
    unattend = f" /unattend:Z:\\library\\{entry['id']}\\landeploy-unattend.xml" if entry.get("unattend") else ""
    winpeshl = '[LaunchApps]\r\n"install.cmd"\r\n'
    cmd = ("@echo off\r\n"
           "title LANDeploy - Windows network install\r\n"
           "wpeinit\r\n"
           "echo Waiting for network...\r\n"
           f":retry\r\nping -n 3 {server_ip} >nul || goto retry\r\n"
           f"net use Z: \\\\{server_ip}\\{share} /user:{user} \"{pw}\" || (echo SMB share not reachable & pause & goto retry)\r\n"
           f"Z:\\library\\{entry['id']}\\setup.exe{unattend}\r\n"
           "wpeutil reboot\r\n")
    return winpeshl, cmd


def clean(text):
    """Make text safe for an iPXE menu/echo line."""
    return re.sub(r"[^\w .,()+\-/:]", "", str(text))[:60]
