from __future__ import annotations

import base64
import hashlib
import shlex
from pathlib import PurePosixPath
from typing import Tuple

from .config import Settings
from .errors import PermanentError, TransientError


def datastore_path(value: str) -> Tuple[str, str, str]:
    """Return (datastore, relative path, ESXi filesystem path) for API-owned VMDK paths."""
    text = (value or "").strip()
    if not text.startswith("[") or "]" not in text:
        raise PermanentError(f"Unsupported virtual disk path: {text!r}")
    datastore, relative = text[1:].split("]", 1)
    datastore = datastore.strip()
    relative = relative.strip().lstrip("/")
    if not datastore or not relative or any(ord(ch) < 32 for ch in text):
        raise PermanentError("Unsafe or incomplete datastore path")
    parts = PurePosixPath(relative).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise PermanentError("Datastore paths cannot contain traversal segments")
    full = str(PurePosixPath("/vmfs/volumes") / datastore / PurePosixPath(*parts))
    return datastore, str(PurePosixPath(*parts)), full


class _PinnedHostKeyPolicy:
    def __init__(self, expected: str) -> None:
        self.expected = expected.removeprefix("SHA256:").strip()

    def missing_host_key(self, client, hostname, key) -> None:
        actual = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")
        if actual != self.expected:
            raise PermanentError(
                f"SSH host key mismatch for {hostname}; expected SHA256:{self.expected}, got SHA256:{actual}"
            )


class EsxiSshExecutor:
    """Key-checked, allowlisted SSH execution for vmkfstools disk cloning only."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _connect(self):
        try:
            import paramiko
        except ImportError as exc:
            raise PermanentError("SSH fallback requires the Paramiko dependency") from exc
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self.settings.esxi_ssh_host_key_sha256.strip():
            client.set_missing_host_key_policy(_PinnedHostKeyPolicy(self.settings.esxi_ssh_host_key_sha256))
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        password = self.settings.esxi_ssh_password
        if not password and self.settings.esxi_ssh_use_login_password:
            password = self.settings.vcenter_password
        key_path = self.settings.esxi_ssh_key_path
        key_filename = str(key_path) if str(key_path) not in {"", "."} and key_path.is_file() else None
        jump_client = None
        sock = None
        try:
            if self.settings.access_jump_enabled:
                if not self.settings.access_jump_password:
                    raise PermanentError("The saved jump-host credential is unavailable")
                from .guest_tools import _open_jump_channel

                jump_client, sock = _open_jump_channel(
                    self.settings.vcenter_host,
                    self.settings.esxi_ssh_port,
                    {
                        "address": self.settings.access_jump_address,
                        "port": self.settings.access_jump_port,
                        "username": self.settings.access_jump_user,
                        "password": self.settings.access_jump_password,
                        "host_key_sha256": self.settings.access_jump_host_key_sha256,
                    },
                    self.settings.esxi_ssh_timeout_seconds,
                )
            client.connect(
                hostname=self.settings.vcenter_host,
                port=self.settings.esxi_ssh_port,
                username=self.settings.esxi_ssh_user or self.settings.vcenter_user,
                password=password or None,
                key_filename=key_filename,
                timeout=self.settings.esxi_ssh_timeout_seconds,
                banner_timeout=self.settings.esxi_ssh_timeout_seconds,
                auth_timeout=self.settings.esxi_ssh_timeout_seconds,
                allow_agent=True,
                look_for_keys=True,
                sock=sock,
            )
        except PermanentError as exc:
            client.close()
            if jump_client is not None:
                jump_client.close()
            message = str(exc)
            for secret in (password, self.settings.access_jump_password):
                if secret:
                    message = message.replace(secret, "[redacted]")
            raise PermanentError(message) from exc
        except Exception as exc:
            client.close()
            if jump_client is not None:
                jump_client.close()
            message = str(exc)
            for secret in (password, self.settings.access_jump_password):
                if secret:
                    message = message.replace(secret, "[redacted]")
            raise TransientError(f"Could not establish verified SSH connection to ESXi: {message}") from exc
        return client, jump_client

    @staticmethod
    def _run(client, argv: list[str], timeout: int) -> str:
        command = " ".join(shlex.quote(item) for item in argv)
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        status = stdout.channel.recv_exit_status()
        output = (stdout.read() + stderr.read()).decode("utf-8", errors="replace")[-8192:]
        if status:
            raise PermanentError(f"ESXi command failed ({status}): {output.strip()}")
        return output.strip()

    def clone_disk(self, source: str, destination: str, target: str) -> None:
        formats = {
            "thin": "thin",
            "thick": "zeroedthick",
            "lazy_zeroed_thick": "zeroedthick",
            "eager_zeroed_thick": "eagerzeroedthick",
        }
        if target not in formats:
            raise PermanentError("Unsupported vmkfstools target")
        client, jump_client = self._connect()
        try:
            try:
                self._run(client, ["test", "-s", destination], timeout=30)
                return
            except PermanentError:
                pass
            self._run(client, ["vmkfstools", "-i", source, destination, "-d", formats[target]], timeout=7200)
            self._run(client, ["test", "-s", destination], timeout=30)
        finally:
            client.close()
            if jump_client is not None:
                jump_client.close()
