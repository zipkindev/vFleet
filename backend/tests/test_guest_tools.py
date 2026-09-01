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


def test_pfsense_preflight_requires_platform_and_root(monkeypatch):
    captured = {}

    def run(*args, **kwargs):
        captured["command"] = args[3]
        captured["sudo"] = kwargs["sudo"]
        return RemoteResult(0, '{"platform":"pfSense","version":"2.7.2-RELEASE","uid":0,"installed":false}', "")

    monkeypatch.setattr(guest_tools, "_linux_command", run)
    result = guest_tools.pfsense_preflight(
        "10.0.0.1",
        "root",
        "secret",
        port=22,
        host_key_sha256="SHA256:test",
    )

    assert result["platform"] == "pfSense"
    assert "cat /etc/platform" in captured["command"]
    assert '"$(id -u)" = "0"' in captured["command"]
    assert captured["sudo"] is False


def test_pfsense_install_uses_only_supported_pfsense_package(monkeypatch):
    captured = {}

    def run(*args, **kwargs):
        captured["command"] = args[3]
        captured["jump"] = kwargs.get("jump")
        return RemoteResult(0, "12.5.0", "")

    monkeypatch.setattr(guest_tools, "_linux_command", run)
    jump = {
        "address": "192.168.1.60",
        "port": 22,
        "username": "operator",
        "password": "jump-secret",
        "host_key_sha256": "SHA256:jump",
    }
    result = guest_tools.pfsense_install(
        "10.0.0.1",
        "root",
        "secret",
        port=22,
        host_key_sha256="SHA256:target",
        jump=jump,
    )

    assert "/usr/local/sbin/pkg-static install -y pfSense-pkg-Open-VM-Tools" in captured["command"]
    assert "apt-get" not in captured["command"]
    assert captured["jump"] == jump
    assert result["message"] == "12.5.0"


def test_jump_connection_test_only_requires_pinned_ssh_handshake(monkeypatch):
    class WindowsJumpClient:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    client = WindowsJumpClient()
    captured = {}

    def connect(address, username, password, **kwargs):
        captured.update({"address": address, "username": username, "password": password, **kwargs})
        return client

    monkeypatch.setattr(guest_tools, "_connect_pinned_ssh", connect)

    detected = guest_tools.ssh_connection_test(
        "192.168.1.60",
        "jump-user",
        "jump-secret",
        port=22,
        host_key_sha256="SHA256:jump",
        host_type="windows",
    )

    assert captured["host_key_sha256"] == "SHA256:jump"
    assert detected == "windows"
    assert client.closed is True


def test_jump_connection_test_auto_discovers_windows(monkeypatch):
    class Stream:
        def __init__(self, output: bytes, status: int):
            self.output = output
            self.channel = self
            self.status = status

        def read(self):
            return self.output

        def recv_exit_status(self):
            return self.status

    class Input:
        def close(self):
            pass

    class WindowsJumpClient:
        def __init__(self):
            self.closed = False

        def exec_command(self, command, timeout):
            assert command.startswith("cmd.exe")
            return Input(), Stream(b"VFLEET_WINDOWS\r\n", 0), Stream(b"", 0)

        def close(self):
            self.closed = True

    client = WindowsJumpClient()
    monkeypatch.setattr(guest_tools, "_connect_pinned_ssh", lambda *args, **kwargs: client)

    detected = guest_tools.ssh_connection_test(
        "192.168.1.60",
        "jump-user",
        "jump-secret",
        host_key_sha256="SHA256:jump",
    )

    assert detected == "windows"
    assert client.closed is True
