"""Official Phone Link entry points; account, pairing and permissions stay interactive."""

import os
import subprocess
import sys
from pathlib import Path

PHONE_LINK_STORE = "ms-windows-store://pdp/?ProductId=9NMPJ99VJBWV"
PHONE_LINK_APP = r"shell:AppsFolder\Microsoft.YourPhone_8wekyb3d8bbwe!App"
_DETECTION = (
    "$ErrorActionPreference='Stop'; "
    "$package=Get-AppxPackage -Name Microsoft.YourPhone | Select-Object -First 1; "
    "if ($null -ne $package) { Write-Output 'GUARDMATE_PHONE_LINK_PRESENT' }"
)


class PhoneLinkError(RuntimeError):
    pass


def _windows() -> None:
    if sys.platform != "win32":
        raise PhoneLinkError("Phone Link setup is available only on Windows.")


def _system_program(name: str) -> str:
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / name)


def detect_phone_link() -> bool:
    """Inspect this user's installed package without opening an app or audio stream."""
    _windows()
    try:
        result = subprocess.run(
            [
                _system_program(r"System32\WindowsPowerShell\v1.0\powershell.exe"),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _DETECTION,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or len(result.stdout) > 256:
            raise PhoneLinkError("Phone Link installation status could not be checked.")
        return result.stdout.strip() == b"GUARDMATE_PHONE_LINK_PRESENT"
    except (OSError, subprocess.TimeoutExpired):
        raise PhoneLinkError("Phone Link installation status could not be checked.") from None


def open_phone_link() -> None:
    _windows()
    try:
        subprocess.run(
            [_system_program("explorer.exe"), PHONE_LINK_APP],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        raise PhoneLinkError("Open Phone Link from the Windows Start menu.") from None


def open_phone_link_install() -> None:
    """Open Microsoft's Store page; the user completes installation and any agreements."""
    _windows()
    try:
        os.startfile(PHONE_LINK_STORE)
    except OSError:
        raise PhoneLinkError(
            "Open Windows Settings, search Phone Link, and enable or install it there."
        ) from None
