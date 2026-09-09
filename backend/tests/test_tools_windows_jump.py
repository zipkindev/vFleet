import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import guest_tools
from app.automation_vault import AutomationCredentialStore
from app.config import Settings
from app.credential_vault import CredentialVault
from app.errors import PermanentError
from app.relay import RelayWorker
from app.store import LocalStore


class ToolsAdapter:
    def __init__(self):
        self.checks = 0
        self.restored = False

    def tools_status(self, vm_id):
        self.checks += 1
        return {
            "running": self.checks >= 2,
            "running_status": "guestToolsRunning" if self.checks >= 2 else "guestToolsNotRunning",
        }

    def mount_tools_installer(self, vm_id):
        return {"media": {"file_name": "[data] prior.iso"}}

    def restore_tools_media(self, vm_id, media):
        self.restored = media.get("file_name") == "[data] prior.iso"


def build_worker(tmp_path: Path):
    store = LocalStore(tmp_path / "jobs.db")
    vault = CredentialVault(tmp_path / "secrets.enc.json", key_file=tmp_path / "key")
    credentials = AutomationCredentialStore(tmp_path / "automation.json", vault)
    target = credentials.save(
        name="Windows admin",
        kind="windows",
        username="Administrator",
        secret="target-secret",
        scope="global",
        endpoint_fingerprint="",
    )
    jump = credentials.save(
        name="jump host",
        kind="ssh",
        username="jump-user",
        secret="jump-secret",
        scope="global",
        endpoint_fingerprint="",
    )
    adapter = ToolsAdapter()
    worker = RelayWorker(Settings(data_dir=tmp_path), store, lambda: adapter, credentials)
    return worker, store, adapter, target, jump


def test_windows_preflight_matches_selected_vm_mac(monkeypatch):
    payload = {
        "computer": "SERVER22CORE",
        "os": "Windows Server 2022",
        "administrator": True,
        "network": [{"mac_address": "00:50:56:AA:BB:CC", "ip_addresses": ["10.0.0.2"]}],
    }
    monkeypatch.setattr(
        guest_tools,
        "run_windows_ps",
        lambda *args, **kwargs: guest_tools.RemoteResult(0, json.dumps(payload), ""),
    )

    result = guest_tools.windows_preflight(
        "10.0.0.2",
        "Administrator",
        "secret",
        expected_mac_addresses=["00-50-56-aa-bb-cc"],
        expected_name="Server22Core",
        transport="http",
        port=5985,
        validate_certificate=True,
    )

    assert result["address_verified"] is True
    assert result["matched_mac_address"] == "005056aabbcc"


def test_windows_preflight_rejects_wrong_vm_mac(monkeypatch):
    payload = {
        "computer": "SOME-OTHER-SERVER",
        "os": "Windows Server 2022",
        "administrator": True,
        "network": [{"mac_address": "00:50:56:11:22:33", "ip_addresses": ["10.0.0.2"]}],
    }
    monkeypatch.setattr(
        guest_tools,
        "run_windows_ps",
        lambda *args, **kwargs: guest_tools.RemoteResult(0, json.dumps(payload), ""),
    )

    with pytest.raises(PermanentError, match="does not match Server22Core"):
        guest_tools.windows_preflight(
            "10.0.0.2",
            "Administrator",
            "secret",
            expected_mac_addresses=["00:50:56:aa:bb:cc"],
            expected_name="Server22Core",
            transport="http",
            port=5985,
            validate_certificate=True,
        )


def test_run_windows_ps_uses_jump_loopback_endpoint(monkeypatch):
    captured = {}

    @contextmanager
    def forward(address, port, jump, timeout_seconds):
        captured["forward"] = (address, port, jump["address"], timeout_seconds)
        yield "127.0.0.1", 43123

    class Session:
        def __init__(self, endpoint, **kwargs):
            captured["endpoint"] = endpoint
            captured["session"] = kwargs

        def run_ps(self, script):
            return SimpleNamespace(status_code=0, std_out=b"{}", std_err=b"")

    monkeypatch.setattr(guest_tools, "_jump_tcp_forward", forward)
    monkeypatch.setitem(sys.modules, "winrm", SimpleNamespace(Session=Session))
    result = guest_tools.run_windows_ps(
        "10.0.0.2",
        "Administrator",
        "secret",
        "Get-ComputerInfo",
        transport="http",
        port=5985,
        validate_certificate=True,
        jump={"address": "192.168.1.60", "password": "jump-secret"},
    )

    assert result.status_code == 0
    assert captured["forward"][:3] == ("10.0.0.2", 5985, "192.168.1.60")
    assert captured["endpoint"] == "http://127.0.0.1:43123/wsman"
    assert captured["session"]["message_encryption"] == "always"


def test_windows_tools_job_carries_pinned_jump_to_preflight_and_install(tmp_path, monkeypatch):
    worker, store, adapter, target, jump = build_worker(tmp_path)
    captured = {}
    monkeypatch.setattr(
        guest_tools,
        "windows_preflight",
        lambda *args, **kwargs: captured.setdefault("preflight", kwargs) or {"administrator": True},
    )
    monkeypatch.setattr(
        guest_tools,
        "windows_install",
        lambda *args, **kwargs: captured.setdefault("install", kwargs) or {"reboot_scheduled": True},
    )
    job = store.enqueue("tools_deploy", "Deploy tools", {
        "_endpoint_fingerprint": "demo",
        "vm_id": "vm-1",
        "vm_name": "Server22Core",
        "address": "10.0.0.2",
        "expected_mac_addresses": ["00:50:56:aa:bb:cc"],
        "os_family": "windows",
        "credential_id": target.id,
        "windows_transport": "http",
        "windows_port": 5985,
        "validate_certificate": True,
        "jump_address": "192.168.1.60",
        "jump_port": 22,
        "jump_credential_id": jump.id,
        "jump_host_key_sha256": "SHA256:jump",
    })

    result = worker._deploy_guest_tools(job, adapter, {})

    assert result["tools"]["running"] is True
    assert captured["preflight"]["expected_name"] == "Server22Core"
    assert captured["preflight"]["expected_mac_addresses"] == ["00:50:56:aa:bb:cc"]
    assert captured["preflight"]["jump"]["username"] == "jump-user"
    assert captured["install"]["jump"]["host_key_sha256"] == "SHA256:jump"
    assert adapter.restored is True
    assert "target-secret" not in str(job.payload)
    assert "jump-secret" not in str(job.payload)
