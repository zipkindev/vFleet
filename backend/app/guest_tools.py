from __future__ import annotations

import base64
import hashlib
import json
import shlex
import socket
import time
from dataclasses import dataclass
from typing import Dict, Optional

from .errors import PermanentError, TransientError


WINDOWS_PREFLIGHT = r"""
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
[ordered]@{
  computer = $env:COMPUTERNAME
  os = (Get-CimInstance Win32_OperatingSystem).Caption
  architecture = $env:PROCESSOR_ARCHITECTURE
  administrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
} | ConvertTo-Json -Compress
""".strip()

WINDOWS_INSTALL = r"""
$ErrorActionPreference = 'Stop'
$cd = Get-CimInstance Win32_LogicalDisk | Where-Object DriveType -eq 5 | Where-Object {
  Test-Path (Join-Path $_.DeviceID 'setup64.exe')
} | Select-Object -First 1
if (-not $cd) { throw 'The VMware Tools setup64.exe was not found on any CD-ROM drive' }
$setup = Join-Path $cd.DeviceID 'setup64.exe'
$process = Start-Process -FilePath $setup -ArgumentList '/S','/v"/qn REBOOT=R"' -Wait -PassThru
if ($process.ExitCode -notin @(0, 1641, 3010)) {
  throw "VMware Tools installer exited with code $($process.ExitCode)"
}
shutdown.exe /r /t 15 /d p:2:4 /c "vFleet VMware Tools installation" | Out-Null
[ordered]@{ installer_exit_code = $process.ExitCode; reboot_scheduled = $true } | ConvertTo-Json -Compress
""".strip()


@dataclass
class RemoteResult:
    status_code: int
    stdout: str
    stderr: str


def _safe_text(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()


def run_windows_ps(
    address: str,
    username: str,
    password: str,
    script: str,
    *,
    transport: str,
    port: int,
    validate_certificate: bool,
    timeout_seconds: int = 90,
) -> RemoteResult:
    try:
        import winrm
    except ImportError as exc:
        raise PermanentError("Windows automation support is not installed; install backend requirements") from exc
    scheme = "https" if transport == "https" else "http"
    endpoint = f"{scheme}://{address}:{port}/wsman"
    try:
        session = winrm.Session(
            endpoint,
            auth=(username, password),
            transport="ntlm",
            server_cert_validation="validate" if validate_certificate else "ignore",
            message_encryption="always",
            read_timeout_sec=timeout_seconds,
            operation_timeout_sec=max(5, timeout_seconds - 5),
        )
        response = session.run_ps(script)
    except (socket.timeout, OSError) as exc:
        raise TransientError(f"WinRM could not reach {address}:{port}: {exc}") from exc
    except Exception as exc:
        message = str(exc).replace(password, "[redacted]")
        lowered = message.lower()
        if any(token in lowered for token in ("timed out", "connection", "temporarily", "reset by peer")):
            raise TransientError(f"WinRM connection failed for {address}:{port}: {message}") from exc
        raise PermanentError(f"WinRM authentication or command failed for {address}: {message}") from exc
    result = RemoteResult(response.status_code, _safe_text(response.std_out), _safe_text(response.std_err))
    if result.status_code != 0:
        raise PermanentError(result.stderr or f"Remote PowerShell exited with code {result.status_code}")
    return result


def windows_preflight(address: str, username: str, password: str, **options) -> Dict[str, object]:
    result = run_windows_ps(address, username, password, WINDOWS_PREFLIGHT, **options)
    try:
        payload = json.loads(result.stdout.splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise PermanentError("Windows preflight did not return valid system information") from exc
    if not payload.get("administrator"):
        raise PermanentError("The selected Windows credential is not a local administrator")
    return payload


def windows_install(address: str, username: str, password: str, **options) -> Dict[str, object]:
    result = run_windows_ps(address, username, password, WINDOWS_INSTALL, timeout_seconds=900, **options)
    try:
        return json.loads(result.stdout.splitlines()[-1])
    except (ValueError, IndexError):
        return {"message": result.stdout or "Installer completed and reboot was scheduled"}


def ssh_host_key_sha256(address: str, port: int = 22, timeout_seconds: int = 10) -> str:
    try:
        import paramiko

        sock = socket.create_connection((address, port), timeout=timeout_seconds)
        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=timeout_seconds)
            key = transport.get_remote_server_key()
            return _key_sha256(key)
        finally:
            transport.close()
            sock.close()
    except (socket.timeout, OSError) as exc:
        raise TransientError(f"SSH could not reach {address}:{port}: {exc}") from exc
    except Exception as exc:
        raise PermanentError(f"Could not read the SSH host key from {address}:{port}: {exc}") from exc


def _key_sha256(key) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


class _PinnedHostKeyPolicy:
    def __init__(self, expected: str) -> None:
        self.expected = expected

    def missing_host_key(self, client, hostname, key) -> None:
        actual = _key_sha256(key)
        if actual != self.expected:
            raise PermanentError(f"SSH host key mismatch for {hostname}; expected {self.expected}, received {actual}")


def _linux_command(
    address: str,
    username: str,
    password: str,
    command: str,
    *,
    port: int,
    host_key_sha256: str,
    sudo: bool,
    timeout_seconds: int,
) -> RemoteResult:
    if not host_key_sha256.startswith("SHA256:"):
        raise PermanentError(f"A verified SSH host-key fingerprint is required for {address}")
    try:
        import paramiko

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(_PinnedHostKeyPolicy(host_key_sha256))
        client.connect(
            hostname=address,
            port=port,
            username=username,
            password=password,
            timeout=timeout_seconds,
            auth_timeout=timeout_seconds,
            banner_timeout=timeout_seconds,
            look_for_keys=False,
            allow_agent=False,
        )
        try:
            transport = client.get_transport()
            actual_key = _key_sha256(transport.get_remote_server_key()) if transport is not None else ""
            if actual_key != host_key_sha256:
                raise PermanentError(
                    f"SSH host key mismatch for {address}; expected {host_key_sha256}, received {actual_key or 'unavailable'}"
                )
            remote = command
            if sudo and username != "root":
                remote = "sudo -S -p '' sh -c " + shlex.quote(command)
            stdin, stdout, stderr = client.exec_command(remote, get_pty=bool(sudo and username != "root"), timeout=timeout_seconds)
            if sudo and username != "root":
                stdin.write(password + "\n")
                stdin.flush()
            channel = stdout.channel
            output = bytearray()
            errors = bytearray()
            deadline = time.monotonic() + timeout_seconds
            while True:
                while channel.recv_ready():
                    output.extend(channel.recv(65536))
                while channel.recv_stderr_ready():
                    errors.extend(channel.recv_stderr(65536))
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() >= deadline:
                    channel.close()
                    raise TransientError(f"SSH command timed out on {address}")
                time.sleep(0.05)
            status = channel.recv_exit_status()
            result = RemoteResult(status, _safe_text(bytes(output)), _safe_text(bytes(errors)))
        finally:
            client.close()
    except PermanentError:
        raise
    except (socket.timeout, OSError) as exc:
        raise TransientError(f"SSH could not reach {address}:{port}: {exc}") from exc
    except Exception as exc:
        message = str(exc).replace(password, "[redacted]")
        raise PermanentError(f"SSH authentication or command failed for {address}: {message}") from exc
    if result.status_code != 0:
        raise PermanentError(result.stderr or f"Remote Linux command exited with code {result.status_code}")
    return result


LINUX_PREFLIGHT = """
set -eu
. /etc/os-release 2>/dev/null || true
printf '{"id":"%s","name":"%s","uid":%s}\n' "${ID:-unknown}" "${PRETTY_NAME:-Linux}" "$(id -u)"
""".strip()

LINUX_INSTALL = """
set -eu
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get -qq update
  apt-get -qq install -y open-vm-tools
elif command -v dnf >/dev/null 2>&1; then
  dnf -q install -y open-vm-tools
elif command -v yum >/dev/null 2>&1; then
  yum -q install -y open-vm-tools
elif command -v zypper >/dev/null 2>&1; then
  zypper --non-interactive install open-vm-tools
elif command -v tdnf >/dev/null 2>&1; then
  tdnf install -y open-vm-tools
elif command -v apk >/dev/null 2>&1; then
  apk add open-vm-tools
else
  echo 'No supported package manager found' >&2
  exit 42
fi
if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now vmtoolsd.service 2>/dev/null || systemctl enable --now open-vm-tools.service 2>/dev/null || true
fi
pgrep -x vmtoolsd >/dev/null || { echo 'vmtoolsd is not running after installation' >&2; exit 43; }
vmware-toolbox-cmd -v 2>/dev/null || vmtoolsd -v 2>/dev/null || true
""".strip()


def linux_preflight(address: str, username: str, password: str, **options) -> Dict[str, object]:
    result = _linux_command(address, username, password, LINUX_PREFLIGHT, timeout_seconds=30, **options)
    try:
        return json.loads(result.stdout.splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise PermanentError("Linux preflight did not return valid system information") from exc


def linux_install(address: str, username: str, password: str, **options) -> Dict[str, object]:
    result = _linux_command(address, username, password, LINUX_INSTALL, timeout_seconds=900, **options)
    return {"message": result.stdout.splitlines()[-1] if result.stdout else "open-vm-tools installed and vmtoolsd is running"}
