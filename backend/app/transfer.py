from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

import httpx

from .config import Settings
from .errors import PermanentError, TransientError, is_transient

ProgressFn = Callable[[int, Dict[str, Any]], None]


class OffsetReader:
    """File-like byte iterator so httpx 0.28+ can stream `content=`."""

    def __init__(self, path: Path, start: int, on_read: Optional[Callable[[int], None]] = None) -> None:
        self._handle = path.open("rb")
        self._handle.seek(start)
        self._sent = start
        self._on_read = on_read

    def read(self, size: int = 1024 * 1024) -> bytes:
        data = self._handle.read(size)
        if data:
            self._sent += len(data)
            if self._on_read:
                self._on_read(self._sent)
        return data

    def __iter__(self):
        while True:
            chunk = self.read()
            if not chunk:
                break
            yield chunk

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "OffsetReader":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class VCenterRest:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: Optional[httpx.Client] = None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        base = f"https://{self.settings.vcenter_host}:{self.settings.vcenter_port}"
        timeout = httpx.Timeout(self.settings.connect_timeout_seconds, read=120, write=120)
        client = httpx.Client(base_url=base, verify=not self.settings.vcenter_insecure, timeout=timeout)
        try:
            response = client.post("/api/session", auth=(self.settings.vcenter_user, self.settings.vcenter_password))
            if response.status_code in {200, 201}:
                token = response.json()
                if isinstance(token, str) and token:
                    client.headers["vmware-api-session-id"] = token
                    self._client = client
                    return client
            response = client.post(
                "/rest/com/vmware/cis/session",
                auth=(self.settings.vcenter_user, self.settings.vcenter_password),
            )
            response.raise_for_status()
            payload = response.json()
            token = payload.get("value", payload) if isinstance(payload, dict) else payload
            client.headers["vmware-api-session-id"] = str(token)
            self._client = client
            return client
        except Exception:
            client.close()
            raise

    def reset(self) -> None:
        self.close()


def put_file(
    url: str,
    local_path: Path,
    start: int,
    total: int,
    headers: Dict[str, str],
    verify: bool,
    on_progress: Optional[ProgressFn] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> int:
    remaining = total - start
    if remaining <= 0:
        return total
    hdrs = dict(headers)
    hdrs["Content-Length"] = str(remaining)
    hdrs.setdefault("Content-Type", "application/octet-stream")
    if start > 0:
        hdrs["Content-Range"] = f"bytes {start}-{total - 1}/{total}"

    sent = start
    state = extra or {}

    def mark(value: int) -> None:
        nonlocal sent
        sent = value
        if on_progress:
            on_progress(value, state)

    try:
        with OffsetReader(local_path, start, mark) as body:
            timeout = httpx.Timeout(30.0, read=300.0, write=300.0, connect=30.0)
            with httpx.Client(verify=verify, timeout=timeout, follow_redirects=True) as client:
                response = client.put(url, headers=hdrs, content=body)
        if response.status_code in {200, 201, 204}:
            mark(total)
            return total
        if response.status_code in {400, 411, 501} and start > 0:
            raise TransientError(f"Remote did not accept resume at byte {start}; will retry from 0")
        if response.status_code in {401, 403}:
            raise TransientError(f"Upload auth failed ({response.status_code})")
        if response.status_code >= 500:
            raise TransientError(f"Upload server error {response.status_code}")
        raise PermanentError(f"Upload rejected ({response.status_code}): {response.text[:300]}")
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        raise TransientError(str(exc)) from exc


def library_upload(
    rest: VCenterRest,
    datastore_id: str,
    filename: str,
    local_path: Path,
    size: int,
    extra: Dict[str, Any],
    on_progress: Optional[ProgressFn] = None,
) -> Dict[str, Any]:
    client = rest.client()
    library_id = extra.get("library_id") or _ensure_library(client, datastore_id)
    extra["library_id"] = library_id
    item_id = extra.get("library_item_id") or _create_item(client, library_id, filename)
    extra["library_item_id"] = item_id
    session_id = extra.get("update_session_id") or _create_session(client, item_id)
    extra["update_session_id"] = session_id

    file_info = _add_or_get_file(client, session_id, filename, size)
    extra["library_file"] = filename
    transferred = int(file_info.get("bytes_transferred") or extra.get("bytes_sent") or 0)
    endpoint = _upload_url(file_info)
    if not endpoint:
        raise PermanentError("Content library did not return an upload endpoint")

    if transferred < size:
        headers = {"vmware-api-session-id": client.headers.get("vmware-api-session-id", "")}
        put_file(
            endpoint,
            local_path,
            transferred,
            size,
            headers,
            verify=not rest.settings.vcenter_insecure,
            on_progress=on_progress,
            extra=extra,
        )

    complete = client.post(f"/api/content/library/item/update-session/{session_id}?action=complete")
    if complete.status_code >= 400:
        complete = client.post(f"/rest/com/vmware/content/library/item/update-session/id:{session_id}?~action=complete")
    if complete.status_code >= 400:
        raise TransientError(f"Could not complete library session: {complete.text[:200]}")
    extra["bytes_sent"] = size
    extra["phase"] = "library_complete"
    return extra


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return {}


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "value" in payload and len(payload) <= 2:
        return payload["value"]
    return payload


def _ensure_library(client: httpx.Client, datastore_id: str) -> str:
    response = client.get("/api/content/local-library")
    if response.status_code == 200:
        for lib_id in _unwrap(response.json()) or []:
            info = _unwrap(_json(client.get(f"/api/content/local-library/{lib_id}")))
            if isinstance(info, dict) and info.get("name") == "vfleet-relay":
                return str(lib_id)
    spec = {
        "name": "vfleet-relay",
        "description": "Resumable uploads from the local vFleet relay",
        "type": "LOCAL",
        "storage_backings": [{"type": "DATASTORE", "datastore_id": datastore_id}],
    }
    created = client.post("/api/content/local-library", json=spec)
    if created.status_code in {200, 201}:
        return str(_unwrap(created.json()))
    created = client.post("/rest/com/vmware/content/local-library", json={"create_spec": spec})
    if created.status_code in {200, 201}:
        return str(_unwrap(created.json()))
    raise PermanentError(f"Cannot create content library (need Content Library privileges): {created.text[:240]}")


def _create_item(client: httpx.Client, library_id: str, filename: str) -> str:
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    item_type = {"iso": "iso", "ovf": "ovf", "ova": "ovf", "vmdk": "ovf"}.get(suffix, "iso" if suffix == "iso" else "other")
    spec = {"library_id": library_id, "name": filename.rsplit(".", 1)[0][:80] or filename, "type": item_type}
    created = client.post("/api/content/library/item", json=spec)
    if created.status_code in {200, 201}:
        return str(_unwrap(created.json()))
    created = client.post("/rest/com/vmware/content/library/item", json={"create_spec": spec})
    if created.status_code in {200, 201}:
        return str(_unwrap(created.json()))
    raise PermanentError(f"Cannot create library item: {created.text[:240]}")


def _create_session(client: httpx.Client, item_id: str) -> str:
    spec = {"library_item_id": item_id}
    created = client.post("/api/content/library/item/update-session", json=spec)
    if created.status_code in {200, 201}:
        return str(_unwrap(created.json()))
    created = client.post("/rest/com/vmware/content/library/item/update-session", json={"create_spec": spec})
    if created.status_code in {200, 201}:
        return str(_unwrap(created.json()))
    raise TransientError(f"Cannot create library update session: {created.text[:240]}")


def _add_or_get_file(client: httpx.Client, session_id: str, filename: str, size: int) -> Dict[str, Any]:
    listed = client.get(f"/api/content/library/item/update-session/{session_id}/file")
    if listed.status_code == 200:
        files = _unwrap(listed.json()) or []
        for item in files:
            if isinstance(item, dict) and item.get("name") == filename:
                return item
            if isinstance(item, str) and item == filename:
                detail = client.get(f"/api/content/library/item/update-session/{session_id}/file/{quote(filename)}")
                if detail.status_code == 200:
                    payload = _unwrap(detail.json())
                    if isinstance(payload, dict):
                        return payload
    spec = {"name": filename, "source_type": "PUSH", "size": size}
    added = client.post(
        f"/api/content/library/item/update-session/{session_id}/file?action=add",
        json=spec,
    )
    if added.status_code in {200, 201}:
        payload = _unwrap(added.json())
        if isinstance(payload, dict):
            return payload
    added = client.post(
        f"/rest/com/vmware/content/library/item/update-session/id:{session_id}/file?~action=add",
        json={"file_spec": spec},
    )
    if added.status_code in {200, 201}:
        payload = _unwrap(added.json())
        if isinstance(payload, dict):
            return payload
    raise TransientError(f"Cannot add library upload file: {added.text[:240]}")


def _upload_url(info: Dict[str, Any]) -> str:
    endpoint = info.get("upload_endpoint") or info.get("uploadEndpoint") or {}
    if isinstance(endpoint, dict):
        return str(endpoint.get("uri") or endpoint.get("url") or "")
    return str(info.get("uri") or "")


def classify_http(exc: BaseException) -> BaseException:
    if is_transient(exc):
        return TransientError(str(exc))
    return exc
