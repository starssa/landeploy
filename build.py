#!/usr/bin/env python3
"""Build the LANDeploy desktop app for the current OS (PyInstaller), in a portable
and an installable form:

  Windows : LANDeploy-<v>-windows-x64-setup.exe     (installer, Inno Setup)
            LANDeploy-<v>-windows-x64-portable.zip  (unzip and run, data kept next to it)
  Linux   : landeploy_<v>_<arch>.deb                (installs to /opt/landeploy + menu entry)
            LANDeploy-<v>-linux-<arch>.AppImage     (single portable file)
  macOS   : LANDeploy-<v>-macos-<arch>.dmg          (drag to Applications)
            LANDeploy-<v>-macos-<arch>-portable.zip (run from anywhere, data kept next to it)

    pip install pyinstaller pycdlib pywebview      (pywebview: Windows/macOS only)
    python build.py linux-x64

The client agent is compiled separately into a standalone program (no Python on client PCs):

    python build.py agent linux-x64     -> dist/agent/landeploy-agent-linux-x64 (also copied to agent/bin/)

Programs found in agent/bin/ are bundled into the desktop app, which hands them out to client PCs.
"""
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
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST, BUILD = HERE / "dist", HERE / "build"
IS_WIN, IS_MAC = os.name == "nt", sys.platform == "darwin"
DESC = "Deploy operating systems and software to PCs over the local network"


def version():
    tag = os.environ.get("GITHUB_REF_NAME", "")
    if tag.startswith("v"):
        return tag[1:]
    for line in (HERE / "server.py").read_text().splitlines():
        if line.startswith("VERSION ="):
            return line.split('"')[1]
    return "0.0.0"


def run(cmd, **kw):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.check_call([str(c) for c in cmd], **kw)


# ------------------------------------------------------------------ PyInstaller
def pyinstaller():
    sep = ";" if IS_WIN else ":"
    icon = HERE / "assets" / ("icon.ico" if IS_WIN else "icon.icns" if IS_MAC else "icon.png")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
           "--name", "LANDeploy", "--icon", icon,
           "--add-data", f"web{sep}web", "--add-data", f"agent{sep}agent", "--add-data", f"assets{sep}assets",
           "--hidden-import", "server", "--hidden-import", "pycdlib"]
    if IS_WIN or IS_MAC:
        cmd += ["--hidden-import", "webview", "--collect-submodules", "webview"]
    if IS_MAC:
        cmd += ["--osx-bundle-identifier", "io.github.landeploy"]
    run(cmd + ["desktop.py"], cwd=HERE)
    if IS_MAC:
        app = DIST / "LANDeploy.app"
        return app, app / "Contents" / "MacOS" / "LANDeploy"
    folder = DIST / "LANDeploy"
    return folder, folder / ("LANDeploy.exe" if IS_WIN else "LANDeploy")


def smoke_test(exe):
    """Start the built app in server mode on a free port and query it; then check --selftest."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    data = Path(tempfile.mkdtemp())
    p = subprocess.Popen([str(exe), "--server", "--no-pxe", "--ip", "127.0.0.1", "--port", str(port),
                          "--data", str(data), "--bootfiles", str(data / "bootfiles")])
    try:
        for _ in range(90):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
                    body = json.loads(r.read())
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
                    assert b"LANDeploy" in r.read()
                print("smoke test OK: server", body["server"]["version"])
                break
            except OSError:
                time.sleep(1)
        else:
            raise SystemExit("smoke test FAILED: server did not answer")
    finally:
        p.terminate()
        p.wait(timeout=10)
    out = data / "selftest.json"
    subprocess.run([str(exe), "--selftest", str(out)], timeout=60)
    info = json.loads(out.read_text())
    print("selftest:", info)
    if (IS_WIN or IS_MAC) and info["window"] != "native":
        raise SystemExit("native window support (pywebview) missing from the build")


# ------------------------------------------------------------------ Windows
def package_windows(folder, ver, target):
    # portable zip
    port = DIST / f"LANDeploy-{ver}-{target}-portable.zip"
    (folder / "portable.txt").write_text("This copy of LANDeploy is portable: its data is kept in the "
                                         "LANDeploy-data folder next to it.\r\n")
    with zipfile.ZipFile(port, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(folder.rglob("*")):
            z.write(f, Path("LANDeploy") / f.relative_to(folder))
    (folder / "portable.txt").unlink()
    # installer
    iscc = shutil.which("iscc") or next((str(p) for p in (
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe") if p.is_file()), None)
    if not iscc:
        run(["choco", "install", "innosetup", "-y", "--no-progress"])
        iscc = str(Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe")
    iss = BUILD / "landeploy.iss"
    iss.write_text(f"""
[Setup]
AppId={{{{6B1E7C1A-5D0B-4D8E-9A51-1A2D3C4B5E6F}}
AppName=LANDeploy
AppVersion={ver}
AppPublisher=LANDeploy
AppComments={DESC}
DefaultDirName={{autopf}}\\LANDeploy
DefaultGroupName=LANDeploy
UninstallDisplayIcon={{app}}\\LANDeploy.exe
OutputDir={DIST}
OutputBaseFilename=LANDeploy-{ver}-{target}-setup
SetupIconFile={HERE / 'assets' / 'icon.ico'}
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "firewall"; Description: "Allow LANDeploy through Windows Firewall (needed for network boot)"; GroupDescription: "Network:"

[Files]
Source: "{folder}\\*"; DestDir: "{{app}}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{{group}}\\LANDeploy"; Filename: "{{app}}\\LANDeploy.exe"
Name: "{{group}}\\Uninstall LANDeploy"; Filename: "{{uninstallexe}}"
Name: "{{autodesktop}}\\LANDeploy"; Filename: "{{app}}\\LANDeploy.exe"; Tasks: desktopicon

[Run]
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""LANDeploy"" dir=in action=allow program=""{{app}}\\LANDeploy.exe"" enable=yes"; Flags: runhidden; Tasks: firewall
Filename: "{{app}}\\LANDeploy.exe"; Description: "Start LANDeploy"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""LANDeploy""\"; Flags: runhidden; RunOnceId: "fw"
""".lstrip(), encoding="utf-8")
    run([iscc, "/Q", iss])
    return [port, DIST / f"LANDeploy-{ver}-{target}-setup.exe"]


# ------------------------------------------------------------------ Linux
def package_linux(folder, ver, target):
    arch = platform.machine().lower()
    deb_arch, ai_arch = ("arm64", "aarch64") if arch in ("aarch64", "arm64") else ("amd64", "x86_64")
    desktop_entry = ("[Desktop Entry]\nType=Application\nName=LANDeploy\nComment=" + DESC +
                     "\nExec={exec}\nIcon=landeploy\nTerminal=false\nCategories=Network;System;\n"
                     "Keywords=PXE;deploy;network boot;install;\n")
    # .deb
    root = BUILD / "deb"
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(folder, root / "opt" / "landeploy")
    (root / "usr" / "bin").mkdir(parents=True)
    os.symlink("/opt/landeploy/LANDeploy", root / "usr" / "bin" / "landeploy")
    (root / "usr/share/applications").mkdir(parents=True)
    (root / "usr/share/applications/landeploy.desktop").write_text(desktop_entry.format(exec="/opt/landeploy/LANDeploy"))
    for size, src in ((256, "icon-256.png"), (512, "icon.png")):
        d = root / f"usr/share/icons/hicolor/{size}x{size}/apps"
        d.mkdir(parents=True)
        shutil.copy(HERE / "assets" / src, d / "landeploy.png")
    (root / "DEBIAN").mkdir()
    size_kb = sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) // 1024
    (root / "DEBIAN/control").write_text(
        f"Package: landeploy\nVersion: {ver}\nArchitecture: {deb_arch}\nMaintainer: LANDeploy\n"
        f"Installed-Size: {size_kb}\nSection: net\nPriority: optional\nRecommends: pkexec | policykit-1\n"
        f"Description: {DESC}\n Network-boot PCs into OS installers (ISO) and push software to them,\n"
        f" from a desktop app with a web console.\n")
    deb = DIST / f"landeploy_{ver}_{deb_arch}.deb"
    run(["dpkg-deb", "--root-owner-group", "--build", root, deb])
    # AppImage
    appdir = BUILD / "LANDeploy.AppDir"
    shutil.rmtree(appdir, ignore_errors=True)
    shutil.copytree(folder, appdir / "usr" / "lib" / "landeploy")
    (appdir / "AppRun").write_text('#!/bin/sh\nHERE="$(dirname "$(readlink -f "$0")")"\n'
                                   'exec "$HERE/usr/lib/landeploy/LANDeploy" "$@"\n')
    (appdir / "AppRun").chmod(0o755)
    (appdir / "landeploy.desktop").write_text(desktop_entry.format(exec="LANDeploy"))
    shutil.copy(HERE / "assets" / "icon-256.png", appdir / "landeploy.png")
    tool = BUILD / f"appimagetool-{ai_arch}.AppImage"
    if not tool.exists():
        url = f"https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-{ai_arch}.AppImage"
        print("downloading", url)
        urllib.request.urlretrieve(url, tool)
        tool.chmod(0o755)
    image = DIST / f"LANDeploy-{ver}-{target}.AppImage"
    run([tool, "--appimage-extract-and-run", appdir, image],
        env={**os.environ, "ARCH": ai_arch, "APPIMAGE_EXTRACT_AND_RUN": "1"})
    return [deb, image]


# ------------------------------------------------------------------ macOS
def package_mac(app, ver, target):
    port = DIST / f"LANDeploy-{ver}-{target}-portable.zip"
    run(["ditto", "-c", "-k", "--keepParent", app, port])
    stage = BUILD / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    run(["ditto", app, stage / "LANDeploy.app"])
    os.symlink("/Applications", stage / "Applications")
    dmg = DIST / f"LANDeploy-{ver}-{target}.dmg"
    run(["hdiutil", "create", "-volname", "LANDeploy", "-srcfolder", stage, "-ov", "-format", "UDZO", dmg])
    return [port, dmg]


def build_agent(target):
    name = f"landeploy-agent-{target}"
    out = DIST / "agent"
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--name", name,
           "--distpath", out, "--workpath", BUILD / "agent", "--specpath", BUILD]
    if IS_WIN:
        cmd += ["--noconsole", "--icon", HERE / "assets" / "icon.ico"]
    run(cmd + [HERE / "agent" / "landeploy_agent.py"], cwd=HERE)
    exe = out / (name + (".exe" if IS_WIN else ""))
    # smoke test: one poll against a server that isn't there must exit cleanly
    r = subprocess.run([str(exe), "--server", "http://127.0.0.1:9", "--token", "x", "--once"], timeout=120)
    if r.returncode != 0:
        raise SystemExit(f"agent smoke test failed (exit {r.returncode})")
    print(f"agent OK: {exe.name} ({exe.stat().st_size >> 20} MB)")
    bin_dir = HERE / "agent" / "bin"
    bin_dir.mkdir(exist_ok=True)
    shutil.copy(exe, bin_dir / exe.name)


def main():
    if sys.argv[1:2] == ["agent"]:
        target = sys.argv[2] if len(sys.argv) > 2 else ("windows-x64" if IS_WIN else f"linux-{'arm64' if platform.machine().lower() in ('aarch64', 'arm64') else 'x64'}")
        DIST.mkdir(exist_ok=True)
        BUILD.mkdir(exist_ok=True)
        return build_agent(target)
    target = sys.argv[1] if len(sys.argv) > 1 else f"{sys.platform}-{platform.machine().lower()}"
    ver = version()
    shutil.rmtree(DIST, ignore_errors=True)
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir()
    bundled = sorted(p.name for p in (HERE / "agent" / "bin").glob("landeploy-agent-*")) if (HERE / "agent" / "bin").is_dir() else []
    print("bundling client agents:", bundled or "NONE (client setup will not work)")
    bundle, exe = pyinstaller()
    smoke_test(exe)
    if IS_WIN:
        out = package_windows(bundle, ver, target)
    elif IS_MAC:
        out = package_mac(bundle, ver, target)
    else:
        out = package_linux(bundle, ver, target)
    release = DIST / "release"
    release.mkdir()
    for f in out:
        shutil.move(str(f), release / f.name)
        print(f"built {f.name} ({(release / f.name).stat().st_size >> 20} MB)")


if __name__ == "__main__":
    main()
