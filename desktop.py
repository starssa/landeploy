#!/usr/bin/env python3
"""LANDeploy desktop app.

Double-click -> starts the LANDeploy server (asking for administrator rights,
needed for network boot) and shows the console in its own window.

    LANDeploy                 desktop app (window)
    LANDeploy --server ...    headless server, same options as server.py
    LANDeploy --selftest [f]  print (or write to f) what the desktop app would do, then exit

Portable vs installed:
  * installed (Program Files, /Applications, /opt):  data in a system folder
      Windows %ProgramData%\\LANDeploy  macOS /Library/Application Support/LANDeploy  Linux /var/lib/landeploy
  * portable (zip / AppImage / anywhere else):       data in "LANDeploy-data" next to the app
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = "LANDeploy"
IS_WIN, IS_MAC = os.name == "nt", sys.platform == "darwin"
FROZEN = getattr(sys, "frozen", False)
PORT = 8080

# PyInstaller and the AppImage runtime prepend their own bundled libraries to the environment.
# System programs we launch (zenity, the browser, xdg-open) must NOT inherit those, or they load a
# mismatched mix of our bundled libs and the system libs and crash (e.g. the libpango/fontconfig
# "undefined symbol: FcConfigSetDefaultSubstitute" error). Restore the pre-launch values.
_HOST_ENV_VARS = ("LD_LIBRARY_PATH", "LD_PRELOAD", "GTK_PATH", "GTK_EXE_PREFIX", "GTK_DATA_PREFIX",
                  "GDK_PIXBUF_MODULE_FILE", "GDK_PIXBUF_MODULEDIR", "GIO_MODULE_DIR", "GI_TYPELIB_PATH",
                  "GSETTINGS_SCHEMA_DIR", "FONTCONFIG_FILE", "FONTCONFIG_PATH", "GCONV_PATH",
                  "QT_PLUGIN_PATH", "XDG_DATA_DIRS", "PYTHONHOME", "PYTHONPATH")


def host_env():
    env = dict(os.environ)
    for var in _HOST_ENV_VARS:
        orig = env.pop(var + "_ORIG", None)      # PyInstaller saves the original here
        if orig:
            env[var] = orig
        else:
            env.pop(var, None)                   # AppImage sets it with no _ORIG: drop it entirely
    return env


def open_url(url):
    """Open the console in the user's default browser, with a clean environment."""
    for opener in (["xdg-open", url], ["gio", "open", url]):
        if shutil.which(opener[0]):
            try:
                subprocess.Popen(opener, env=host_env(),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except Exception:
                pass
    import webbrowser
    webbrowser.open(url)


# ------------------------------------------------------------------ locations
def exe_path():
    return Path(sys.executable if FROZEN else __file__).resolve()


def app_container():
    """The thing the user sees and moves around: folder with the .exe, the .app bundle, or the .AppImage file."""
    if os.environ.get("APPIMAGE"):
        return Path(os.environ["APPIMAGE"])
    p = exe_path()
    if IS_MAC:
        for parent in p.parents:
            if parent.suffix == ".app":
                return parent
    return p.parent


def is_portable():
    if os.environ.get("LANDEPLOY_PORTABLE") or os.environ.get("APPIMAGE"):
        return True
    c = app_container()
    if (c / "portable.txt").exists() or (exe_path().parent / "portable.txt").exists():
        return True
    s = str(c).lower()
    if IS_WIN:
        pf = [os.environ.get(k, "").lower() for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
        return not any(x and s.startswith(x) for x in pf)
    if IS_MAC:
        return not s.startswith("/applications/")
    return not (s.startswith("/opt/") or s.startswith("/usr/"))


_DATA_OVERRIDE = None


def user_data_dir():
    """Per-user folder, used when an installed copy runs without administrator rights."""
    if IS_WIN:
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP
    if IS_MAC:
        return Path.home() / "Library/Application Support" / APP
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "landeploy"


def data_dir():
    if _DATA_OVERRIDE:
        return _DATA_OVERRIDE
    if is_portable():
        c = app_container()
        base = c.parent if (c.is_file() or c.suffix == ".app") else c
        return base / "LANDeploy-data"
    if IS_WIN:
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / APP
    if IS_MAC:
        return Path("/Library/Application Support") / APP
    return Path("/var/lib/landeploy")


def server_command(extra):
    """Command line that starts this same program in server mode."""
    if os.environ.get("APPIMAGE"):
        # root can't read the user's FUSE mount: let the AppImage runtime extract itself instead
        return [os.environ["APPIMAGE"], "--appimage-extract-and-run", "--server"] + extra
    if FROZEN:
        return [str(exe_path()), "--server"] + extra
    return [sys.executable, str(exe_path()), "--server"] + extra


# ------------------------------------------------------------------ server process
def port_answers(port, timeout=1.0):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=timeout) as r:
            return b'"server"' in r.read()
    except Exception:
        return False


def port_busy(port):
    s = socket.socket()
    try:
        s.bind(("", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def start_server(port, elevated=True):
    """Start the server as a separate process. Returns a Popen (or None when started through an
    elevation helper that we can't track; the server then watches our PID and exits with us)."""
    d = data_dir()
    args = ["--data", str(d), "--bootfiles", str(d / "bootfiles"), "--port", str(port),
            "--log", str(d / "landeploy.log"), "--parent-pid", str(os.getpid())]
    if not elevated:
        args.append("--no-pxe")
    cmd = server_command(args)
    if IS_WIN:
        if elevated and not _win_is_admin():
            import ctypes
            r = ctypes.windll.shell32.ShellExecuteW(None, "runas", cmd[0], subprocess.list2cmdline(cmd[1:]),
                                                    str(exe_path().parent), 0)
            return None if r > 32 else False                            # False = user said no
        return subprocess.Popen(cmd, creationflags=0x08000000)          # CREATE_NO_WINDOW
    if elevated and os.geteuid() != 0:
        if IS_MAC:
            line = " ".join(_sh_quote(x) for x in cmd) + " >/dev/null 2>&1 &"
            script = f'do shell script "{_as_quote(line)}" with administrator privileges with prompt "LANDeploy needs administrator rights to network-boot PCs."'
            r = subprocess.run(["osascript", "-e", script], capture_output=True)
            return None if r.returncode == 0 else False
        if shutil.which("pkexec"):
            return subprocess.Popen(["pkexec"] + cmd)
        return False
    return subprocess.Popen(cmd)


def _win_is_admin():
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _sh_quote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def _as_quote(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def ensure_server(port):
    """Start (or reuse) the server; returns (url, process, note)."""
    url = f"http://127.0.0.1:{port}/"
    if port_answers(port):
        return url, None, "already running"
    if port_busy(port):
        return None, None, f"Port {port} is used by another program."
    d = data_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        pass                                    # the elevated server creates it
    proc, note = start_server(port, elevated=True), ""
    if proc is False:                           # elevation refused/unavailable: run without network boot
        note = "Running without administrator rights: network boot is off, software deployment works."
        _fallback_to_user_dir()
        proc = start_server(port, elevated=False)
    for _ in range(120):                        # up to ~60 s (password prompt, first start)
        if port_answers(port, 0.5):
            return url, proc, note
        if proc and proc.poll() is not None and proc.returncode not in (None, 0):
            if proc.returncode in (126, 127):   # pkexec: dismissed / not authorized
                _fallback_to_user_dir()
                proc = start_server(port, elevated=False)
                note = "Running without administrator rights: network boot is off, software deployment works."
                continue
            break
        time.sleep(0.5)
    return None, proc, f"The server did not start. See the log: {d / 'landeploy.log'}"


def _fallback_to_user_dir():
    global _DATA_OVERRIDE
    d = data_dir()
    probe = d if d.exists() else d.parent
    if not os.access(probe, os.W_OK):           # system folder not writable without admin rights
        _DATA_OVERRIDE = user_data_dir()
        _DATA_OVERRIDE.mkdir(parents=True, exist_ok=True)


def stop_server(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    # an elevated server we can't signal exits on its own (it watches our PID)


# ------------------------------------------------------------------ windows
def window_backend():
    if IS_WIN or IS_MAC:
        try:
            import webview  # noqa: F401
            return "native"
        except Exception:
            pass
    return "browser-app" if find_app_browser() else "browser"


def find_app_browser():
    cands = []
    if IS_WIN:
        for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""), os.environ.get("LOCALAPPDATA", "")):
            cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe", Path(base) / "Google/Chrome/Application/chrome.exe"]
        return next((str(c) for c in cands if c.is_file()), None)
    if IS_MAC:
        for name in ("Google Chrome", "Microsoft Edge", "Chromium", "Brave Browser"):
            p = Path(f"/Applications/{name}.app/Contents/MacOS/{name}")
            if p.is_file():
                return str(p)
        return None
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
                 "microsoft-edge-stable", "brave-browser"):
        if shutil.which(name):
            return shutil.which(name)
    return None


def show_native(url):
    import webview
    webview.create_window(f"{APP}", url, width=1300, height=850, min_size=(900, 600))
    webview.start()


def show_browser_app(url, browser):
    profile = Path.home() / ".cache" / "landeploy-window"
    p = subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                          "--no-default-browser-check", "--window-size=1300,850"], env=host_env())
    p.wait()                                     # separate profile => this process lives as long as the window


def show_control_window(url, note):
    """Keep LANDeploy running and give a reliable way to stop it, without needing a terminal."""
    open_url(url)
    try:
        import tkinter as tk
    except Exception:
        tk = None
    if tk is not None:
        root = tk.Tk()
        root.title(APP)
        root.geometry("440x180")
        tk.Label(root, text=f"{APP} is running", font=("Sans", 14, "bold")).pack(pady=(16, 4))
        tk.Label(root, text=url, fg="#2563eb").pack()
        if note:
            tk.Label(root, text=note, wraplength=400, fg="#92400e").pack(pady=4)
        row = tk.Frame(root)
        row.pack(pady=10)
        tk.Button(row, text="Open console", command=lambda: open_url(url)).pack(side="left", padx=6)
        tk.Button(row, text="Stop LANDeploy", command=root.destroy).pack(side="left", padx=6)
        root.mainloop()
        return
    if shutil.which("zenity"):                   # no tkinter (minimal build): a plain GTK dialog still works
        text = f"{APP} is running.\n\n{url}\n\n{note}\n\nClose this window (or click OK) to stop LANDeploy."
        subprocess.run(["zenity", "--info", "--title", APP, "--width", "420",
                        "--ok-label", "Stop LANDeploy", "--text", text], env=host_env())
        return
    print(f"{APP} is running: {url}")
    print("Close this terminal or press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return


def error_box(msg):
    if IS_WIN:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, APP, 0x10)
    elif IS_MAC:
        subprocess.run(["osascript", "-e", f'display alert "{APP}" message "{_as_quote(msg)}" as critical'])
    elif shutil.which("zenity"):
        subprocess.run(["zenity", "--error", "--title", APP, "--text", msg], env=host_env())
    else:
        print(msg, file=sys.stderr)


# ------------------------------------------------------------------ main
def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--server"]:                # headless: same data folder as the desktop window
        import server
        args = argv[1:]
        if not any(x == "--data" or x.startswith("--data=") for x in args):
            args += ["--data", str(data_dir())]
        if not any(x == "--bootfiles" or x.startswith("--bootfiles=") for x in args):
            d = next((args[i + 1] for i, x in enumerate(args[:-1]) if x == "--data"), str(data_dir()))
            args += ["--bootfiles", str(Path(d) / "bootfiles")]
        return server.main(args)
    if argv[:1] == ["--selftest"]:              # optional output file: windowed builds have no console
        info = json.dumps({"portable": is_portable(), "data_dir": str(data_dir()), "window": window_backend(),
                           "server_cmd": server_command([])}, indent=1)
        if len(argv) > 1:
            Path(argv[1]).write_text(info)
        elif sys.stdout:
            print(info)
        return
    port = int(os.environ.get("LANDEPLOY_PORT", PORT))
    url, proc, note = ensure_server(port)
    if not url:
        error_box(note)
        return stop_server(proc)
    try:
        backend = window_backend()
        if backend == "native":
            try:
                show_native(url)
            except Exception:                    # e.g. WebView2 runtime missing
                b = find_app_browser()
                show_browser_app(url, b) if b else show_control_window(url, note)
        elif backend == "browser-app":
            show_browser_app(url, find_app_browser())
        else:
            show_control_window(url, note)
    finally:
        stop_server(proc)


if __name__ == "__main__":
    main()
