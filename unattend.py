"""Builds a Windows answer file (autounattend.xml) for one PC from the "Windows setup"
profile, which is shared by every Windows ISO in LANDeploy."""
import re
from xml.sax.saxutils import escape

LANGUAGES = {  # UI language -> (label, default keyboard)
    "fr-FR": ("Français (France)", "040c:0000040c"),
    "en-US": ("English (United States)", "0409:00000409"),
    "en-GB": ("English (United Kingdom)", "0809:00000809"),
    "ar-SA": ("العربية (Arabic)", "0401:00000401"),
    "es-ES": ("Español", "0c0a:0000040a"),
    "de-DE": ("Deutsch", "0407:00000407"),
    "it-IT": ("Italiano", "0410:00000410"),
    "pt-PT": ("Português", "0816:00000816"),
}
KEYBOARDS = {
    "040c:0000040c": "French (AZERTY)",
    "0409:00000409": "US (QWERTY)",
    "0809:00000809": "UK (QWERTY)",
    "0401:00000401": "Arabic (101)",
    "0c0a:0000040a": "Spanish",
    "0407:00000407": "German (QWERTZ)",
    "0410:00000410": "Italian",
    "0816:00000816": "Portuguese",
    "080c:0000080c": "Belgian French",
    "100c:0000100c": "Swiss French",
}
TIMEZONES = ["Morocco Standard Time", "Romance Standard Time", "W. Europe Standard Time", "GMT Standard Time",
             "UTC", "Arab Standard Time", "Egypt Standard Time", "Eastern Standard Time", "Pacific Standard Time"]
EDITION_KEYS = {  # generic install keys: choose the edition, they do NOT activate Windows
    "pro": "VK7JG-NPHTM-C97JM-9MPGT-3V66T",
    "home": "YTMG3-N6DKC-DKB77-7M9GH-8HVX7",
    "education": "YNMGQ-8RYV3-4PGQ3-C8XTP-7CFBY",
}

DEFAULT_PROFILE = {
    "enabled": False,
    "language": "fr-FR", "keyboard": "040c:0000040c", "timezone": "Morocco Standard Time",
    "edition": "pro", "product_key": "",
    "accounts": [
        {"prefix": "Admin", "suffix": "number", "count": 2, "admin": True, "password": ""},
        {"prefix": "Eleve", "suffix": "none", "count": 1, "admin": False, "password": ""},
    ],
    "same_password": True, "shared_password": "",
    "computer_names": "inventory",            # inventory | pattern | random
    "pc_prefix": "PC-", "pc_suffix": "number2", "pc_start": 1,
    "skip_oobe": True, "bypass_requirements": True,
    "wipe_disk": False,
}


def suffix(kind, i):
    """i-th suffix, i starting at 0."""
    if kind == "number":
        return str(i + 1)
    if kind == "number2":
        return f"{i + 1:02d}"
    if kind in ("letter", "LETTER"):
        s, n = "", i
        while True:                            # a..z, aa, ab... (spreadsheet style)
            s = chr(97 + n % 26) + s
            n = n // 26 - 1
            if n < 0:
                break
        return s if kind == "letter" else s.upper()
    return ""


def account_names(row):
    count = 1 if row.get("suffix") == "none" else max(1, min(int(row.get("count") or 1), 50))
    return [clean_user(row.get("prefix", "") + suffix(row.get("suffix"), i)) for i in range(count)]


def clean_user(name):
    return re.sub(r'[\\/\[\]:;|=,+*?<>"@%]', "", name).strip()[:20]


def clean_pc(name):
    n = re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9-]", "-", name)).strip("-").upper()[:15].strip("-")
    return n if n and not n.isdigit() else ("PC-" + n)[:15]


def computer_name(profile, machine, number):
    mode = profile.get("computer_names", "inventory")
    if mode == "pattern":
        return clean_pc(profile.get("pc_prefix", "PC-") + suffix(profile.get("pc_suffix", "number2"), number))
    if mode == "inventory" and machine and machine.get("name"):
        return clean_pc(machine["name"])
    return "*"                                 # Windows picks a random DESKTOP-XXXX name


def check(profile):
    """List of problems that would make Windows Setup fail."""
    errs, seen = [], set()
    rows = profile.get("accounts", [])
    if not rows:
        errs.append("Add at least one account.")
    if not any(r.get("admin") for r in rows):
        errs.append("At least one account must be an administrator.")
    for r in rows:
        for n in account_names(r):
            if not n:
                errs.append("An account has an empty name.")
            elif n.lower() in seen:
                errs.append(f"Account name used twice: {n}")
            elif n.lower() in ("administrator", "guest", "defaultaccount", "system"):
                errs.append(f"'{n}' is a reserved Windows name.")
            seen.add(n.lower())
    return errs


def _c(name, body, arch="amd64"):
    return (f'    <component name="{name}" processorArchitecture="{arch}" publicKeyToken="31bf3856ad364e35" '
            f'language="neutral" versionScope="nonSxS">\n{body}    </component>\n')


def _disk(uefi):
    if uefi:
        parts = """          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Type>EFI</Type><Size>300</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>2</Order><Type>MSR</Type><Size>16</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>3</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Format>FAT32</Format><Label>System</Label></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>3</PartitionID><Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter></ModifyPartition>
          </ModifyPartitions>"""
        target = 3
    else:
        parts = """          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Type>Primary</Type><Size>500</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>2</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Format>NTFS</Format><Label>System</Label><Active>true</Active></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>2</PartitionID><Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter></ModifyPartition>
          </ModifyPartitions>"""
        target = 2
    return f"""      <DiskConfiguration>
        <Disk wcm:action="add">
          <DiskID>0</DiskID><WillWipeDisk>true</WillWipeDisk>
{parts}
        </Disk>
      </DiskConfiguration>
      <ImageInstall><OSImage>
        <InstallTo><DiskID>0</DiskID><PartitionID>{target}</PartitionID></InstallTo>
        <InstallToAvailablePartition>false</InstallToAvailablePartition>
      </OSImage></ImageInstall>
"""


def build(profile, machine=None, number=0, uefi=True):
    p = {**DEFAULT_PROFILE, **(profile or {})}
    lang, kb, x = p["language"], p["keyboard"], escape
    locale = f"""      <InputLocale>{x(kb)}</InputLocale>
      <SystemLocale>{x(lang)}</SystemLocale>
      <UILanguage>{x(lang)}</UILanguage>
      <UserLocale>{x(lang)}</UserLocale>
"""
    key = p.get("product_key", "").strip() if p.get("edition") == "custom" else EDITION_KEYS.get(p.get("edition"), "")
    key_xml = (f"        <ProductKey><Key>{x(key)}</Key><WillShowUI>OnError</WillShowUI></ProductKey>\n"
               if key else "")
    run, order = [], 1
    if p.get("bypass_requirements"):
        for v in ("BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck"):
            run.append(f'        <RunSynchronousCommand wcm:action="add"><Order>{order}</Order>'
                       f'<Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v {v} /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>\n')
            order += 1
    setup = ((_disk(uefi) if p.get("wipe_disk") else "")          # schema order: Disk, Image, Run, UserData
             + ("      <RunSynchronous>\n" + "".join(run) + "      </RunSynchronous>\n" if run else "")
             + f"      <UserData>\n        <AcceptEula>true</AcceptEula>\n{key_xml}      </UserData>\n")
    pe = _c("Microsoft-Windows-International-Core-WinPE",
            f"      <SetupUILanguage><UILanguage>{x(lang)}</UILanguage></SetupUILanguage>\n" + locale) \
        + _c("Microsoft-Windows-Setup", setup)
    spec = _c("Microsoft-Windows-Shell-Setup",
              f"      <ComputerName>{x(computer_name(p, machine, number))}</ComputerName>\n"
              f"      <TimeZone>{x(p['timezone'])}</TimeZone>\n")
    accounts = ""
    for r in p.get("accounts", []):
        for n in account_names(r):
            accounts += (f'          <LocalAccount wcm:action="add"><Name>{x(n)}</Name><DisplayName>{x(n)}</DisplayName>'
                         f'<Group>{"Administrators" if r.get("admin") else "Users"}</Group>'
                         f'<Password><Value>{x(p.get("shared_password", "") if p.get("same_password") else r.get("password", ""))}</Value>'
                         f'<PlainText>true</PlainText></Password>'
                         f'</LocalAccount>\n')
    oobe = ("      <OOBE>\n        <HideEULAPage>true</HideEULAPage>\n"
            + ("        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>\n"
               "        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>\n"
               "        <ProtectYourPC>3</ProtectYourPC>\n" if p.get("skip_oobe") else "")
            + "      </OOBE>\n")
    shell = oobe + (f"      <UserAccounts>\n        <LocalAccounts>\n{accounts}        </LocalAccounts>\n      </UserAccounts>\n"
                    if accounts else "")
    oo = _c("Microsoft-Windows-International-Core", locale) + _c("Microsoft-Windows-Shell-Setup", shell)
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<!-- Generated by LANDeploy (Windows setup page) -->\n'
            '<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">\n'
            f'  <settings pass="windowsPE">\n{pe}  </settings>\n'
            f'  <settings pass="specialize">\n{spec}  </settings>\n'
            f'  <settings pass="oobeSystem">\n{oo}  </settings>\n'
            '</unattend>\n')
