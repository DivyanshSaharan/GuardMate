import subprocess
from types import SimpleNamespace

import pytest
from guardmate.desktop import phone_link


def test_detection_is_fixed_bounded_and_does_not_install_or_open_apps(monkeypatch):
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=0, stdout=b"GUARDMATE_PHONE_LINK_PRESENT\r\n")

    monkeypatch.setattr(phone_link, "_windows", lambda: None)
    monkeypatch.setattr(phone_link.subprocess, "run", run)
    assert phone_link.detect_phone_link()
    command, options = calls[0]
    assert "Get-AppxPackage -Name Microsoft.YourPhone" in command[-1]
    assert "-NoProfile" in command and "-NonInteractive" in command
    assert options["timeout"] == 10 and not options["shell"]
    assert options["stdin"] == subprocess.DEVNULL
    assert "install" not in command[-1].lower()


def test_absent_package_is_distinct_from_failed_detection(monkeypatch):
    monkeypatch.setattr(phone_link, "_windows", lambda: None)
    monkeypatch.setattr(
        phone_link.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"")
    )
    assert not phone_link.detect_phone_link()
    monkeypatch.setattr(
        phone_link.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"")
    )
    with pytest.raises(phone_link.PhoneLinkError):
        phone_link.detect_phone_link()


def test_install_opens_only_the_official_interactive_store(monkeypatch):
    opened = []
    monkeypatch.setattr(phone_link, "_windows", lambda: None)
    monkeypatch.setattr(phone_link.os, "startfile", opened.append, raising=False)
    phone_link.open_phone_link_install()
    assert opened == ["ms-windows-store://pdp/?ProductId=9NMPJ99VJBWV"]


def test_app_launch_has_a_fixed_target_and_sanitized_failure(monkeypatch):
    monkeypatch.setattr(phone_link, "_windows", lambda: None)
    commands = []
    monkeypatch.setattr(
        phone_link.subprocess, "run", lambda command, **options: commands.append(command)
    )
    phone_link.open_phone_link()
    assert commands[0][-1] == phone_link.PHONE_LINK_APP
    assert not any("winget" in part for part in commands[0])
