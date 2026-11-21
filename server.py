#!/usr/bin/env python3
"""LANDeploy - deploy operating systems (ISO) and software over your local network.

    sudo python3 server.py            # then open http://<this-pc>:8080
"""
import argparse
import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import library
import unattend
import tools
import pxe

VERSION = "1.6.3"
# RES: bundled read-only files (web/, agent/). HOME: where data/ and bootfiles/ live.
# When packaged with PyInstaller, RES is the unpack folder and HOME is the folder of the executable.
FROZEN = getattr(sys, "frozen", False)
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
HOME = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
ROOT = RES
log = logging.getLogger("landeploy")

DEFAULT_SETTINGS = {
    "server_ip": "", "http_port": 8080, "proxy_dhcp": True, "tftp": True,
    "default_action": "menu",      # unknown machines: "menu" (show menu) or "local" (boot disk silently)
    "menu_timeout": 15, "auto_delay": 8, "confirm_timeout": 60,
    "iso_mode": "mount",           # new ISOs: "mount" (no copy, needs admin) or "extract" (copy the files)
    "smb_share": "landeploy", "smb_user": "", "smb_pass": "",
    "agent_token": "", "admin_password": "",
}


def detect_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))      # no packet is sent; just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def norm_mac(mac):
    m = re.sub(r"[^0-9a-f]", "", (mac or "").lower())
    return ":".join(m[i:i + 2] for i in range(0, 12, 2)) if len(m) == 12 else ""


def now():
    return int(time.time())


# ====================================================================== state
class Store:
    SECTIONS = ("settings", "library", "machines", "assignments", "packages", "agents", "jobs", "events")

    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        for sub in ("isos", "library", "mnt", "unattend", "packages", "winpe-drivers"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "state.json"
        self.lock = threading.RLock()
        self.d = {k: {} for k in self.SECTIONS}
        self.d["events"] = []
        if self.file.exists():
            self.d.update(json.loads(self.file.read_text()))
        s = {**DEFAULT_SETTINGS, **self.d["settings"]}
        s["agent_token"] = s["agent_token"] or secrets.token_urlsafe(12)
        self.d["settings"] = s
        self.save()

    @property
    def settings(self):
        return self.d["settings"]

    def save(self):
        with self.lock:
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.d, indent=1))
            tmp.replace(self.file)

    def event(self, msg, kind="info"):
        with self.lock:
            self.d["events"] = ([{"t": now(), "msg": msg, "k": kind}] + self.d["events"])[:200]
            self.save()
        log.info(msg)

    def machine(self, mac, **fields):
        mac = norm_mac(mac)
        if not mac:
            return None
        with self.lock:
            m = self.d["machines"].setdefault(mac, {"mac": mac, "first_seen": now(), "name": ""})
            m.update({k: v for k, v in fields.items() if v not in (None, "")})
            m["last_seen"] = now()
            self.save()
            return m


# ====================================================================== ISO worker
class Library:
    """Prepares ISOs in one of two ways:
       extract: copy the files to data/library/<id>   (works everywhere, uses disk space)
       mount:   mount the ISO read-only at data/mnt/<id> (no copy; needs admin rights)"""

    def __init__(self, store):
        self.store = store
        self.q, self.cv = [], threading.Condition()
        threading.Thread(target=self._worker, daemon=True, name="prepare").start()

    def root(self, e):
        """Folder holding the ISO's files for this entry."""
        sub = "mnt" if e.get("mode") == "mount" else "library"
        return self.store.dir / sub / e["id"]

    def scan(self):
        """Register new *.iso files, re-prepare ISOs that changed on disk, and restore mounts after a restart."""
        todo = []
        with self.store.lock:
            L = self.store.d["library"]
            by_iso = {e["iso"]: e for e in L.values()}
            for iso in sorted((self.store.dir / "isos").glob("*.[iI][sS][oO]")):
                st = iso.stat()
                e = by_iso.get(iso.name)
                if not e:
                    eid = uuid.uuid4().hex[:8]
                    L[eid] = {"id": eid, "iso": iso.name, "name": iso.stem.replace("_", " "), "status": "queued",
                              "size": st.st_size, "boot": {}, "enabled": True, "extra_args": "",
                              "mode": self.store.settings.get("iso_mode", "extract"),
                              "unattend": "", "autoinstall": "", "kickstart": "", "added": now()}
                    todo.append(eid)
                elif e.get("mtime") and int(st.st_mtime) != e["mtime"]:
                    e.update(size=st.st_size, status="queued")  # the ISO was replaced (e.g. a new custom build)
                    todo.append(e["id"])
                elif e.get("mode") == "mount" and e.get("status") == "ready" and not library.is_mounted(self.root(e)):
                    e["status"] = "queued"                      # server restarted: mount again
                    todo.append(e["id"])
            self.store.save()
        for eid in todo:
            self.prepare(eid)
        return todo

    def prepare(self, eid):
        with self.cv:
            if eid not in self.q:
                self.q.append(eid)
            self.cv.notify()

    def release(self, e, keep=None):
        """Undo how an entry was prepared (unmount and/or delete the extracted copy)."""
        iso = self.store.dir / "isos" / e["iso"]
        if keep != "mount":
            library.unmount_iso(iso, self.store.dir / "mnt" / e["id"])
        if keep != "extract":
            shutil.rmtree(self.store.dir / "library" / e["id"], ignore_errors=True)

    def _worker(self):
        while True:
            with self.cv:
                while not self.q:
                    self.cv.wait()
                eid = self.q.pop(0)
            e = self.store.d["library"].get(eid)
            if not e:
                continue
            iso = self.store.dir / "isos" / e["iso"]
            mode = e.get("mode") or "extract"
            if not iso.is_file():
                e["status"] = "error: ISO file missing"
                self.store.save()
                continue
            e["status"] = "mounting" if mode == "mount" else "extracting"
            self.store.save()
            note = ""
            try:
                if mode == "mount":
                    try:
                        self.release(e)
                        tool = library.mount_iso(iso, self.root(e))
                    except Exception as ex:                    # e.g. no admin rights: copy the files instead
                        note = f" (mounting failed: {str(ex)[:120]}, extracted instead)"
                        e["mode"] = mode = "extract"
                if mode == "extract":
                    self.release(e, keep="extract")
                    tool = library.extract_iso(iso, self.root(e))
                e["boot"] = library.detect(self.root(e))
                e["mtime"] = int(iso.stat().st_mtime)
                e["status"] = "ready"
                self.store.event(f"Prepared {e['iso']} ({tool}): {library.TYPE_LABELS[e['boot']['type']]}{note}")
            except Exception as ex:
                e["status"] = f"error: {ex}"
                self.store.event(f"Failed to prepare {e['iso']}: {ex}")
            self.store.save()


# ====================================================================== HTTP
class App:
    def __init__(self, data_dir, bootfiles=None):
        self.store = Store(data_dir)
        s = self.store.settings
        self.ip = s["server_ip"] or detect_ip()
        self.port = int(s["http_port"])
        self.base = f"http://{self.ip}:{self.port}"
        self.bootfiles = Path(bootfiles) if bootfiles else HOME / "bootfiles"
        self.bootfiles.mkdir(parents=True, exist_ok=True)
        self.lib = Library(self.store)
        self.services = {}          # "dhcp"/"tftp" -> service object with .ok once its port is bound
        self.live = {}              # mac -> {pxe_at, dhcp_at, ping_at}: in memory only, changes every few seconds
        self.tools = tools.Tools(self.store.dir / "tools", self.bootfiles, [RES / "agent" / "bin"])

    # ------------------------------------------------ PXE hooks
    def choose_bootfile(self, mac, arch, is_ipxe):
        if is_ipxe:
            return f"{self.base}/boot.ipxe"
        names = {0: ["undionly.kpxe"], 6: ["ipxe-i386.efi"], 7: ["snponly.efi", "ipxe.efi"],
                 9: ["snponly.efi", "ipxe.efi"], 11: ["snponly-arm64.efi", "ipxe-arm64.efi"]}.get(arch, [])
        if arch == 16:                                   # UEFI HTTP boot: skip TFTP entirely
            return f"{self.base}/bootfiles/ipxe.efi"
        for n in names:
            if (self.bootfiles / n).is_file():
                return n
        log.warning("No iPXE binary for arch %s in %s (run fetch_bootfiles.py)", arch, self.bootfiles)
        return None

    def on_pxe_seen(self, mac, arch):
        m = self.store.machine(mac, arch=arch, state="pxe")
        live = self.live.setdefault(norm_mac(mac), {})
        live["pxe_at"] = now()
        if now() - live.get("pxe_event", 0) > 120:     # one notice per boot, not per packet
            live["pxe_event"] = now()
            self.store.event(f"{(m or {}).get('name') or mac} is network-booting", kind="boot")

    def on_dhcp(self, req):
        """Any known PC asking for an IP address has just been switched on (or woke up)."""
        mac = norm_mac(req["mac"])
        m = self.store.d["machines"].get(mac)
        if not m:
            return                                     # phones, printers... are not tracked
        self.live.setdefault(mac, {})["dhcp_at"] = now()
        ip = req["req_ip"] or (socket.inet_ntoa(req["ciaddr"]) if req["ciaddr"] != b"\0\0\0\0" else "")
        if ip and m.get("ip") != ip:
            self.store.machine(mac, ip=ip)

    def monitor(self, interval=20):
        """Ping every known PC with an IP address, in the background."""
        from concurrent.futures import ThreadPoolExecutor

        def one(mac, ip):
            if pxe.ping(ip):
                self.live.setdefault(mac, {})["ping_at"] = now()

        def loop():
            with ThreadPoolExecutor(32) as pool:
                while True:
                    targets = {mac: m["ip"] for mac, m in list(self.store.d["machines"].items()) if m.get("ip")}
                    for a in list(self.store.d["agents"].values()):
                        for mac in a.get("macs", []):
                            if mac in self.store.d["machines"] and a.get("ip"):
                                targets.setdefault(mac, a["ip"])
                    list(pool.map(lambda kv: one(*kv), targets.items()))
                    time.sleep(interval)
        threading.Thread(target=loop, daemon=True, name="monitor").start()

    def status(self, m, agent=None):
        """booting | installing | on | off  (from network boot, install, agent, ping and DHCP signals)"""
        t, live = now(), self.live.get(m["mac"], {}) if m else {}
        if t - live.get("pxe_at", 0) < 90:
            return "booting"
        agent_on = bool(agent and t - agent.get("last_seen", 0) < 90)
        if (m and m.get("state") == "installing" and t - m.get("last_deploy_at", 0) < 2 * 3600
                and not (agent_on and agent.get("last_seen", 0) > m.get("last_deploy_at", 0) + 300)):
            return "installing"
        if agent_on or t - live.get("ping_at", 0) < 60 or t - live.get("dhcp_at", 0) < 60:
            return "on"
        return "off"

    # ------------------------------------------------ iPXE scripts
    def menu_script(self, q):
        st = self.store
        mac = norm_mac(q.get("mac"))
        m = st.machine(mac, platform=q.get("platform"), buildarch=q.get("arch"), manufacturer=q.get("manuf"),
                       product=q.get("product"), serial=q.get("serial"), ip=q.get("ip"), state="pxe")
        s, base, b = st.settings, self.base, self.base
        ident = f"mac={quote(mac)}"
        a = st.d["assignments"].get(mac)
        e = a and st.d["library"].get(a["entry_id"])
        if e and e.get("status") == "ready" and q.get("skip") != "1":
            name = library.clean(e["name"])
            if a["mode"] == "auto":
                return (f"#!ipxe\necho\necho ===== LANDeploy =====\necho This PC is scheduled to install: {name}\n"
                        f"prompt --timeout {int(s['auto_delay']) * 1000} Starting in {s['auto_delay']} s - press any key to cancel... "
                        f"&& goto menu || goto go\n:go\nchain {b}/boot/{e['id']}.ipxe?{ident} || goto menu\n"
                        f":menu\nchain {b}/menu.ipxe?{ident}&skip=1\n")
            return (f"#!ipxe\necho\necho ===== LANDeploy =====\necho The administrator scheduled: {name}\n"
                    "echo WARNING: installing an operating system can ERASE this computer's disk.\n"
                    f"prompt --key y --timeout {int(s['confirm_timeout']) * 1000} Press Y to install now, any other key for the menu... "
                    f"&& goto go || goto menu\n:go\nchain {b}/boot/{e['id']}.ipxe?{ident} || goto menu\n"
                    f":menu\nchain {b}/menu.ipxe?{ident}&skip=1\n")
        if s["default_action"] == "local" and q.get("skip") != "1" and not a:
            return "#!ipxe\necho LANDeploy: no deployment scheduled, booting local disk\nexit\n"
        title = library.clean((m or {}).get("name") or mac)
        out = ["#!ipxe", f"menu LANDeploy - {title}", "item --gap -- Operating systems"]
        entries = [x for x in st.d["library"].values() if x.get("status") == "ready" and x.get("enabled")]
        for x in entries:
            out.append(f"item e{x['id']} {library.clean(x['name'])}  [{x['boot'].get('type')}]")
        if not entries:
            out.append("item --gap --   (library is empty - add ISOs in the web console)")
        out += ["item --gap -- Tools", "item local Boot from local disk", "item netbootxyz netboot.xyz (Internet)",
                "item shell iPXE shell", "item reboot Reboot",
                f"choose --default local --timeout {int(s['menu_timeout']) * 1000} target && goto ${{target}} || goto local"]
        for x in entries:
            out += [f":e{x['id']}", f"chain {b}/boot/{x['id']}.ipxe?{ident} || goto failed"]
        out += [":local", "echo Booting from local disk...", "exit",
                ":netbootxyz", "iseq ${platform} efi && chain --autofree https://boot.netboot.xyz/ipxe/netboot.xyz.efi "
                "|| chain --autofree https://boot.netboot.xyz/ipxe/netboot.xyz.lkrn", "goto failed",
                ":shell", "shell", ":reboot", "reboot",
                ":failed", "echo Boot failed.", "prompt Press any key to return to the menu",
                f"chain {base}/menu.ipxe?{ident}&skip=1"]
        return "\n".join(out) + "\n"

    def winpe_drivers(self):
        """Driver files added for Windows PE (network card drivers WinPE lacks)."""
        d = self.store.dir / "winpe-drivers"
        return sorted(f.name for f in d.iterdir() if f.is_file() and re.fullmatch(r"[\w.\-]+", f.name)) if d.is_dir() else []

    def win_profile(self):
        return {**unattend.DEFAULT_PROFILE, **self.store.settings.get("win_profile", {})}

    def answer_mode(self, e):
        """profile (Windows setup page) | custom (XML pasted in the ISO's Options) | none"""
        return e.get("answer") or ("custom" if e.get("unattend") else "profile")

    def answer_file(self, e, mac):
        """Write the answer file this PC will use and return its name (None = no answer file)."""
        mode, st = self.answer_mode(e), self.store
        if mode == "custom" and e.get("unattend"):
            name, xml = f"{e['id']}.xml", e["unattend"]
        elif mode == "profile" and self.win_profile().get("enabled"):
            prof = self.win_profile()
            with st.lock:
                m = st.d["machines"].get(mac) or {}
                if prof.get("computer_names") == "pattern" and m and "win_number" not in m:
                    used = [x.get("win_number", -1) for x in st.d["machines"].values()]
                    m["win_number"] = max([int(prof.get("pc_start", 1)) - 2] + used) + 1
                    st.save()
            name = f"{e['id']}-{mac.replace(':', '') or 'any'}.xml"
            xml = unattend.build(prof, m, m.get("win_number", 0), uefi=(m.get("platform") or "efi") == "efi")
        else:
            return None
        (st.dir / "unattend" / name).write_text(xml, encoding="utf-8")     # outside the (read-only) ISO
        return name

    def entry_script(self, eid, q):
        st = self.store
        e = st.d["library"].get(eid)
        if not e or e.get("status") != "ready":
            return None
        mac = norm_mac(q.get("mac"))
        with st.lock:
            a = st.d["assignments"].get(mac)
            if a and a["entry_id"] == eid:
                st.d["assignments"].pop(mac)       # one-shot: next network boot falls back to the disk
            if mac:
                st.machine(mac, state="installing", last_deploy=e["name"], last_deploy_at=now())
        st.event(f"{mac or 'A PC'} started installing {e['name']}")
        return library.boot_script(e, self.base, mac, self.winpe_drivers() if e["boot"].get("type") == "windows" else ())


def make_handler(app):
    st = app.store

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "LANDeploy/1.0"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        # ---------------- helpers
        def send(self, code=200, body=b"", ctype="text/plain; charset=utf-8", headers=None):
            if isinstance(body, str):
                body = body.encode()
            elif not isinstance(body, bytes):
                body, ctype = json.dumps(body).encode(), "application/json"
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def send_file(self, path, download=False):
            if not path or not path.is_file():
                return self.send(404, "not found")
            size = path.stat().st_size
            start, end, code = 0, size - 1, 200
            m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
            if m and size:
                a, b = m.groups()
                if a:
                    start, end = int(a), min(int(b) if b else size - 1, size - 1)
                elif b:
                    start = max(0, size - int(b))
                if start > end:
                    return self.send(416, "", headers={"Content-Range": f"bytes */{size}"})
                code = 206
            self.send_response(code)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def body_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def authed_admin(self):
            pw = st.settings.get("admin_password")
            if not pw:
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    if base64.b64decode(h[6:]).decode().split(":", 1)[1] == pw:
                        return True
                except Exception:
                    pass
            self.send(401, "login required", headers={"WWW-Authenticate": 'Basic realm="LANDeploy"'})
            return False

        def authed_agent(self, q, body=None):
            tok = self.headers.get("X-Agent-Token") or q.get("token") or (body or {}).get("token")
            if tok == st.settings["agent_token"]:
                return True
            self.send(403, {"error": "bad agent token"})
            return False

        # ---------------- routing
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urlparse(self.path)
            p, q = unquote(u.path), {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                self.route_get(p, q)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def route_get(self, p, q):
            ipxe = "text/plain; charset=utf-8"
            if p == "/boot.ipxe":
                return self.send(body=(
                    "#!ipxe\n"
                    f"chain {app.base}/menu.ipxe?mac=${{netX/mac}}&ip=${{netX/ip}}&platform=${{platform}}"
                    "&arch=${buildarch}&manuf=${manufacturer:uristring}&product=${product:uristring}"
                    "&serial=${serial:uristring}\n"), ctype=ipxe)
            if p == "/menu.ipxe":
                return self.send(body=app.menu_script(q), ctype=ipxe)
            m = re.fullmatch(r"/boot/(\w+)\.ipxe", p)
            if m:
                s = app.entry_script(m.group(1), q)
                return self.send(200 if s else 404, s or "#!ipxe\necho entry not ready\nprompt\n", ctype=ipxe)
            m = re.fullmatch(r"/files/(\w+)/(.+)", p)
            if m:
                e = st.d["library"].get(m.group(1))
                return self.send_file(library.find_ci(app.lib.root(e), m.group(2)) if e else None)
            m = re.fullmatch(r"/isos/([^/]+)", p)
            if m:
                return self.send_file(st.dir / "isos" / Path(m.group(1)).name)
            m = re.fullmatch(r"/winpe-drivers/([\w.\-]+)", p)
            if m:
                return self.send_file(st.dir / "winpe-drivers" / m.group(1))
            m = re.fullmatch(r"/bootfiles/([\w.\-]+)", p)
            if m:
                return self.send_file(app.bootfiles / m.group(1))
            m = re.fullmatch(r"/winpe/(\w+)/(winpeshl\.ini|install\.cmd)", p)
            if m and m.group(1) in st.d["library"]:
                e = st.d["library"][m.group(1)]
                ans = app.answer_file(e, norm_mac(q.get("mac"))) if m.group(2) == "install.cmd" else None
                ini, cmd = library.winpe_files(e, st.settings, app.ip, ans, app.winpe_drivers())
                return self.send(body=ini if m.group(2) == "winpeshl.ini" else cmd)
            m = re.fullmatch(r"/autoinstall/(\w+)/(user-data|meta-data|vendor-data)", p)
            if m and m.group(1) in st.d["library"]:
                e = st.d["library"][m.group(1)]
                return self.send(body=e.get("autoinstall", "") if m.group(2) == "user-data" else "")
            m = re.fullmatch(r"/kickstart/(\w+)\.cfg", p)
            if m and m.group(1) in st.d["library"]:
                return self.send(body=st.d["library"][m.group(1)].get("kickstart", ""))
            m = re.fullmatch(r"/pkg/(\w+)/([^/]+)", p)
            if m:
                if not self.authed_agent(q):
                    return
                return self.send_file(st.dir / "packages" / m.group(1) / Path(m.group(2)).name)
            if p.startswith("/api/agent/"):
                return self.agent_get(p, q)
            if p in ("/", "/index.html"):
                if self.authed_admin():
                    self.send_file(ROOT / "web" / "index.html")
                return
            if p == "/agent/landeploy_agent.py":
                return self.send_file(ROOT / "agent" / "landeploy_agent.py")
            m = re.fullmatch(r"/agent/bin/([\w.\-]+)", p)             # compiled agents for client PCs
            if m:
                return self.send_file(app.tools.agent_file(m.group(1)), download=True)
            m = re.fullmatch(r"/tools/([\w.\-]+)", p)
            if m:
                return self.send_file(app.tools.dir / m.group(1),
                                      download=True)
            if p in ("/setup.ps1", "/setup.cmd", "/setup.sh"):    # one-click agent installers
                tok = st.settings["agent_token"]
                if q.get("t") != tok:
                    return self.send(403, "bad or missing token (?t=...)")
                if p == "/setup.ps1":
                    return self.send(body=tools.setup_ps1(app.base, tok))
                if p == "/setup.sh":
                    return self.send(body=tools.setup_sh(app.base, tok))
                return self.send(body=tools.setup_cmd(app.base, tok), ctype="application/octet-stream",
                                 headers={"Content-Disposition": 'attachment; filename="LANDeploy-Agent-Setup.cmd"'})
            if p.startswith("/api/"):
                if self.authed_admin():
                    self.admin_get(p, q)
                return
            self.send(404, "not found")

        def do_POST(self):
            u = urlparse(self.path)
            p, q = unquote(u.path), {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if p.startswith("/api/agent/"):
                    return self.agent_post(p, q)
                if self.authed_admin():
                    self.admin_post(p, q)
            except Exception as ex:
                log.exception("POST %s", p)
                self.send(500, {"error": str(ex)})

        # ---------------- admin API
        def admin_get(self, p, q):
            if p == "/api/state":
                with st.lock:
                    d = json.loads(json.dumps(st.d))
                d["server"] = {"ip": app.ip, "port": app.port, "base": app.base,
                               "bootfiles": sorted(x.name for x in app.bootfiles.glob("*") if x.is_file()),
                               "data_dir": str(st.dir.resolve()), "version": VERSION,
                               "pxe": {k: getattr(v, "ok", False) for k, v in app.services.items()},
                               "pxe_enabled": bool(app.services)}
                agent_by_mac = {mac: a for a in st.d["agents"].values() for mac in a.get("macs", [])}
                for mac, m in d["machines"].items():
                    m["status"] = app.status(st.d["machines"].get(mac, m), agent_by_mac.get(mac))
                for a in d["agents"].values():
                    a["status"] = "on" if now() - a.get("last_seen", 0) < 90 else "off"
                d["server"]["now"] = now()
                d["tools"] = app.tools.info()
                d["winpe_drivers"] = app.winpe_drivers()
                d["win_profile"] = app.win_profile()
                d["win_options"] = {"languages": unattend.LANGUAGES, "keyboards": unattend.KEYBOARDS,
                                    "timezones": unattend.TIMEZONES}
                return self.send(body=d)
            if p == "/api/smbconf":                        # Windows 11 24H2 Setup refuses guest shares: use a user
                u, d, sh = st.settings.get("smb_user") or "landeploy", st.dir.resolve(), st.settings["smb_share"]
                return self.send(body=(
                    f"# 1. Install Samba and create a share user (any password; it is not your login):\n"
                    f"sudo apt install samba\n"
                    f"sudo useradd -M -s /usr/sbin/nologin {u}\n"
                    f"sudo smbpasswd -a {u}\n\n"
                    f"# 2. Add this at the END of /etc/samba/smb.conf  (sudo nano /etc/samba/smb.conf):\n"
                    f"[{sh}]\n   path = {d}\n   read only = yes\n   valid users = {u}\n   browseable = yes\n\n"
                    f"# 3. Restart Samba and open the firewall:\n"
                    f"sudo systemctl restart smbd\n"
                    f"sudo ufw allow samba\n\n"
                    f"# 4. In LANDeploy > Settings > Windows SMB share, enter user '{u}' and the same password.\n\n"
                    f"# Windows server instead: share the folder {d} as '{sh}' (read access) for a local\n"
                    f"# user account, and enter that user and password in Settings."))
            self.send(404, {"error": "unknown"})

        def admin_post(self, p, q):
            L = st.d["library"]
            if p == "/api/upload":                         # raw streaming upload: ?kind=iso|package&name=..&id=..
                name = Path(q.get("name", "upload.bin")).name
                if q.get("kind") == "iso":
                    dest = st.dir / "isos" / name
                elif q.get("kind") == "winpedrv":
                    if not re.fullmatch(r"[\w.\-]+", name):
                        return self.send(400, {"error": "use simple file names (letters, digits, . - _)"})
                    dest = st.dir / "winpe-drivers" / name
                else:
                    pk = st.d["packages"].get(q.get("id", ""))
                    if not pk:
                        return self.send(404, {"error": "package not found"})
                    d = st.dir / "packages" / pk["id"]
                    if d.exists():
                        shutil.rmtree(d)
                    d.mkdir(parents=True)
                    dest = d / name
                left, h = int(self.headers.get("Content-Length") or 0), hashlib.sha256()
                tmp = dest.with_name(dest.name + ".part")
                with open(tmp, "wb") as f:
                    while left > 0:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            break
                        f.write(chunk)
                        h.update(chunk)
                        left -= len(chunk)
                if left:
                    tmp.unlink(missing_ok=True)
                    return self.send(400, {"error": "upload interrupted"})
                tmp.replace(dest)
                if q.get("kind") == "iso":
                    app.lib.scan()
                elif q.get("kind") == "winpedrv":
                    pass
                else:
                    pk.update(file=name, sha256=h.hexdigest(), size=dest.stat().st_size)
                    st.save()
                st.event(f"Uploaded {name}")
                return self.send(body={"ok": True})
            if p == "/api/tools/fetch":                    # {what: "bootfiles"}
                app.tools.fetch(self.body_json().get("what"))
                return self.send(body={"ok": True})
            if p == "/api/winpe-drivers/clear":
                for f in (st.dir / "winpe-drivers").iterdir():
                    f.unlink()
                st.event("Removed the Windows PE drivers")
                return self.send(body={"ok": True})
            if p == "/api/library/scan":
                return self.send(body={"added": app.lib.scan()})
            m = re.fullmatch(r"/api/library/(\w+)/(update|prepare|delete)", p)
            if m and m.group(1) in L:
                eid, act, e = m.group(1), m.group(2), L[m.group(1)]
                if act == "update":
                    b = self.body_json()
                    for k in ("name", "enabled", "extra_args", "unattend", "autoinstall", "kickstart", "answer"):
                        if k in b:
                            e[k] = b[k]
                    if b.get("mode") in ("mount", "extract") and b["mode"] != e.get("mode", "extract"):
                        e["mode"], e["status"] = b["mode"], "queued"
                        app.lib.prepare(eid)                   # switch between mounted and extracted
                    st.save()
                elif act == "prepare":
                    e["status"] = "queued"
                    app.lib.prepare(eid)
                else:
                    L.pop(eid)
                    app.lib.release(e)
                    if self.body_json().get("delete_iso"):
                        (st.dir / "isos" / e["iso"]).unlink(missing_ok=True)
                    st.save()
                return self.send(body={"ok": True})
            if p == "/api/assign":                         # {macs: [...] or mac, entry_id, mode: auto|confirm, wake}
                b = self.body_json()
                macs = [norm_mac(x) for x in (b.get("macs") or [b.get("mac")])]
                if not macs or not all(macs):
                    return self.send(400, {"error": "invalid MAC address"})
                if not b.get("entry_id"):
                    for mac in macs:
                        st.d["assignments"].pop(mac, None)
                    st.save()
                    return self.send(body={"ok": True})
                if b["entry_id"] not in L:
                    return self.send(400, {"error": "unknown OS"})
                mode = b.get("mode", "confirm")
                for mac in macs:
                    st.machine(mac)
                    st.d["assignments"][mac] = {"entry_id": b["entry_id"], "mode": mode, "created": now()}
                    if b.get("wake"):
                        pxe.send_wol(mac)                  # wake them all: they PXE-boot in parallel
                st.save()
                st.event(f"Scheduled {L[b['entry_id']]['name']} on {len(macs)} computer(s) ({mode})")
                return self.send(body={"ok": True, "count": len(macs)})
            if p == "/api/winprofile":                     # save the Windows setup page
                prof = {**unattend.DEFAULT_PROFILE, **self.body_json()}
                errs = unattend.check(prof) if prof.get("enabled") else []
                st.settings["win_profile"] = prof
                st.save()
                return self.send(body={"ok": True, "problems": errs})
            if p == "/api/winprofile/preview":             # {profile, mac?} -> XML for one PC
                b = self.body_json()
                prof = {**unattend.DEFAULT_PROFILE, **b.get("profile", {})}
                m = st.d["machines"].get(norm_mac(b.get("mac"))) or {"name": "Salle-Info-01", "platform": "efi"}
                xml = unattend.build(prof, m, m.get("win_number", 0), uefi=(m.get("platform") or "efi") == "efi")
                return self.send(body={"xml": xml, "problems": unattend.check(prof)})
            if p == "/api/wol":
                pxe.send_wol(norm_mac(self.body_json().get("mac")))
                return self.send(body={"ok": True})
            if p == "/api/computers/remove":               # {macs: [...], agents: [...]}
                b = self.body_json()
                macs = {norm_mac(x) for x in b.get("macs", [])} - {""}
                agents = set(b.get("agents", []))
                with st.lock:
                    for a in st.d["agents"].values():      # a PC's agent goes with it
                        if macs & set(a.get("macs", [])):
                            agents.add(a["id"])
                    names = [st.d["machines"].get(m, {}).get("name") or m for m in macs if m in st.d["machines"]]
                    names += [st.d["agents"][x]["hostname"] for x in agents if x in st.d["agents"]
                              and not macs & set(st.d["agents"][x].get("macs", []))]
                    for m in macs:
                        st.d["machines"].pop(m, None)
                        st.d["assignments"].pop(m, None)   # cancel a scheduled OS install
                        app.live.pop(m, None)
                    for x in agents:
                        st.d["agents"].pop(x, None)
                    for j in st.d["jobs"].values():        # software not yet installed is cancelled
                        if j["agent_id"] in agents and j["status"] in ("pending", "asking"):
                            j.update(status="cancelled", updated=now())
                    st.save()
                if names:
                    st.event(f"Removed from the list: {', '.join(names)}")
                return self.send(body={"ok": True, "removed": len(names)})
            if p == "/api/machine":
                b = self.body_json()
                mac = norm_mac(b.get("mac"))
                if b.get("delete"):
                    st.d["machines"].pop(mac, None)
                    st.save()
                else:
                    st.machine(mac, name=b.get("name"))
                return self.send(body={"ok": True})
            if p == "/api/settings":
                b = self.body_json()
                for k, v in b.items():
                    if k in DEFAULT_SETTINGS:
                        st.settings[k] = type(DEFAULT_SETTINGS[k])(v) if DEFAULT_SETTINGS[k] is not None else v
                st.save()
                return self.send(body={"ok": True, "note": "IP/port changes apply after restart"})
            if p == "/api/packages":                       # create/update {id?, name, os, args, command, consent}
                b = self.body_json()
                pid = b.get("id") or uuid.uuid4().hex[:8]
                pk = st.d["packages"].setdefault(pid, {"id": pid, "file": "", "sha256": "", "created": now()})
                for k in ("name", "os", "args", "command", "consent", "run_as"):
                    if k in b:
                        pk[k] = b[k]
                st.save()
                return self.send(body=pk)
            m = re.fullmatch(r"/api/packages/(\w+)/delete", p)
            if m:
                st.d["packages"].pop(m.group(1), None)
                shutil.rmtree(st.dir / "packages" / m.group(1), ignore_errors=True)
                st.save()
                return self.send(body={"ok": True})
            if p == "/api/deploy":                         # {package_id, agent_ids: [...]}
                b = self.body_json()
                pk = st.d["packages"].get(b.get("package_id"))
                if not pk:
                    return self.send(400, {"error": "unknown package"})
                ids = []
                for aid in b.get("agent_ids", []):
                    if aid not in st.d["agents"]:
                        continue
                    jid = uuid.uuid4().hex[:10]
                    st.d["jobs"][jid] = {"id": jid, "agent_id": aid, "package_id": pk["id"], "package": pk["name"],
                                         "status": "pending", "message": "", "created": now(), "updated": now()}
                    ids.append(jid)
                st.save()
                st.event(f"Queued {pk['name']} for {len(ids)} computer(s)")
                return self.send(body={"jobs": ids})
            m = re.fullmatch(r"/api/jobs/(\w+)/cancel", p)
            if m and m.group(1) in st.d["jobs"]:
                j = st.d["jobs"][m.group(1)]
                if j["status"] in ("pending", "asking"):
                    j.update(status="cancelled", updated=now())
                    st.save()
                return self.send(body={"ok": True})
            if p == "/api/events/clear":
                st.d["events"] = []
                st.d["jobs"] = {k: v for k, v in st.d["jobs"].items() if v["status"] in ("pending", "asking", "running")}
                st.save()
                return self.send(body={"ok": True})
            self.send(404, {"error": "unknown endpoint"})

        # ---------------- agent API
        def agent_get(self, p, q):
            if not self.authed_agent(q):
                return
            if p == "/api/agent/jobs":
                aid = q.get("id", "")
                ag = st.d["agents"].get(aid)
                if not ag:
                    return self.send(404, {"error": "register first"})
                ag["last_seen"] = now()
                out = []
                for j in st.d["jobs"].values():
                    if j["agent_id"] == aid and j["status"] == "pending":
                        pk = st.d["packages"].get(j["package_id"])
                        if not pk:
                            continue
                        out.append({"job_id": j["id"], "name": pk["name"], "args": pk.get("args", ""),
                                    "command": pk.get("command", ""), "consent": pk.get("consent", True),
                                    "run_as": pk.get("run_as", "system"),
                                    "file": pk.get("file", ""), "sha256": pk.get("sha256", ""),
                                    "url": f"{app.base}/pkg/{pk['id']}/{quote(pk['file'])}" if pk.get("file") else ""})
                return self.send(body={"jobs": out})
            self.send(404, {"error": "unknown"})

        def agent_post(self, p, q):
            b = self.body_json()
            if not self.authed_agent(q, b):
                return
            if p == "/api/agent/hello":
                aid = b.get("id") or uuid.uuid4().hex
                with st.lock:
                    new = aid not in st.d["agents"]
                    st.d["agents"][aid] = {"id": aid, "hostname": b.get("hostname", "?"), "os": b.get("os", "?"),
                                           "user": b.get("user", ""), "macs": [norm_mac(x) for x in b.get("macs", [])],
                                           "ip": self.client_address[0], "version": b.get("version", ""),
                                           "last_seen": now()}
                    for mac in st.d["agents"][aid]["macs"]:
                        m = st.d["machines"].get(mac)
                        if m:
                            m["name"] = m.get("name") or b.get("hostname")
                            m["ip"] = self.client_address[0]
                            if m.get("state") == "installing" and now() - m.get("last_deploy_at", 0) > 300:
                                m["state"] = "ready"
                                st.event(f"{m['name'] or mac} finished installing {m.get('last_deploy', '')}".strip())
                    st.save()
                if new:
                    st.event(f"Agent registered: {b.get('hostname')} ({b.get('os')})")
                return self.send(body={"id": aid})
            if p == "/api/agent/report":
                j = st.d["jobs"].get(b.get("job_id"))
                if not j:
                    return self.send(404, {"error": "unknown job"})
                if j["status"] == "cancelled":
                    return self.send(body={"cancelled": True})
                j.update(status=b.get("status", j["status"]), message=str(b.get("message", ""))[-2000:], updated=now())
                st.save()
                if j["status"] in ("success", "failed", "denied"):
                    host = st.d["agents"].get(j["agent_id"], {}).get("hostname", "?")
                    st.event(f"{j['package']} on {host}: {j['status']}")
                return self.send(body={"ok": True})
            self.send(404, {"error": "unknown"})

    return H


def watch_parent(pid):
    """Exit when the desktop window that started us goes away (we may run elevated, separately)."""
    def alive():
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)      # SYNCHRONIZE
            if not h:
                return False
            still = ctypes.windll.kernel32.WaitForSingleObject(h, 0) == 0x102   # WAIT_TIMEOUT
            ctypes.windll.kernel32.CloseHandle(h)
            return still
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    def loop():
        while alive():
            time.sleep(2)
        log.info("Desktop app closed, stopping server")
        os._exit(0)
    threading.Thread(target=loop, daemon=True, name="parent-watch").start()


def main(argv=None):
    ap = argparse.ArgumentParser(description="LANDeploy server")
    ap.add_argument("--version", action="version", version=f"LANDeploy {VERSION}")
    ap.add_argument("--data", default=str(HOME / "data"), help="data folder (ISOs, extracted trees, packages)")
    ap.add_argument("--bootfiles", help="folder with iPXE/wimboot files (default: next to the program)")
    ap.add_argument("--ip", help="IP address of this server on the LAN (auto-detected)")
    ap.add_argument("--port", type=int, help="HTTP port (default 8080)")
    ap.add_argument("--no-pxe", action="store_true", help="don't start ProxyDHCP/TFTP (use your own DHCP/dnsmasq)")
    ap.add_argument("--log", help="also write the log to this file")
    ap.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    handlers = []
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    if a.log:
        Path(a.log).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(a.log, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, handlers=handlers or [logging.NullHandler()],
                        format="%(asctime)s %(name)-8s %(message)s", datefmt="%H:%M:%S")
    if a.parent_pid:
        watch_parent(a.parent_pid)
    app = App(a.data, a.bootfiles)
    if a.ip:
        app.ip = a.ip
    if a.port:
        app.port = a.port
    app.base = f"http://{app.ip}:{app.port}"
    s = app.store.settings
    if not a.no_pxe:
        if s["proxy_dhcp"]:
            app.services["dhcp"] = pxe.ProxyDHCP(app.ip, app.choose_bootfile, app.on_pxe_seen, app.on_dhcp)
        if s["tftp"]:
            app.services["tftp"] = pxe.TFTPServer(app.bootfiles)
        for svc in app.services.values():
            svc.start()
    pxe.Beacon(app.base).start()
    app.monitor()
    app.lib.scan()
    httpd = ThreadingHTTPServer(("", app.port), make_handler(app))
    httpd.daemon_threads = True
    log.info("Web console: %s   (agent token: %s)", app.base, s["agent_token"])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
