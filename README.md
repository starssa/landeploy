# LANDeploy

Deploy operating systems (from plain ISO files) and software to many PCs on your local network from one server, using a web wizard.

- **OS deployment**: works like netboot.xyz plus WDS. A PC boots from the network and gets a menu, or it starts the installer you scheduled for it. The installer can start automatically or wait for someone to press Y on that PC.
- **Software deployment**: works like SmartDeploy's Application Packs. A small agent on each PC receives packages. By default the user sees a dialog that asks for permission before anything is installed.
- **Many PCs at once**: tick several computers, pick an OS or package, and click Deploy. Every PC boots and installs at the same time. Optional Wake-on-LAN can turn them on for you.

## Download

Get the latest version from the **[Releases page](../../releases/latest)**. Every system has an **installable** and a **portable** version, and none of them needs Python:

| System | Install it | Portable (no install) |
|---|---|---|
| Windows 10/11 | `LANDeploy-…-windows-x64-setup.exe` | `LANDeploy-…-windows-x64-portable.zip` → unzip → `LANDeploy.exe` |
| Ubuntu / Debian | `landeploy_…_amd64.deb` (double-click, or `sudo apt install ./landeploy_*.deb`) | `LANDeploy-…-linux-x64.AppImage` (`chmod +x`, then double-click) |
| Mac | `LANDeploy-…-macos-arm64.dmg` / `-x64.dmg` → drag to Applications | `LANDeploy-…-macos-*-portable.zip` |

**Start LANDeploy** like any other app (Start menu, app grid or Launchpad). It:
1. asks for your administrator password, because network boot needs ports 67/69/4011. If you decline, software deployment still works and network boot stays off.
2. opens the console in its own window: native on Windows/macOS; on Linux a Chrome/Chromium/Edge app window if one is installed, otherwise your browser.
3. stops the server when you close the window.

**Where data goes** (ISOs, packages, settings):
- installed: `C:\ProgramData\LANDeploy`, `/Library/Application Support/LANDeploy`, `/var/lib/landeploy`
- portable: a `LANDeploy-data` folder next to the app, so the whole thing can live on a USB drive.

**Headless / server use:** `LANDeploy --server` (or `landeploy --server` after installing the .deb) runs without a window, with the same options as `server.py`.

The apps are not code-signed. On Windows, SmartScreen → *More info → Run anyway*. On Mac, right-click the app → *Open* the first time.

Running from source instead needs only Python 3.8+. Optionally install 7-Zip or `pycdlib` to extract ISOs.

## Making a release (maintainers)

Change `VERSION = "1.0.1"` in `server.py`, commit and push to `main`. The tag `v1.0.1` is created for you; pushing a tag yourself also works.
GitHub Actions (`.github/workflows/release.yml`) then compiles the client agents, bundles them into the desktop app for Windows x64, Linux x64/ARM64 and macOS ARM64/x64 (PyInstaller), smoke-tests each build, and publishes them with SHA256 checksums as a GitHub Release. To build locally: `pip install pyinstaller pycdlib && python build.py`.

---

## How it works

```
  Client PC (power on, F12 → Network boot)
      │ 1. DHCP DISCOVER ("PXEClient", arch=BIOS/UEFI)
      ▼
  Router DHCP ─────────── gives the IP address (unchanged)
  LANDeploy ProxyDHCP ─── "boot file = snponly.efi / undionly.kpxe, server = me"
      │ 2. TFTP download of the iPXE binary (≈100 KB)
      ▼
  iPXE starts, DHCPs again → ProxyDHCP now answers "http://server:8080/boot.ipxe"
      │ 3. HTTP: /menu.ipxe?mac=…  ← the server decides, per MAC address:
      │       • install scheduled → countdown or "Press Y" → install
      │       • nothing scheduled  → menu (timeout → boot local disk)
      ▼
  4. Boot script for the chosen ISO, everything over HTTP:
       Windows  : wimboot + boot.wim; WinPE maps \\server\landeploy, runs setup.exe [/unattend]
       Ubuntu   : casper kernel + url=…/file.iso   [+ autoinstall]
       Fedora   : inst.repo=… or root=live:…       [+ kickstart]
       Arch     : archiso_http_srv=…
       other    : sanboot …/file.iso (raw ISO)
```

The schedule for each PC is **one-shot**. As soon as the installer starts, the assignment is cleared, so when the PC reboots after the install it boots its own disk instead of reinstalling.

Software path: `agent → poll /api/agent/jobs → consent dialog → download (SHA-256 checked) → silent install → report`.

| Port | Proto | Used for |
|---|---|---|
| 67, 4011 | UDP | ProxyDHCP (answers PXE clients only, never assigns IPs) |
| 69 | UDP | TFTP (iPXE binaries) |
| 8080 | TCP | Web console, iPXE scripts, ISO/kernel files, agent API |
| 4012 | UDP | Agent auto-discovery |
| 445 | TCP | SMB share (Windows installs only) |

---

## Quick start (Linux server, e.g. Arch)

```bash
sudo pacman -S python p7zip ipxe samba        # or: pip install pycdlib instead of p7zip
python3 fetch_bootfiles.py                     # puts iPXE + wimboot into ./bootfiles
cp ~/Downloads/*.iso data/isos/                # or drag-and-drop them in the web console
sudo python3 server.py                         # root is needed for ports 67/69
# open http://<server-ip>:8080
```
Open the firewall ports listed above, for example `sudo ufw allow 67,69,4011,4012/udp && sudo ufw allow 8080,445/tcp`.

**Windows server**: install Python, run `python fetch_bootfiles.py`, then `python server.py` from an *administrator* terminal, and allow Python through the Windows Firewall. Don't run it on a machine that is already a DHCP server.

### Windows ISOs (extra step)
Windows Setup reads its files from this server over a shared folder (SMB). Windows 11 24H2 and later refuse guest (password-less) shares, so create a share user:
```bash
sudo apt install samba
sudo useradd -M -s /usr/sbin/nologin landeploy && sudo smbpasswd -a landeploy
```
Add the `[landeploy]` section shown under **iPXE network boot → Shared folder → Show setup** to `/etc/samba/smb.conf`. Then run `sudo systemctl restart smbd && sudo ufw allow samba`, and enter the same user and password in **Settings → Windows SMB share**.

If a PC stops at "No connection to the LANDeploy server" with no IPv4 address, the Windows installer lacks its network-card driver. Add the driver files under **iPXE network boot → Network drivers for the Windows installer**. For Intel I225/I226 cards, take `e2fn` and `e2f` from the `PRO2500\Winx64` folders of Intel's Complete Driver Pack.

### Client PCs
In BIOS/UEFI, enable **Network/PXE boot** (often called "Network Stack" in UEFI). Then either put it first in the boot order, or press F12 (or F9/F11/Esc, depending on the brand) once.

Secure Boot: the stock iPXE binaries aren't Microsoft-signed, so turn Secure Boot off on the clients during deployment, or build/sign your own iPXE.

---

## Using it

Console menu: **Overview** · **Computers** · *OS install*: OS library, Windows install config, Post-install, iPXE network boot · *Software install*: Packages, Install the agent, Command generator · Activity · Settings.

1. **OS library**: drop ISOs, official or custom (Tiny11, your own drivers or apps…). By default each ISO is **mounted**: LANDeploy reads it in place, with no copy and no extra disk space. Replace the `.iso` file, click **Rescan**, and the new version is used. Mounting needs admin rights. Without them, or if you choose **Extracting** at the top of the page, the files are copied instead. Each ISO can be switched in **Options → Storage**. After a restart, mounted ISOs are mounted again automatically.
    Its type is detected automatically. Use **Options** to rename it, add kernel arguments, or paste an *unattended answer file* (autounattend.xml / Ubuntu autoinstall / kickstart). Templates are included, and they keep disk selection interactive on purpose.
2. **Windows install config** (one page for every Windows ISO): language, keyboard, time zone, serial (none, a generic key that only picks the edition, or your own key), plus user accounts built from a **prefix + suffix** (`Admin` + `1, 2…` → Admin1, Admin2; `Eleve-` + `a, b…` → Eleve-a, Eleve-b…). Each row is admin or standard, with one shared password or one per row. Computer names come from the Computers list or a pattern (`Salle-Info-` + `01, 02…`; each PC keeps its number when reinstalled). Options to skip the Microsoft-account and privacy screens, to allow PCs without TPM 2.0 or Secure Boot, and to erase disk 0 for a fully hands-off install. Each PC gets its own answer file when it starts installing; **Preview answer file** shows it. An ISO can still use its own XML or none (OS library → Options → Answer file).
3. **Post-install** (Windows): steps that run automatically on each PC right after Windows is installed, before anyone logs in. The LANDeploy agent is installed first, then the packages you pick, in your order (e.g. MS Office as a `.zip` package, Chrome, GeoGebra), then an optional PowerShell script. Nothing asks for confirmation. The answer file adds a start-up task that fetches the agent from this server and removes itself once done; progress shows in Packages → Deployments as *(post-install)*. Requires Windows install config to be on.
4. **Computers** shows a live status for every PC: 🟣 **network-booting** (with a pop-up notice), 🔵 **installing**, 🟢 **on** (the agent reports, the PC answers ping, or it just asked the network for an IP address), ⚫ **off**. Some PCs, Windows in particular, block ping by default; for those only the agent or the start-up address request shows they're on. Remove a PC from the list with 🗑 (or tick several → **Remove selected**); its scheduled install and pending software are cancelled.
    PCs appear after their first network boot, or when the agent registers. You can also add PCs by MAC address.
5. **Deploy OS…**: tick **one or many** PCs → choose OS → *Ask on screen* (safe) or *Start automatically* (+ Wake-on-LAN) → Schedule. Then power on or network-boot them. They all install in parallel.
6. **Software**: New package → upload an `.msi/.exe/.ps1/.deb/.rpm/.sh` with silent switches (presets included), *or* enter a command like `winget install -e --id 7zip.7zip --silent` / `apt-get install -y vlc`. Then **Deploy…** to any number of agents, or use *Install software on selected* from Computers. Status for each PC shows up live: pending → asking → running → success / failed / denied.

### Installing the agent (console → Software install → **Install the agent**)
Client PCs need **no Python and no Internet**. LANDeploy includes the agent as a small standalone program for Windows x64, Linux x64 and Linux ARM64, and hands it out over the LAN:

- **Windows PCs**: download **LANDeploy-Agent-Setup.cmd** from the console and choose *Run as administrator*, or paste the one-line command (📋 Copy button) into an admin PowerShell. The agent goes into `C:\ProgramData\LANDeploy-Agent` and a SYSTEM scheduled task starts it at every boot.
- **Linux PCs**: paste `wget -qO- "http://SERVER:8080/setup.sh?t=TOKEN" | sudo sh`. The agent goes into `/opt/landeploy-agent` and runs as the `landeploy-agent` systemd service; zenity is installed for the consent prompt.

The setup URLs only work with the agent token, which the console fills in for you. The **Command generator** page builds winget/apt/snap/flatpak commands and silent switches, and can turn them into a package in one click. Re-running the setup updates an existing agent, including the older Python-based one.

The agent programs are also attached to each release (`landeploy-agent-*`). Running LANDeploy from source? Build the agent for your OS with `pip install pyinstaller && python build.py agent`.

---

## Deploying to many PCs: what to expect
- Each PC downloads over plain HTTP. 10–20 PCs at once is fine on gigabit. With more, Ubuntu's `url=` mode (each PC pulls the whole ISO into RAM) saturates the link, so stagger them or switch to a 2.5/10 GbE server port. There's no multicast.
- Ubuntu's live `url=` boot needs RAM ≥ about 2× the ISO size (8 GB+ for desktop ISOs).
- For a truly hands-off run on a whole room, combine *Start automatically* + Wake-on-LAN + an unattended answer file **with disk configuration**. That erases the disks, so double-check which PCs are ticked.

## Using your own DHCP/dnsmasq instead
Run `python3 server.py --no-pxe` and point your DHCP server at it. Example for dnsmasq:
```
dhcp-match=set:ipxe,175
dhcp-boot=tag:ipxe,http://SERVER:8080/boot.ipxe
dhcp-match=set:efi64,option:client-arch,7
dhcp-boot=tag:efi64,tag:!ipxe,snponly.efi,,SERVER
dhcp-boot=tag:!efi64,tag:!ipxe,undionly.kpxe,,SERVER
```

## Security notes
- Set a **console password** (Settings) if other people share the LAN. Agents must present the **agent token**. Package downloads are verified with SHA-256.
- Everything is plain HTTP on the LAN. Don't expose port 8080 to the Internet.

## Files
`desktop.py` desktop app (window, admin prompt, portable/installed data) · `server.py` web console + API + iPXE generation · `tools.py` tool downloads + agent setup scripts · `pxe.py` ProxyDHCP/TFTP/discovery/WoL · `library.py` ISO extraction, detection, boot scripts · `web/index.html` the GUI · `agent/landeploy_agent.py` client agent · `fetch_bootfiles.py` gets iPXE/wimboot · `build.py` + `.github/workflows/release.yml` build the releases · `data/` ISOs, extracted trees, packages, `state.json`.
