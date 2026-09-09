from __future__ import annotations

import hashlib
from collections.abc import Callable
import io
import re
import shlex
import tarfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from .errors import PermanentError, TransientError
from .esxi_ssh import EsxiSshExecutor


class UpgradeSsh(EsxiSshExecutor):
    """Fixed-purpose ESXi upgrade preparation/recovery; never runs installer code."""

    def __init__(self, settings, before_connect: Callable[[], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(settings)
        self.before_connect = before_connect
        self.sleep = sleep
        self._client = None
        self._jump = None

    def __enter__(self):
        return self

    def __exit__(self, _kind, _value, _traceback) -> None:
        self.close()

    def close(self) -> None:
        client, jump = self._client, self._jump
        self._client = None
        self._jump = None
        if client is not None:
            client.close()
        if jump is not None:
            jump.close()

    def _session(self):
        if self._client is not None:
            try:
                transport = self._client.get_transport()
                if transport is not None and transport.is_active() and transport.is_authenticated():
                    return self._client, self._jump
            except Exception:
                pass
            self.close()

        last_error = None
        # ESXi reports TSM-SSH running before sshd consistently accepts a
        # banner/authentication. Check SOAP before every connection attempt,
        # then absorb that short readiness window inside this job instead of
        # consuming relay retries or opening a new login for every command.
        for attempt in range(12):
            if self.before_connect is not None:
                self.before_connect()
            try:
                self._client, self._jump = super()._connect()
                return self._client, self._jump
            except TransientError as exc:
                last_error = exc
                if attempt == 11:
                    raise
                self.sleep(2)
        raise last_error or TransientError("ESXi SSH service did not become ready")

    def command(self, argv: list[str], timeout: int = 90, limit: int = 2 * 1024**2) -> bytes:
        client, _jump = self._session()
        try:
            _stdin, stdout, stderr = client.exec_command(" ".join(shlex.quote(x) for x in argv), timeout=timeout)
            channel = stdout.channel
            output, errors = bytearray(), bytearray()
            deadline = time.monotonic() + timeout
            # Drain both streams before waiting for exit: SSH channel buffers can fill.
            while True:
                while channel.recv_ready():
                    output.extend(channel.recv(65536))
                    if len(output) + len(errors) > limit:
                        raise PermanentError("ESXi upgrade command output exceeded its limit")
                while channel.recv_stderr_ready():
                    errors.extend(channel.recv_stderr(65536))
                    if len(output) + len(errors) > limit:
                        raise PermanentError("ESXi upgrade command output exceeded its limit")
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() >= deadline:
                    raise TransientError("ESXi upgrade command timed out; reconcile host state before continuing")
                time.sleep(0.02)
            if channel.recv_exit_status() != 0:
                # Raw command output may include host configuration or credentials.
                raise PermanentError("ESXi rejected an upgrade workflow command; inspect the host console")
            return bytes(output)
        except TransientError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise TransientError(
                f"Verified ESXi SSH session was interrupted before the command completed: {type(exc).__name__}"
            ) from exc

    def text(self, argv: list[str], timeout: int = 90) -> str:
        return self.command(argv, timeout).decode("utf-8", errors="strict").strip()

    def identity(self) -> dict:
        uuid = self.text(["esxcfg-info", "-u"]).lower()
        if not re.fullmatch(r"[0-9a-f-]{36}", uuid):
            raise PermanentError("Cannot establish the ESXi hardware UUID over SSH")
        version = self.text(["vmware", "-vl"])
        found = re.search(r"VMware ESXi (\d+\.\d+\.\d+) build-(\d+)", version)
        if not found:
            raise PermanentError("Cannot identify the running ESXi version over SSH")
        state = self.text(["esxcli", "system", "maintenanceMode", "get"]).lower()
        if state not in {"enabled", "disabled"}:
            raise PermanentError("Cannot determine maintenance mode")
        return {"uuid": uuid, "version": found[1], "build": found[2], "maintenance_mode": state == "enabled"}

    @staticmethod
    def vm_id(value: str) -> str:
        if not re.fullmatch(r"[0-9]+", value):
            raise PermanentError("Invalid ESXi VM identifier")
        return value

    def inventory(self) -> list[dict]:
        listing = self.text(["vim-cmd", "vmsvc/getallvms"])
        if "invalid" in listing.lower() or "skipping" in listing.lower():
            raise PermanentError("Resolve inaccessible or invalid VM registrations before upgrading")
        ids = re.findall(r"(?m)^\s*(\d+)\s+", listing)
        rows = []
        for ident in ids:
            config = self.text(["vim-cmd", "vmsvc/get.config", self.vm_id(ident)])
            uuid = re.search(r'(?m)^\s*uuid = "([^"]+)"', config)
            vmx = re.search(r'(?m)^\s*vmPathName = "([^"\r\n]+)"', config)
            if uuid is None or vmx is None:
                raise PermanentError("Cannot establish a stable VM identity from ESXi")
            rows.append({"id": ident, "uuid": uuid[1].lower(), "vmx": vmx[1], "power_state": self.power_state(ident)})
        if len({r["uuid"] for r in rows}) != len(rows):
            raise PermanentError("Duplicate VM UUIDs prevent safe automatic restoration")
        return rows

    def power_state(self, ident: str) -> str:
        value = self.text(["vim-cmd", "vmsvc/power.getstate", self.vm_id(ident)])
        for raw, state in [("Powered on", "POWERED_ON"), ("Powered off", "POWERED_OFF"), ("Suspended", "SUSPENDED")]:
            if value.splitlines()[-1:] == [raw]:
                return state
        raise PermanentError("Cannot determine VM power state")

    def shutdown(self, ident: str) -> None:
        self.text(["vim-cmd", "vmsvc/power.shutdown", self.vm_id(ident)])

    def power_on(self, ident: str) -> None:
        self.text(["vim-cmd", "vmsvc/power.on", self.vm_id(ident)])

    def maintenance(self, enabled: bool) -> None:
        self.text(["esxcli", "system", "maintenanceMode", "set", "--enable", str(enabled).lower(), "--timeout", "120"], timeout=150)

    def backup(self, destination: Path) -> str:
        self.text(["vim-cmd", "hostsvc/firmware/sync_config"])
        reply = self.text(["vim-cmd", "hostsvc/firmware/backup_config"])
        urls = re.findall(r"https?://[^\s\"']+", reply)
        if len(urls) != 1:
            raise PermanentError("Cannot locate the generated configuration backup")
        path = urlsplit(urls[0]).path
        if not re.fullmatch(r"/downloads/[A-Za-z0-9_-]+/configBundle-[A-Za-z0-9_.-]+\.tgz", path):
            raise PermanentError("Unexpected configuration backup location")
        data = self.command(["cat", "/scratch" + path], limit=64 * 1024**2)
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                if archive.next() is None:
                    raise ValueError("empty")
        except (tarfile.TarError, ValueError) as exc:
            raise PermanentError("Host configuration backup is not a readable archive") from exc
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with destination.open("wb") as stream:
            destination.chmod(0o600)
            stream.write(data)
            stream.flush()
            import os
            os.fsync(stream.fileno())
        return hashlib.sha256(data).hexdigest()
