from types import SimpleNamespace
import importlib
import pytest
from fastapi.testclient import TestClient
from app.adapters.vcenter import VCenterAdapter
from app.config import Settings
from app.errors import PermanentError
from app.models import ConnectionInfo
from app import guest_tools


def probe_adapter(monkeypatch, *, running=False, auth_fail=False, stop_fail=False):
    events = []
    def stop(**kwargs):
        events.append("stop")
        if stop_fail:
            raise RuntimeError("stop failed")
    services = SimpleNamespace(
        RefreshServices=lambda: events.append("refresh"),
        StartService=lambda **kwargs: events.append("start"),
        StopService=stop,
        serviceInfo=SimpleNamespace(service=[SimpleNamespace(key="TSM-SSH", running=running)]),
    )
    adapter = VCenterAdapter(Settings(_env_file=None, vcenter_host="esxi.test",
        esxi_ssh_user="ssh-test", esxi_ssh_password="test-secret"))
    monkeypatch.setattr(adapter, "_is_esxi", lambda: True)
    monkeypatch.setattr(adapter, "_host_obj", lambda: SimpleNamespace(configManager=SimpleNamespace(serviceSystem=services)))
    def read_key(*args, **kwargs):
        events.append("key")
        return "SHA256:test"
    def authenticate(*args, **kwargs):
        events.append("auth")
        assert kwargs["host_key_sha256"] == "SHA256:test"
        if auth_fail:
            raise RuntimeError("sensitive-test-secret")
        return SimpleNamespace(close=lambda: events.append("close"))
    monkeypatch.setattr(guest_tools, "ssh_host_key_sha256", read_key)
    monkeypatch.setattr(guest_tools, "_connect_pinned_ssh", authenticate)
    return adapter, events


def test_setup_requires_confirmation_before_any_service_access(monkeypatch):
    adapter, events = probe_adapter(monkeypatch)
    with pytest.raises(PermanentError, match="Confirm"):
        adapter.prepare_ssh_host_key()
    assert events == []


@pytest.mark.parametrize("running", [False, True])
def test_setup_restores_original_service_state(monkeypatch, running):
    adapter, events = probe_adapter(monkeypatch, running=running)
    assert adapter.prepare_ssh_host_key(confirm=True) == "SHA256:test"
    assert events == (["refresh", "key", "auth", "close"] if running else
                      ["refresh", "start", "key", "auth", "close", "stop"])


def test_setup_restores_service_on_auth_failure_and_redacts_error(monkeypatch):
    adapter, events = probe_adapter(monkeypatch, auth_fail=True)
    with pytest.raises(PermanentError) as error:
        adapter.prepare_ssh_host_key(confirm=True)
    assert "sensitive-test-secret" not in str(error.value)
    assert events[-1] == "stop"


def test_setup_reports_failed_service_restoration(monkeypatch):
    adapter, events = probe_adapter(monkeypatch, stop_fail=True)
    with pytest.raises(PermanentError, match="Could not restore"):
        adapter.prepare_ssh_host_key(confirm=True)
    assert events[-1] == "stop"


def test_setup_does_not_touch_services_on_vcenter_or_missing_credentials(monkeypatch):
    adapter, events = probe_adapter(monkeypatch)
    monkeypatch.setattr(adapter, "_is_esxi", lambda: False)
    with pytest.raises(PermanentError, match="direct ESXi"):
        adapter.prepare_ssh_host_key(confirm=True)
    monkeypatch.setattr(adapter, "_is_esxi", lambda: True)
    adapter.settings.esxi_ssh_password = ""
    with pytest.raises(PermanentError, match="username and password"):
        adapter.prepare_ssh_host_key(confirm=True)
    assert events == []


@pytest.mark.parametrize("confirm,pinned,expected_calls", [
    (False, "", 0), (True, "", 1), (True, "SHA256:existing", 0),
])
def test_login_test_only_prepares_with_explicit_opt_in(monkeypatch, confirm, pinned, expected_calls):
    main = importlib.import_module("app.main")
    calls = []
    class Candidate:
        def __init__(self, settings):
            self.settings = settings
        def connection(self):
            return ConnectionInfo(mode="esxi", connected=True, endpoint_kind="esxi",
                                  endpoint_fingerprint="test-esxi", host="esxi.test")
        def prepare_ssh_host_key(self, *, confirm=False):
            assert confirm
            calls.append("prepare")
            return "SHA256:detected"
        def close(self):
            pass
    with TestClient(main.app) as client:
        monkeypatch.setattr(main, "VCenterAdapter", Candidate)
        previous = main.app.state.adapter
        response = client.post("/api/login", json={
            "host": "esxi.test", "user": "api-test", "password": "test-only-password",
            "connect": False, "remember": False, "ssh_enabled": True,
            "ssh_user": "ssh-test", "ssh_password": "test-only-ssh",
            "ssh_start_service_confirm": confirm, "ssh_host_key_sha256": pinned,
        })
        assert response.status_code == 200, response.text
        assert len(calls) == expected_calls
        assert response.json()["ssh_host_key_sha256"] == (pinned or ("SHA256:detected" if confirm else ""))
        assert main.app.state.adapter is previous
        assert "test-only-password" not in response.text
