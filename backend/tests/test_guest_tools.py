from app import guest_tools
from app.guest_tools import RemoteResult


def test_windows_preflight_requires_administrator(monkeypatch):
    monkeypatch.setattr(
        guest_tools,
        "run_windows_ps",
        lambda *args, **kwargs: RemoteResult(0, '{"computer":"LAB","administrator":true}', ""),
    )
    assert guest_tools.windows_preflight("10.0.0.1", "admin", "secret", transport="http", port=5985, validate_certificate=True)["computer"] == "LAB"


def test_linux_install_uses_allowlisted_open_vm_tools_script(monkeypatch):
    captured = {}

    def run(*args, **kwargs):
        captured["command"] = args[3]
        return RemoteResult(0, "12.4.0.12345 (build-1)", "")

    monkeypatch.setattr(guest_tools, "_linux_command", run)
    result = guest_tools.linux_install(
        "10.0.0.2",
        "operator",
        "secret",
        port=22,
        host_key_sha256="SHA256:test",
        sudo=True,
    )
    assert "apt-get -qq install -y open-vm-tools" in captured["command"]
    assert "dnf -q install -y open-vm-tools" in captured["command"]
    assert result["message"].startswith("12.4.0")
