from __future__ import annotations

import re
from typing import Iterable, Union

ErrorLike = Union[BaseException, str]

TRANSIENT_MARKERS = (
    "timed out",
    "timeout",
    "temporar",
    "connection reset",
    "connection aborted",
    "connection refused",
    "broken pipe",
    "unreachable",
    "not authenticated",
    "session is not authenticated",
    "invalid session",
    "unable to connect",
    "name or service not known",
    "nodename nor servname",
    "network is down",
    "network is unreachable",
    "ssl",
    "eof occurred",
    "503",
    "502",
    "504",
    "would block",
    "try again",
    "temporarily unavailable",
    "serverfault",
    "server fault",
    "soap",
    "vim.fault.notauthenticated",
    "failed to connect",
    "remote end closed",
)

PERMANENT_MARKERS = (
    "already exists",
    "duplicate",
    "insufficient",
    "no permission",
    "nopermission",
    "privilegeid",
    "permission to perform",
    "permission denied",
    "was denied",
    "invalid name",
    "invalidargument",
    "tools is not",
    "unsupported",
    "out of space",
    "restricted",
    "unknown template",
    "unknown datastore",
    "unknown host",
    "unknown network",
    "unknown vm",
    "same cluster",
    "not in cluster",
)


class TransientError(Exception):
    """VPN / session / network failure that should be retried."""


class PermanentError(Exception):
    """Operator or inventory error that should not be retried."""


def _haystack(exc: BaseException) -> str:
    parts = [type(exc).__name__, str(exc)]
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        parts.append(type(cause).__name__)
        parts.append(str(cause))
    return " ".join(parts).lower()


def is_permanent(exc: BaseException, extra: Iterable[str] = ()) -> bool:
    if isinstance(exc, PermanentError):
        return True
    if isinstance(exc, KeyError):
        return True
    text = _haystack(exc)
    return any(marker in text for marker in (*PERMANENT_MARKERS, *extra))


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, TransientError):
        return True
    if isinstance(exc, PermanentError):
        return False
    if is_permanent(exc):
        return False
    text = _haystack(exc)
    return any(marker in text for marker in TRANSIENT_MARKERS)


def is_not_authenticated(exc: ErrorLike) -> bool:
    text = (str(exc) if not isinstance(exc, BaseException) else _haystack(exc)).lower()
    return "notauthenticated" in text.replace(" ", "") or "session is not authenticated" in text


_PRIVILEGE_HINTS = {
    "resource.coldmigrate": "Cold migrate (powered-off VM or disk convert while off). Grant Resource.ColdMigrate on the VM/folder.",
    "resource.hotmigrate": "vMotion (powered-on VM). Grant Resource.HotMigrate on the VM/folder.",
    "resource.migrate": "Migrate. Grant Resource.Migrate on the VM/folder.",
    "datastore.relocate": "Storage vMotion / relocate disks. Grant Datastore.Relocate on the datastore.",
    "datastore.allocatespace": "Allocate datastore space. Grant Datastore.AllocateSpace.",
    "network.assign": "Change VM network. Grant Network.Assign.",
    "virtualmachine.interact.poweron": "Power on. Grant VirtualMachine.Interact.PowerOn.",
    "virtualmachine.interact.poweroff": "Power off. Grant VirtualMachine.Interact.PowerOff.",
    "virtualmachine.inventory.createfromexisting": "Clone. Grant VirtualMachine.Inventory.CreateFromExisting.",
}


def _privilege_message(raw: str) -> str:
    match = re.search(r"privilegeId\s*=\s*'([^']+)'", raw) or re.search(r"privilegeIds\s*=\s*\(str\)\s*\[\s*'([^']+)'", raw)
    if not match:
        return ""
    privilege = match.group(1).strip()
    hint = _PRIVILEGE_HINTS.get(privilege.lower(), f"Grant {privilege} on the object in vCenter Roles.")
    return f"vCenter denied {privilege}. {hint}"


def humanize_vcenter_error(exc: ErrorLike, *, host: str = "") -> str:
    """Turn SOAP dumps / nginx HTML into a short UI-safe message."""
    raw = str(exc).strip() if not isinstance(exc, BaseException) else str(exc).strip()
    if not raw:
        return "Could not reach vCenter"
    lower = raw.lower()
    where = f" ({host})" if host else ""

    if "<html" in lower or "<!doctype" in lower or "nginx/" in lower:
        code = ""
        title = re.search(r"<title>([^<]+)</title>", raw, re.I)
        heading = re.search(r"<h1>([^<]+)</h1>", raw, re.I)
        label = (title.group(1) if title else "") or (heading.group(1) if heading else "")
        label = re.sub(r"\s+", " ", label).strip()
        if re.search(r"\b404\b", label) or "404 not found" in lower:
            code = "404"
        elif re.search(r"\b502\b", label) or "bad gateway" in lower:
            code = "502"
        elif re.search(r"\b503\b", label) or "service unavailable" in lower:
            code = "503"
        elif re.search(r"\b403\b", label) or "forbidden" in lower:
            code = "403"
        if code == "404":
            return (
                f"vCenter{where} answered HTTP 404 for the API path "
                "(UI may still work). Check host/port or try again after VPN settles."
            )
        if code:
            return f"vCenter{where} returned HTTP {code}" + (f" ({label})" if label else "")
        return f"vCenter{where} returned an HTML error page instead of the API"

    if is_not_authenticated(exc):
        return (
            f"vCenter API session expired{where}. "
            "Reconnecting; use Connect if inventory stays stale."
        )

    privilege = _privilege_message(raw)
    if privilege:
        return privilege

    if "nopermission" in lower.replace(" ", "") or "permission to perform this operation was denied" in lower:
        return "vCenter denied permission for this operation. The account needs a role with migrate/relocate privileges."

    msg = re.search(r"\bmsg\s*=\s*'([^']+)'", raw)
    if msg and ("vim.fault" in lower or "vmodl." in lower or "fault" in lower):
        return msg.group(1).rstrip(".")

    if "cannot complete login" in lower or "incorrect user name or password" in lower:
        return "Invalid vCenter username or password"
    if "certificate" in lower and ("verify" in lower or "ssl" in lower or "tls" in lower):
        return "TLS certificate error — enable “Trust self-signed certificate” or install the CA"
    if "connection refused" in lower:
        return f"Connection refused{where}"
    if "timed out" in lower or "timeout" in lower:
        return f"Timed out reaching vCenter{where}"
    if "name or service not known" in lower or "nodename nor servname" in lower:
        return f"Host not found{where}"

    cleaned = re.sub(r"\s+", " ", raw)
    if len(cleaned) > 240:
        cleaned = cleaned[:237].rstrip() + "…"
    return cleaned
