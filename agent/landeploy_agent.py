#!/usr/bin/env python3
"""LANDeploy agent - runs on client PCs, receives software from the LANDeploy
server and ASKS THE USER before installing anything (unless the admin marked
the package as silent).

    python landeploy_agent.py --token <agent token>                 # auto-discovers the server
    python landeploy_agent.py --server http://192.168.1.10:8080 --token <token>

Run it with admin/root rights (installers need them). On Windows the simplest
way is a Scheduled Task "At log on", "Run with highest privileges", so the
consent dialog appears on the user's desktop.
"""
import argparse
import getpass
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

VERSION = "1.0"
IS_WIN = os.name == "nt"
if IS_WIN:
    STATE = Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData")) / "LANDeploy" / "agent.json"
elif os.geteuid() == 0:
    STATE = Path("/var/lib/landeploy/agent.json")
else:
    STATE = Path.home() / ".landeploy" / "agent.json"


def discover(timeout=3):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(timeout)
    try:
        s.sendto(b"LANDEPLOY?", ("255.255.255.255", 4012))
        data, _ = s.recvfrom(256)
        if data.startswith(b"LANDEPLOY "):
            return data[10:].decode().strip()
    except OSError:
        return None
    finally:
        s.close()


def macs():
    out = set()
    try:
        if IS_WIN:
            r = subprocess.run(["getmac", "/fo", "csv", "/nh"], capture_output=True, text=True)
            for line in r.stdout.splitlines():
                m = line.split(",")[0].strip('"')
                if len(m) == 17:
                    out.add(m.replace("-", ":").lower())
        else:
            for p in Path("/sys/class/net").iterdir():
                a = (p / "address").read_text().strip()
                if a and a != "00:00:00:00:00:00":
                    out.add(a)
    except Exception:
        pass
    n = uuid.getnode()
    out.add(":".join(f"{(n >> s) & 0xff:02x}" for s in range(40, -1, -8)))
    return sorted(out)


class Client:
    def __init__(self, server, token):
        self.server, self.token = server.rstrip("/"), token

    def call(self, path, body=None):
        req = urllib.request.Request(self.server + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Agent-Token": self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")

    def download(self, url, dest):
        req = urllib.request.Request(url, headers={"X-Agent-Token": self.token})
        h = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
        return h.hexdigest()


# ------------------------------------------------------------------ consent UI
def _win_ask(title, text, timeout):
    """Show Yes/No on the logged-in user's desktop. Works even when the agent runs as SYSTEM
    (scheduled task at startup), via WTSSendMessage."""
    import ctypes
    from ctypes import wintypes
    sid = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
    if sid == 0xFFFFFFFF:                                       # nobody logged on
        return False
    style = 0x4 | 0x20 | 0x40000 | 0x10000                       # YESNO | QUESTION | TOPMOST | SETFOREGROUND
    resp = wintypes.DWORD(0)
    ok = ctypes.windll.wtsapi32.WTSSendMessageW(None, sid, title, len(title) * 2, text, len(text) * 2,
                                                style, timeout, ctypes.byref(resp), True)
    return bool(ok) and resp.value == 6                          # IDYES (32000 = timeout)


def _linux_session():
    """Find the active graphical session so a root service can show a dialog to that user."""
    try:
        out = subprocess.run(["loginctl", "list-sessions", "--no-legend"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            sid = line.split()[0]
            r = subprocess.run(["loginctl", "show-session", sid, "-p", "Type", "-p", "Active", "-p", "Name",
                                "-p", "User", "-p", "Display"], capture_output=True, text=True).stdout
            p = dict(x.split("=", 1) for x in r.splitlines() if "=" in x)
            if p.get("Active") == "yes" and p.get("Type") in ("x11", "wayland"):
                uid = p["User"]
                env = {"DISPLAY": p.get("Display") or ":0", "XDG_RUNTIME_DIR": f"/run/user/{uid}",
                       "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus"}
                if p["Type"] == "wayland":
                    env["WAYLAND_DISPLAY"] = "wayland-0"
                return p["Name"], env
    except Exception:
        pass
    return None, None


def ask_user(title, text, timeout=300):
    """Return True only if the user explicitly clicks Yes. No answer = no install."""
    if IS_WIN:
        try:
            return _win_ask(title, text, timeout)
        except Exception:
            import ctypes
            return ctypes.windll.user32.MessageBoxW(0, text, title, 0x4 | 0x20 | 0x40000) == 6
    zen = ["--question", "--title", title, "--text", text, f"--timeout={timeout}", "--width=420"]
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        for exe, args in (("zenity", zen), ("kdialog", ["--title", title, "--yesno", text])):
            if shutil.which(exe):
                return subprocess.run([exe] + args).returncode == 0
    elif os.geteuid() == 0 and shutil.which("zenity"):          # root service: ask the desktop user
        user, env = _linux_session()
        if user:
            cmd = ["runuser", "-u", user, "--", "env"] + [f"{k}={v}" for k, v in env.items()] + ["zenity"] + zen
            return subprocess.run(cmd).returncode == 0
    if sys.stdin and sys.stdin.isatty():
        return input(f"{title}\n{text}\nInstall? [y/N] ").strip().lower() in ("y", "yes")
    return False


def fix_command(cmd):
    """winget isn't on PATH for the SYSTEM account: use its full path."""
    if IS_WIN and cmd.lower().startswith("winget") and not shutil.which("winget"):
        import glob
        hits = sorted(glob.glob(r"C:\Program Files\WindowsApps\Microsoft.DesktopAppInstaller_*_x64__8wekyb3d8bbwe\winget.exe"))
        if hits:
            return f'"{hits[-1]}"' + cmd[6:]
    return cmd


# ------------------------------------------------------------------ installers
ZIP_DEFAULTS = ["setup.exe", "install.cmd", "install.bat", "install.ps1", "install.sh"]


def unzip_package(path):
    """Extract a folder-type package (e.g. Office: setup.exe + Office\\ + configuration.xml).
    Returns the folder that holds the installer (descends into a single top-level folder)."""
    import zipfile
    dest = path.parent / "pkg"
    with zipfile.ZipFile(path) as z:
        for m in z.infolist():                                     # refuse ../ paths
            target = (dest / m.filename).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise ValueError(f"unsafe path in zip: {m.filename}")
        z.extractall(dest)
    items = list(dest.iterdir())
    while len(items) == 1 and items[0].is_dir():
        dest = items[0]
        items = list(dest.iterdir())
    return dest


def zip_command(folder, args):
    """args is the command line to run inside the extracted folder; guessed if empty."""
    if args.strip():
        return args
    for name in ZIP_DEFAULTS:
        if (folder / name).is_file():
            if name.endswith(".ps1"):
                return f"powershell -NoProfile -ExecutionPolicy Bypass -File {name}"
            if name.endswith(".sh"):
                return f"sh {name}"
            return name
    raise ValueError("zip package: set the command to run (e.g. setup.exe /configure configuration.xml)")


def install_cmd(path, args):
    ext = path.suffix.lower()
    a = args.split() if args else []
    if ext == ".msi":
        return ["msiexec", "/i", str(path)] + (a or ["/qn", "/norestart"])
    if ext == ".exe":
        return [str(path)] + a
    if ext == ".ps1":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)] + a
    if ext in (".bat", ".cmd"):
        return ["cmd", "/c", str(path)] + a
    if ext == ".deb":
        return ["apt-get", "install", "-y", str(path)] if shutil.which("apt-get") else ["dpkg", "-i", str(path)]
    if ext == ".rpm":
        for pm in ("dnf", "zypper", "yum"):
            if shutil.which(pm):
                return [pm, "install", "-y", str(path)]
        return ["rpm", "-i", str(path)]
    if ext == ".sh":
        return ["sh", str(path)] + a
    if ext == ".appimage":
        dest = Path("/opt/landeploy-apps") / path.name
        return ["sh", "-c", f'mkdir -p /opt/landeploy-apps && cp "{path}" "{dest}" && chmod +x "{dest}"']
    raise ValueError(f"don't know how to install {path.name}")


# ------------------------------------------------------------------ runners
def logged_on_user():
    if IS_WIN:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "(Get-CimInstance Win32_ComputerSystem).UserName"], capture_output=True, text=True)
        return r.stdout.strip() or None
    return _linux_session()[0]


def job_dir(jid):
    if IS_WIN:
        d = Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData")) / "LANDeploy" / "jobs" / jid
        d.mkdir(parents=True, exist_ok=True)
        return d
    d = Path(tempfile.mkdtemp(prefix=f"landeploy-{jid}-"))
    d.chmod(0o755)
    return d


def run_system(cmd, cwd, timeout):
    if isinstance(cmd, str):
        cmd = fix_command(cmd)
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout, cwd=cwd)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def run_user(cmd, cwd, timeout, jid):
    """Run inside the logged-in user's account, so per-user apps (Antigravity, VS Code user setup,
    Cursor, Discord...) land in THEIR profile instead of the SYSTEM profile."""
    line = cmd if isinstance(cmd, str) else subprocess.list2cmdline(cmd)
    cwd = Path(cwd)
    if not IS_WIN:
        user, env = _linux_session()
        r = subprocess.run(["runuser", "-u", user, "--", "env"] + [f"{k}={v}" for k, v in env.items()] +
                           ["sh", "-c", line], capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    user = logged_on_user()
    log, rc, bat = cwd / "out.log", cwd / "rc.txt", cwd / "run.cmd"
    rc.unlink(missing_ok=True)
    bat.write_text(f'@echo off\r\ncd /d "{cwd}"\r\n{line} > "{log}" 2>&1\r\necho %errorlevel%> "{rc}"\r\n')
    subprocess.run(["icacls", str(cwd), "/grant", "*S-1-5-32-545:(OI)(CI)M", "/T", "/Q"], capture_output=True)
    task = f"LANDeploy-job-{jid}"
    ps = (f"$a = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c \"{bat}\"'; "
          f"$p = New-ScheduledTaskPrincipal -UserId '{user}' -LogonType Interactive -RunLevel Limited; "
          f"Register-ScheduledTask -TaskName '{task}' -Action $a -Principal $p -Force | Out-Null; "
          f"Start-ScheduledTask -TaskName '{task}'")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"could not start the task as {user}: {r.stderr.strip()[-400:]}")
    try:
        deadline = time.time() + timeout
        while not rc.exists():
            if time.time() > deadline:
                raise TimeoutError("installer did not finish in time")
            time.sleep(3)
        time.sleep(1)
        code = int((rc.read_text(errors="replace").strip() or "1").split()[0])
        return code, f"(ran as {user})\n" + (log.read_text(errors="replace") if log.exists() else "")
    finally:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        f"Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false"], capture_output=True)


def run_job(cli, job):
    jid = job["job_id"]
    as_user = job.get("run_as") == "user"

    def report(status, msg=""):
        try:
            return cli.call("/api/agent/report", {"job_id": jid, "status": status, "message": msg})
        except Exception as e:
            print("report failed:", e)
            return {}

    if as_user and not logged_on_user():
        report("pending", "Waiting for someone to log on (this app installs into the user's account).")
        return
    if job.get("consent", True):
        if report("asking").get("cancelled"):
            return
        text = (f"Your IT administrator wants to install:\n\n    {job['name']}\n\n"
                f"on this computer ({socket.gethostname()}).\n\nAllow the installation now?")
        if not ask_user("LANDeploy - software installation", text):
            report("denied", "User declined (or did not answer).")
            return
    report("running", "Installing..." + (" (as the logged-in user)" if as_user else ""))
    work = job_dir(jid)
    try:
        if job.get("command"):
            cmd, cwd, limit = job["command"], work, 3600
        else:
            path = work / job["file"]
            digest = cli.download(job["url"], path)
            if job.get("sha256") and digest != job["sha256"]:
                return report("failed", "Checksum mismatch - download corrupted or tampered with.")
            if path.suffix.lower() == ".zip":                     # folder package: unzip, run command inside
                report("running", "Extracting package...")
                cwd = unzip_package(path)
                cmd, limit = zip_command(cwd, job.get("args", "")), 4 * 3600
            else:
                if not IS_WIN:
                    path.chmod(0o755)
                cmd, cwd, limit = install_cmd(path, job.get("args", "")), work, 3600
        code, out = run_user(cmd, cwd, limit, jid) if as_user else run_system(cmd, cwd, limit)
        ok = code in (0, 3010, 1641)                               # 3010/1641 = success, reboot required
        report("success" if ok else "failed", f"exit code {code}\n{out[-1500:]}")
    except Exception as e:
        report("failed", repr(e))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="LANDeploy agent")
    ap.add_argument("--server", help="http://ip:8080 (auto-discovered if omitted)")
    ap.add_argument("--token", required=True, help="agent token shown in the LANDeploy console")
    ap.add_argument("--interval", type=int, default=20, help="poll interval in seconds")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    a = ap.parse_args()

    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    server = a.server or state.get("server")
    while not server:
        server = discover()
        if not server:
            print("No LANDeploy server found on the LAN, retrying in 30 s...")
            time.sleep(30)
    cli = Client(server, a.token)
    print(f"LANDeploy agent {VERSION} -> {server}")
    while True:
        try:
            r = cli.call("/api/agent/hello", {
                "id": state.get("id"), "hostname": socket.gethostname(), "version": VERSION,
                "os": f"{platform.system()} {platform.release()}", "user": getpass.getuser(), "macs": macs()})
            if r.get("id") != state.get("id") or state.get("server") != server:
                state.update(id=r["id"], server=server)
                STATE.parent.mkdir(parents=True, exist_ok=True)
                STATE.write_text(json.dumps(state))
            for job in cli.call(f"/api/agent/jobs?id={state['id']}").get("jobs", []):
                print("job:", job["name"])
                run_job(cli, job)
        except Exception as e:
            print("server unreachable:", e)
        if a.once:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
