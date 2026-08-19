from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.session import parse_endpoint, quote_env_value, upsert_env


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
        assert response.status_code == 401
        assert client.get("/api/health").json()["mode"] == "demo"


def test_logout_returns_demo():
    with TestClient(app) as client:
        response = client.post("/api/logout", json={"forget": False})
        assert response.status_code == 200
        payload = response.json()
        assert payload["mode"] == "demo"
        assert payload["can_disconnect"] is False
