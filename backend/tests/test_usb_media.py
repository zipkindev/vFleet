from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.errors import PermanentError
from app import usb_media
from app.usb_media import UsbWriteRequest


def usb_row(**overrides):
    row = {
        "number": 3,
        "friendly_name": "Test USB",
        "serial_number": "SERIAL-123",
        "unique_id": "USBSTOR\\TEST",
        "size_bytes": 16 * 1024**3,
        "drive_letters": ["E"],
        "is_boot": False,
        "is_system": False,
        "is_offline": False,
        "is_read_only": False,
    }
    row.update(overrides)
    return row


def request(**overrides):
    values = {
        "plan_id": uuid4(),
        "disk_number": 3,
        "friendly_name": "Test USB",
        "serial_number": "SERIAL-123",
        "unique_id": "USBSTOR\\TEST",
        "size_bytes": 16 * 1024**3,
        "confirmation": "ERASE USB DISK 3",
    }
    values.update(overrides)
    return UsbWriteRequest(**values)


def test_usb_inventory_allows_only_stable_non_system_usb(monkeypatch):
    rows = [usb_row(), usb_row(number=4, serial_number="", unique_id="", is_system=True)]
    monkeypatch.setattr(usb_media, "_run_powershell", lambda _script: json.dumps(rows))

    inventory = usb_media.list_usb_devices()

    assert inventory["supported"] is True
    assert inventory["devices"][0]["safe"] is True
    assert inventory["devices"][1]["safe"] is False
    assert "system" in inventory["devices"][1]["blocked_reason"].lower()


def test_usb_inventory_explains_container_limitation(monkeypatch):
    monkeypatch.setenv("VFLEET_RUNTIME", "container")
    monkeypatch.setattr(usb_media.shutil, "which", lambda _name: None)

    inventory = usb_media.list_usb_devices()

    assert inventory["supported"] is False
    assert "container image" in inventory["message"]
    assert "Windows application" in inventory["message"]


def test_usb_inventory_rejects_non_windows_even_if_powershell_is_on_path(monkeypatch):
    monkeypatch.delenv("VFLEET_RUNTIME", raising=False)
    monkeypatch.setattr(usb_media.sys, "platform", "linux")
    monkeypatch.setattr(usb_media.shutil, "which", lambda _name: "/mnt/c/powershell.exe")

    inventory = usb_media.list_usb_devices()

    assert inventory["supported"] is False
    assert "Linux desktop application" in inventory["message"]


def test_usb_inventory_does_not_expose_internal_exception_details(monkeypatch):
    monkeypatch.setenv("VFLEET_RUNTIME", "container")

    def fail(_script):
        raise PermanentError(r"C:\\Users\\operator\\secret-path: internal failure")

    monkeypatch.setattr(usb_media, "_run_powershell", fail)

    inventory = usb_media.list_usb_devices()

    assert inventory["supported"] is False
    assert inventory["message"] == (
        "USB creation is unavailable in the vFleet container image; use the native Windows application "
        "on the computer where the USB disk is attached"
    )
    assert "secret-path" not in inventory["message"]


def test_usb_write_requires_typed_confirmation_and_exact_rescan_identity(monkeypatch, tmp_path):
    plan_id = uuid4()
    plan = {
        "id": str(plan_id),
        "phase": "planned",
        "staging_id": str(uuid4()),
        "media": {"installer_complete": True, "upgrade_metadata_present": True},
    }
    monkeypatch.setattr(usb_media, "load_run", lambda _store, _id: plan)
    monkeypatch.setattr(usb_media, "media_path", lambda _store, _id: tmp_path / "installer.iso")
    monkeypatch.setattr(usb_media, "list_usb_devices", lambda: {
        "supported": True,
        "devices": [dict(usb_row(), safe=True, blocked_reason="")],
    })

    with pytest.raises(PermanentError, match="Type ERASE USB DISK 3"):
        usb_media.validate_usb_write(None, request(plan_id=plan_id, confirmation="yes"))

    checked, path, device = usb_media.validate_usb_write(None, request(plan_id=plan_id))
    assert checked["id"] == str(plan_id)
    assert path == tmp_path / "installer.iso"
    assert device["serial_number"] == "SERIAL-123"

    with pytest.raises(PermanentError, match="identity changed"):
        usb_media.validate_usb_write(None, request(plan_id=plan_id, size_bytes=8 * 1024**3))


def test_usb_write_is_not_allowed_during_host_preparation(monkeypatch):
    plan = {
        "id": str(uuid4()),
        "phase": "awaiting_installation",
        "staging_id": str(uuid4()),
        "media": {"installer_complete": True, "upgrade_metadata_present": True},
    }
    monkeypatch.setattr(usb_media, "load_run", lambda _store, _id: plan)

    with pytest.raises(PermanentError, match="before host preparation"):
        usb_media.validate_usb_write(None, request(plan_id=plan["id"]))


def test_usb_maker_uses_verified_bit_for_bit_image_write():
    script = usb_media.WRITER_SCRIPT
    assert "PhysicalDrive" in script
    assert "mountvol.exe" in script
    assert "$destination.Flush($true)" in script
    assert "Get-FileHash -Algorithm SHA256" in script
    assert "USB read-back checksum failed" in script


def test_uncertain_launched_job_is_never_relaunched(monkeypatch, tmp_path):
    spec = request()
    plan = {"id": str(spec.plan_id), "media": {"size": 100, "sha256": "0" * 64}}
    monkeypatch.setattr(usb_media, "validate_usb_write", lambda _store, _spec: (plan, tmp_path / "source.iso", usb_row()))
    monkeypatch.setattr(usb_media, "_windows_temp_paths", lambda _id: (tmp_path, {}))
