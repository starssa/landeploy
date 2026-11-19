# LANDeploy

Deploy operating systems (from plain ISO files) and software to many PCs on your local network from one server, using a web wizard.

- **OS deployment**: works like netboot.xyz plus WDS. A PC boots from the network and gets a menu, or it starts the installer you scheduled for it. The installer can start automatically or wait for someone to press Y on that PC.
- **Software deployment**: works like SmartDeploy's Application Packs. A small agent on each PC receives packages. By default the user sees a dialog that asks for permission before anything is installed.
- **Many PCs at once**: tick several computers, pick an OS or package, and click Deploy. Every PC boots and installs at the same time. Optional Wake-on-LAN can turn them on for you.

## Download

Ready-to-run builds are on the **[Releases page](../../releases/latest)**. You don't need Python:

| System | File | Start |
|---|---|---|
| Windows 10/11 | `LANDeploy-…-windows-x64.zip` | unzip → double-click **Start LANDeploy.cmd** |
| Linux x64 / ARM64 | `LANDeploy-…-linux-x64.tar.gz` | `tar xzf LANDeploy-*.tar.gz && cd LANDeploy-* && ./start.sh` |
| macOS (Apple Silicon / Intel) | `LANDeploy-…-macos-arm64.tar.gz` / `-macos-x64` | same as Linux; first run: `xattr -dr com.apple.quarantine .` |

Then open http://localhost:8080. The start scripts ask for administrator/root rights because network boot uses ports 67, 69 and 4011.

Running from source instead needs only Python 3.8+. Optionally install 7-Zip or `pycdlib` to extract ISOs.

## Making a release (maintainers)

```bash
git tag v1.0.1 && git push origin v1.0.1
```
GitHub Actions (`.github/workflows/release.yml`) then builds Windows x64, Linux x64/ARM64 and macOS ARM64/x64 with PyInstaller, smoke-tests each build, and publishes them with SHA256 checksums as a GitHub Release. To build locally: `pip install pyinstaller pycdlib && python build.py`.

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
WinPE has to read the full installation files over SMB. Go to **Overview → "show config"** to get a ready-made `smb.conf` block. On Linux, add it to `/etc/samba/smb.conf` and restart `smb`. On a Windows server, share the `data` folder as `landeploy` with read access. Recent Windows Setup builds may refuse guest SMB access. In that case, create a user, share with that user, and enter it under Settings → SMB.

### Client PCs
In BIOS/UEFI, enable **Network/PXE boot** (often called "Network Stack" in UEFI). Then either put it first in the boot order, or press F12 (or F9/F11/Esc, depending on the brand) once.

Secure Boot: the stock iPXE binaries aren't Microsoft-signed, so turn Secure Boot off on the clients during deployment, or build/sign your own iPXE.

---

## Using it

1. **OS library**: drop ISOs. Each one is extracted and its type is detected automatically. Use **Options** to rename it, add kernel arguments, or paste an *unattended answer file* (autounattend.xml / Ubuntu autoinstall / kickstart). Templates are included, and they keep disk selection interactive on purpose.
2. **Computers**: PCs appear after their first network boot, or when the agent registers. You can also add PCs by MAC address.
3. **Deploy OS…**: tick **one or many** PCs → choose OS → *Ask on screen* (safe) or *Start automatically* (+ Wake-on-LAN) → Schedule. Then power on or network-boot them. They all install in parallel.
4. **Software**: New package → upload an `.msi/.exe/.ps1/.deb/.rpm/.sh` with silent switches (presets included), *or* enter a command like `winget install -e --id 7zip.7zip --silent` / `apt-get install -y vlc`. Then **Deploy…** to any number of agents, or use *Install software on selected* from Computers. Status for each PC shows up live: pending → asking → running → success / failed / denied.

### Installing the agent (console → **Agents & tools**)
1. Click **⬇ Download** next to *iPXE boot files* and *Python for Windows (agent runtime)*. The server keeps them, so client PCs never need Internet access. If the server is offline, upload the files instead.
2. On each **Windows** PC, download **LANDeploy-Agent-Setup.cmd** from the console and choose *Run as administrator*, or paste the one-line command (with a 📋 Copy button) into an admin PowerShell. Portable Python and the agent go into `C:\ProgramData\LANDeploy`, and a SYSTEM scheduled task starts the agent at every boot.
3. On each **Linux** PC, paste `wget -qO- "http://SERVER:8080/setup.sh?t=TOKEN" | sudo sh`. This installs a systemd service plus zenity.

The setup URLs only work with the agent token, which the console fills in for you. The **command generator** on the same page builds winget/apt/snap/flatpak commands and silent switches, and can turn them into a package in one click.

To fetch Python from a local mirror instead of python.org, start the server with `LANDEPLOY_PYTHON_MIRROR=http://mirror/python/`.

If nobody answers the consent dialog within 5 minutes, the install is **denied**. Packages marked *silent* skip the dialog.

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
`server.py` web console + API + iPXE generation · `tools.py` tool downloads + agent setup scripts · `pxe.py` ProxyDHCP/TFTP/discovery/WoL · `library.py` ISO extraction, detection, boot scripts · `web/index.html` the GUI · `agent/landeploy_agent.py` client agent · `fetch_bootfiles.py` gets iPXE/wimboot · `build.py` + `.github/workflows/release.yml` build the releases · `data/` ISOs, extracted trees, packages, `state.json`.
