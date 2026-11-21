"""OS library: extract ISOs, detect what they are, and write the iPXE script
that boots each one over HTTP."""
import logging
import os
import re
import sys
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


# ---------------------------------------------------------------- mounting (no copy)
def mount_iso(iso, mnt):
    """Mount an ISO read-only at folder mnt instead of extracting it. Needs admin/root.
    Windows: virtual drive + a junction at mnt.  macOS: hdiutil.  Linux: loop mount
    (UDF first: Windows ISOs keep their files in UDF, the ISO9660 part only has a README)."""
    iso, mnt = Path(iso).resolve(), Path(mnt)
    unmount_iso(iso, mnt)
    mnt.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        ps = (f"$i = Mount-DiskImage -ImagePath '{iso}' -StorageType ISO -Access ReadOnly -PassThru; "
              "($i | Get-Volume).DriveLetter")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True)
        letter = r.stdout.strip()[-1:]
        if r.returncode or not letter.isalpha():
            raise RuntimeError("Mount-DiskImage failed: " + (r.stderr.strip() or r.stdout.strip())[-300:])
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(mnt), f"{letter}:\\"], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("could not link the virtual drive: " + r.stdout.strip() + r.stderr.strip())
        return f"virtual drive {letter}:"
    mnt.mkdir(exist_ok=True)
    if sys.platform == "darwin":
        r = subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mnt), str(iso)],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("hdiutil: " + r.stderr.strip()[-300:])
        return "hdiutil"
    errors = []
    for fs in ("udf", "iso9660"):
        r = subprocess.run(["mount", "-t", fs, "-o", "loop,ro", str(iso), str(mnt)], capture_output=True, text=True)
        if r.returncode == 0:
            return f"loop mount ({fs})"
        errors.append(f"{fs}: {r.stderr.strip()}")
    raise RuntimeError("; ".join(errors)[-400:])


def unmount_iso(iso, mnt):
    mnt = Path(mnt)
    if os.name == "nt":
        if os.path.lexists(mnt):
            try:
                os.rmdir(mnt)                                  # removes the junction only, never the files
            except OSError:
                pass
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        f"Dismount-DiskImage -ImagePath '{Path(iso).resolve()}' -ErrorAction SilentlyContinue | Out-Null"],
                       capture_output=True)
        return
    if is_mounted(mnt):
        cmd = ["hdiutil", "detach", "-force", str(mnt)] if sys.platform == "darwin" else ["umount", str(mnt)]
        if subprocess.run(cmd, capture_output=True).returncode and sys.platform != "darwin":
            subprocess.run(["umount", "-l", str(mnt)], capture_output=True)   # busy (a PC is downloading): lazy
    try:
        mnt.rmdir()
    except OSError:
        pass


def is_mounted(mnt):
    mnt = Path(mnt)
    if os.name == "nt":
        try:
            return os.path.lexists(mnt) and any(mnt.iterdir())
        except OSError:
            return False
    return os.path.ismount(mnt)


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
def boot_script(entry, base, mac, drivers=()):
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
                 f"initrd {base}/winpe/{eid}/install.cmd?mac={mac} install.cmd\n"]
        # extra network/storage drivers for Windows PE: wimboot copies them into X:\Windows\System32
        lines += [f"initrd {base}/winpe-drivers/{d} {d}\n" for d in drivers]
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


def winpe_files(entry, settings, server_ip, answer_file=None, drivers=()):
    share = settings.get("smb_share", "landeploy")
    user, pw = settings.get("smb_user") or "guest", settings.get("smb_pass") or ""
    unattend = f" /unattend:Z:\\unattend\\{answer_file}" if answer_file else ""
    folder = "mnt" if entry.get("mode") == "mount" else "library"     # mounted ISO or extracted copy
    winpeshl = '[LaunchApps]\r\n"install.cmd"\r\n'
    load = "".join(f"drvload X:\\Windows\\System32\\{d} >nul\r\n" for d in drivers if d.lower().endswith(".inf"))
    s = server_ip
    lines = [
        "@echo off",
        "title LANDeploy - Windows network install",
        "echo Starting the network...",
        "wpeinit",
        load.rstrip("\r\n") if load else "rem (no extra drivers)",
        "set n=0",
        ":retry",
        "set /a n+=1",
        f'ping -n 2 -w 1500 {s} | find "TTL=" >nul && goto connected',
        f"if %n%==1 echo Waiting for network - connecting to the LANDeploy server {s}...",
        "ping -n 3 127.0.0.1 >nul",                     # ~2 s between tries (fails instantly with no network)
        "if not %n%==15 goto retry",
        "echo.",
        f"echo === No connection to the LANDeploy server {s} after about a minute ===",
        "echo This PC's network addresses:",
        'ipconfig | find "IPv4"',
        "echo.",
        "echo  - No IPv4 address above: Windows PE has no driver for this network card.",
        "echo    Add the card's driver (.inf .sys .cat) on the iPXE network boot page, then reboot this PC.",
        "echo  - An address is shown: the cable/network or the server's firewall blocks the connection.",
        "echo.",
        'echo A command prompt opens for checks (ipconfig, ping). Type "exit" in it to keep trying.',
        "start /wait cmd",
        "goto retry",
        ":connected",
        f"echo Connected to {s}.",
        ":share",
        f'net use Z: \\\\{s}\\{share} /user:{user} "{pw}" >nul 2>&1 && goto setup',
        f"echo The shared folder \\\\{s}\\{share} is not reachable: set it up on the iPXE network boot page.",
        "pause",
        "goto share",
        ":setup",
        "echo Starting Windows Setup...",
        f"Z:\\{folder}\\{entry['id']}\\setup.exe{unattend}",
        "wpeutil reboot",
    ]
    return winpeshl, "\r\n".join(lines) + "\r\n"


def clean(text):
    """Make text safe for an iPXE menu/echo line."""
    return re.sub(r"[^\w .,()+\-/:]", "", str(text))[:60]
