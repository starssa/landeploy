#!/usr/bin/env python3
"""Build a standalone LANDeploy server for the current OS with PyInstaller.

    pip install pyinstaller pycdlib
    python build.py linux-x64        # -> dist/LANDeploy-<version>-linux-x64.tar.gz

The archive contains the executable, start script, README and the agent script.
No Python is needed on the machine that runs it.
"""
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
IS_WIN = os.name == "nt"


def version():
    tag = os.environ.get("GITHUB_REF_NAME", "")
    if tag.startswith("v"):
        return tag[1:]
    for line in (HERE / "server.py").read_text().splitlines():
        if line.startswith("VERSION ="):
            return line.split('"')[1]
    return "dev"


def smoke_test(exe):
    """Start the built server on a free port (no PXE, no root needed) and query its API."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    data = HERE / "build" / "smoke-data"
    shutil.rmtree(data, ignore_errors=True)
    p = subprocess.Popen([str(exe), "--no-pxe", "--ip", "127.0.0.1", "--port", str(port), "--data", str(data)])
    try:
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
                    body = r.read()
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
                    page = r.read()
                assert b'"version"' in body and b"LANDeploy" in page
                print("smoke test OK")
                return
            except OSError:
                time.sleep(1)
        raise SystemExit("smoke test FAILED: server did not answer")
    finally:
        p.terminate()
        p.wait(timeout=10)


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else f"{sys.platform}-{os.uname().machine if not IS_WIN else 'x64'}"
    ver = version()
    name = f"LANDeploy-{ver}-{target}"
    sep = ";" if IS_WIN else ":"
    for d in ("build", "dist"):
        shutil.rmtree(HERE / d, ignore_errors=True)

    subprocess.check_call([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--name", "landeploy-server",
        "--add-data", f"web{sep}web", "--add-data", f"agent{sep}agent",
        "--hidden-import", "pycdlib",
        "server.py"], cwd=HERE)

    exe = HERE / "dist" / ("landeploy-server.exe" if IS_WIN else "landeploy-server")
    smoke_test(exe)

    out = HERE / "dist" / name
    out.mkdir()
    shutil.copy(exe, out)
    shutil.copy(HERE / "README.md", out)
    (out / "agent").mkdir()
    shutil.copy(HERE / "agent" / "landeploy_agent.py", out / "agent")
    (out / "bootfiles").mkdir()
    (out / "data" / "isos").mkdir(parents=True)
    if IS_WIN:
        (out / "Start LANDeploy.cmd").write_text(
            "@echo off\r\n"
            "rem Needs administrator rights for the network boot ports (67, 69, 4011)\r\n"
            "net session >nul 2>&1 || (powershell -NoProfile -Command \"Start-Process -Verb RunAs -FilePath '%~f0'\" & exit /b)\r\n"
            "cd /d \"%~dp0\"\r\n"
            "start \"\" http://localhost:8080\r\n"
            "landeploy-server.exe %*\r\n"
            "pause\r\n")
    else:
        start = out / "start.sh"
        start.write_text(
            "#!/bin/sh\n"
            "# Needs root for the network boot ports (67, 69, 4011)\n"
            "cd \"$(dirname \"$0\")\"\n"
            "[ \"$(id -u)\" = 0 ] || exec sudo \"$0\" \"$@\"\n"
            "exec ./landeploy-server \"$@\"\n")
        start.chmod(0o755)
        (out / "landeploy-server").chmod(0o755)

    if IS_WIN:
        archive = HERE / "dist" / f"{name}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(out.rglob("*")):
                z.write(f, f.relative_to(out.parent))
    else:
        archive = HERE / "dist" / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as t:
            t.add(out, arcname=name)
    print("built", archive, f"({archive.stat().st_size >> 20} MB)")


if __name__ == "__main__":
    main()
