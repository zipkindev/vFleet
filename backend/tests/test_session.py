from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.session import parse_endpoint, quote_env_value, resolve_login_password, resolve_ssh_password, upsert_env


def test_parse_endpoint_strips_scheme_and_port():
    assert parse_endpoint("https://vcenter.lab.local:8443/sdk") == ("vcenter.lab.local", 8443)
    assert parse_endpoint("vcenter.lab.local", 443) == ("vcenter.lab.local", 443)


def test_upsert_env_quotes_password(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text("APP_MODE=demo\nVCENTER_HOST=old\n", encoding="utf-8")
    upsert_env(path, {"VCENTER_HOST": "vc.example", "VCENTER_PASSWORD": "p@ss#word"})
    text = path.read_text(encoding="utf-8")
    assert "VCENTER_HOST=vc.example" in text
    assert "VCENTER_PASSWORD=" + quote_env_value("p@ss#word") in text
    assert "APP_MODE=demo" in text


def test_resolve_login_password_reuses_saved_secret():
    settings = Settings(
        vcenter_host="vc.example",
        vcenter_user="user@vsphere.local",
        vcenter_password="saved-secret",
        vcenter_port=443,
    )
    assert resolve_login_password("", "vc.example", "user@vsphere.local", 443, settings) == "saved-secret"
    assert resolve_login_password("typed", "vc.example", "user@vsphere.local", 443, settings) == "typed"
    assert resolve_login_password("", "other.example", "user@vsphere.local", 443, settings) == ""


def test_resolve_ssh_password_reuses_only_matching_saved_secret():
    settings = Settings(
        vcenter_host="esxi.example",
        esxi_ssh_user="root",
        esxi_ssh_password="saved-ssh-secret",
        esxi_ssh_port=22,
    )
    assert resolve_ssh_password("", "esxi.example", "root", 22, settings) == "saved-ssh-secret"
    assert resolve_ssh_password("typed", "esxi.example", "root", 22, settings) == "typed"
    assert resolve_ssh_password("", "other.example", "root", 22, settings) == ""
    assert resolve_ssh_password("", "esxi.example", "admin", 22, settings) == ""
    assert resolve_ssh_password("", "esxi.example", "root", 2222, settings) == ""


def test_login_requires_fields():
    with TestClient(app) as client:
        response = client.post("/api/login", json={"host": "", "user": "a", "password": "b"})
        assert response.status_code == 400


def test_login_rejects_unreachable_host():
    with TestClient(app) as client:
        response = client.post(
            "/api/login",
            json={
                "host": "127.0.0.1",
                "port": 1,
                "user": "nobody@vsphere.local",
                "password": "invalid",
                "insecure": True,
                "remember": False,
            },
        )
        assert response.status_code == 503
        assert client.get("/api/health").json()["mode"] == "demo"


def test_logout_returns_demo():
    with TestClient(app) as client:
        response = client.post("/api/logout", json={"forget": False})
        assert response.status_code == 200
        payload = response.json()
        assert payload["mode"] == "demo"
        assert payload["can_disconnect"] is False
        assert {"ssh_user", "ssh_port", "ssh_host_key_sha256", "has_saved_ssh_password"} <= payload.keys()
