"""Network-boot plumbing: ProxyDHCP (UDP 67 + 4011), TFTP (UDP 69) and a
discovery beacon (UDP 4012) for agents.

ProxyDHCP means LANDeploy never hands out IP addresses: your router keeps
doing that. We only answer PXE clients with "your boot file is X on server Y",
exactly like dnsmasq's `dhcp-range=...,proxy` mode or WDS.
"""
import logging
import socket
import struct
import threading
from pathlib import Path

log = logging.getLogger("pxe")
MAGIC = b"\x63\x82\x53\x63"

# DHCP option 93 (client system architecture), RFC 4578 + IANA registry
ARCH_NAMES = {0: "bios", 6: "efi32", 7: "efi64", 9: "efi64", 10: "arm32", 11: "arm64", 16: "efi64-http"}


def parse_dhcp(data):
    if len(data) < 240 or data[236:240] != MAGIC:
        return None
    hlen = data[2] or 6
    opts, i = {}, 240
    while i < len(data):
        code = data[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        if i + 1 >= len(data):
            break
        ln = data[i + 1]
        opts[code] = data[i + 2:i + 2 + ln]
        i += 2 + ln
    arch = struct.unpack("!H", opts[93][:2])[0] if len(opts.get(93, b"")) >= 2 else 0
    return {
        "op": data[0], "xid": data[4:8], "flags": data[10:12], "ciaddr": data[12:16],
        "giaddr": data[24:28], "chaddr": data[28:44],
        "mac": ":".join("%02x" % b for b in data[28:28 + min(hlen, 16)]),
        "opts": opts, "arch": arch,
        "msgtype": opts.get(53, b"\0")[0],
        "is_pxe": opts.get(60, b"").startswith(b"PXEClient"),
        "is_ipxe": opts.get(77, b"") == b"iPXE" or 175 in opts,
    }


def build_reply(req, msgtype, server_ip, bootfile):
    pkt = bytearray(240)
    pkt[0], pkt[1], pkt[2] = 2, 1, 6
    pkt[4:8] = req["xid"]
    pkt[10:12] = req["flags"]
    pkt[12:16] = req["ciaddr"] if msgtype == 5 else b"\0\0\0\0"
    pkt[20:24] = socket.inet_aton(server_ip)          # siaddr = next-server
    pkt[24:28] = req["giaddr"]
    pkt[28:44] = req["chaddr"]
    sname = server_ip.encode()[:63]
    pkt[44:44 + len(sname)] = sname
    bf = bootfile.encode()[:127]
    pkt[108:108 + len(bf)] = bf
    pkt[236:240] = MAGIC
    opts = [(53, bytes([msgtype])), (54, socket.inet_aton(server_ip)), (60, b"PXEClient")]
    if 97 in req["opts"]:                             # echo client UUID/GUID
        opts.append((97, req["opts"][97]))
    opts.append((43, bytes([6, 1, 8, 255])))          # PXE discovery control: just use the boot file
    opts.append((66, server_ip.encode()))
    opts.append((67, bf))
    for code, val in opts:
        pkt += bytes([code, len(val)]) + val
    pkt += b"\xff"
    return bytes(pkt)


class ProxyDHCP:
    """choose_bootfile(mac, arch, is_ipxe) -> str ; on_seen(mac, arch_name) -> None"""

    def __init__(self, server_ip, choose_bootfile, on_seen=None):
        self.ip, self.choose, self.on_seen = server_ip, choose_bootfile, on_seen
        self.ok = False

    def start(self):
        for port in (67, 4011):
            threading.Thread(target=self._serve, args=(port,), daemon=True, name=f"dhcp{port}").start()

    def _serve(self, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.bind(("", port))
        except OSError as e:
            log.error("ProxyDHCP cannot bind UDP %d (%s). Run as root/admin, and make sure no DHCP "
                      "server runs on this machine.", port, e)
            return
        log.info("ProxyDHCP listening on UDP %d", port)
        if port == 67:
            self.ok = True
        while True:
            try:
                data, addr = s.recvfrom(4096)
                self._handle(s, port, data, addr)
            except Exception:
                log.exception("ProxyDHCP error")

    def _handle(self, s, port, data, addr):
        req = parse_dhcp(data)
        if not req or req["op"] != 1 or not req["is_pxe"]:
            return
        mt = req["msgtype"]
        bootfile = self.choose(req["mac"], req["arch"], req["is_ipxe"])
        if not bootfile:
            return
        arch = ARCH_NAMES.get(req["arch"], str(req["arch"]))
        if self.on_seen:
            self.on_seen(req["mac"], arch)
        if port == 67 and mt == 1:                     # DISCOVER -> proxy OFFER
            reply, dest = build_reply(req, 2, self.ip, bootfile), ("255.255.255.255", 68)
        elif port == 67 and mt == 3 and req["opts"].get(54) == socket.inet_aton(self.ip):
            reply, dest = build_reply(req, 5, self.ip, bootfile), ("255.255.255.255", 68)
        elif port == 4011 and mt in (1, 3):            # UEFI clients ask the boot service directly
            reply, dest = build_reply(req, 5, self.ip, bootfile), addr
        else:
            return
        s.sendto(reply, dest)
        log.info("PXE %s (%s, %s) -> %s", req["mac"], arch, "iPXE" if req["is_ipxe"] else "firmware", bootfile)


class TFTPServer:
    """Read-only TFTP with blksize/tsize options (RFC 1350/2347/2348/2349)."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.ok = False

    def start(self):
        threading.Thread(target=self._serve, daemon=True, name="tftp").start()

    def _serve(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", 69))
        except OSError as e:
            log.error("TFTP cannot bind UDP 69 (%s). Run as root/admin.", e)
            return
        log.info("TFTP serving %s on UDP 69", self.root)
        self.ok = True
        while True:
            data, addr = s.recvfrom(2048)
            if data[:2] == b"\0\1":
                threading.Thread(target=self._transfer, args=(data, addr), daemon=True).start()

    def _resolve(self, name):
        p = (self.root / name.replace("\\", "/").lstrip("/")).resolve()
        return p if p.is_file() and self.root in p.parents else None

    @staticmethod
    def _err(sock, addr, code, msg):
        sock.sendto(struct.pack("!HH", 5, code) + msg.encode() + b"\0", addr)

    def _transfer(self, data, addr):
        parts = data[2:].split(b"\0")
        name = parts[0].decode(errors="replace")
        opts = {parts[i].decode().lower(): parts[i + 1].decode()
                for i in range(2, len(parts) - 1, 2) if parts[i]}
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("", 0))
        sock.settimeout(2)
        try:
            path = self._resolve(name)
            if not path:
                log.warning("TFTP %s: %s not found", addr[0], name)
                return self._err(sock, addr, 1, "File not found")
            size = path.stat().st_size
            blksize, oack = 512, {}
            if "blksize" in opts:
                blksize = max(8, min(int(opts["blksize"]), 1468))
                oack["blksize"] = str(blksize)
            if "tsize" in opts:
                oack["tsize"] = str(size)
            if oack:
                pkt = b"\0\6" + b"".join(k.encode() + b"\0" + v.encode() + b"\0" for k, v in oack.items())
                if not self._send_wait(sock, addr, pkt, 0):
                    return                                      # client aborted (tsize probe)
            log.info("TFTP %s <- %s (%d bytes)", addr[0], name, size)
            with open(path, "rb") as f:
                block = 1
                while True:
                    chunk = f.read(blksize)
                    if not self._send_wait(sock, addr, struct.pack("!HH", 3, block & 0xFFFF) + chunk, block & 0xFFFF):
                        return
                    if len(chunk) < blksize:
                        return
                    block += 1
        finally:
            sock.close()

    @staticmethod
    def _send_wait(sock, addr, pkt, block, retries=5):
        for _ in range(retries):
            sock.sendto(pkt, addr)
            try:
                while True:
                    resp, raddr = sock.recvfrom(1024)
                    if raddr != addr:
                        continue
                    op, num = struct.unpack("!HH", resp[:4])
                    if op == 5:
                        return False
                    if op == 4 and num == block:
                        return True
            except socket.timeout:
                continue
        return False


class Beacon:
    """Agents broadcast 'LANDEPLOY?' on UDP 4012; we answer with the server URL."""

    def __init__(self, url):
        self.url = url

    def start(self):
        threading.Thread(target=self._serve, daemon=True, name="beacon").start()

    def _serve(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", 4012))
        except OSError as e:
            log.warning("Discovery beacon disabled (%s)", e)
            return
        while True:
            data, addr = s.recvfrom(256)
            if data.strip() == b"LANDEPLOY?":
                s.sendto(b"LANDEPLOY " + self.url.encode(), addr)


def send_wol(mac):
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    pkt = b"\xff" * 6 + raw * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for port in (7, 9):
        s.sendto(pkt, ("255.255.255.255", port))
    s.close()
