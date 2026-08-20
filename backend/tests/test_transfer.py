from pathlib import Path

import httpx
import pytest

from app.errors import PermanentError
from app.transfer import OffsetReader, put_file


def test_offset_reader_is_valid_httpx_content(tmp_path: Path):
    path = tmp_path / "iso.bin"
    path.write_bytes(b"0123456789")
    seen: list[int] = []
    with OffsetReader(path, start=4, on_read=seen.append) as body:
        request = httpx.Request("PUT", "https://example.com/upload", content=body)
        assert request.read() == b"456789"
    assert seen[-1] == 10


def test_put_file_streams_from_offset(tmp_path: Path, monkeypatch):
    path = tmp_path / "iso.bin"
    path.write_bytes(b"abcdefghij")
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 201
        text = ""

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def put(self, url, headers=None, content=None):
            request = httpx.Request("PUT", url, headers=headers, content=content)
            captured["body"] = request.read()
            captured["headers"] = request.headers
            return FakeResponse()

    monkeypatch.setattr("app.transfer.httpx.Client", FakeClient)
    progress: list[int] = []
    sent = put_file(url="https://vc.example/folder/file.iso", local_path=path, start=3, total=10, headers={}, verify=False, on_progress=lambda value, _state: progress.append(value))
    assert sent == 10
    assert captured["body"] == b"defghij"
    assert captured["headers"]["content-length"] == "7"
    assert captured["headers"]["content-range"] == "bytes 3-9/10"
    assert captured["headers"]["overwrite"] == "t"
    assert progress[-1] == 10


def test_put_file_keeps_put_across_vcenter_303(tmp_path: Path, monkeypatch):
    path = tmp_path / "iso.bin"
    path.write_bytes(b"abcdefghij")
    calls: list[dict] = []
    client_kwargs: dict = {}

    class FakeResponse:
        def __init__(self, status_code: int, url: str, location: str = ""):
            self.status_code = status_code
            self.text = ""
            self.headers = {"Location": location} if location else {}
            self.url = url

    class FakeClient:
        def __init__(self, **kwargs):
            client_kwargs.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def put(self, url, headers=None, content=None):
            request = httpx.Request("PUT", url, headers=headers, content=content)
            calls.append({"url": url, "body": request.read(), "headers": dict(request.headers)})
            if "vc.example" in url:
                return FakeResponse(303, url, "https://esxi.example/folder/file.iso?ticket=abc")
            return FakeResponse(201, url)

    monkeypatch.setattr("app.transfer.httpx.Client", FakeClient)
    sent = put_file(
        url="https://vc.example/folder/file.iso",
        local_path=path,
        start=0,
        total=10,
        headers={"Cookie": "vmware_soap_session=secret"},
        verify=False,
    )
    assert sent == 10
    assert client_kwargs.get("follow_redirects") is False
    assert [row["url"] for row in calls] == [
        "https://vc.example/folder/file.iso",
        "https://esxi.example/folder/file.iso?ticket=abc",
    ]
    assert calls[1]["body"] == b"abcdefghij"
    assert "cookie" not in {key.lower() for key in calls[1]["headers"]}


def test_put_file_404_without_redirect_is_permanent(tmp_path: Path, monkeypatch):
    path = tmp_path / "iso.bin"
    path.write_bytes(b"abcd")

    class FakeResponse:
        status_code = 404
        text = ""
        headers: dict = {}
        url = "https://vc.example/folder/missing.iso"

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def put(self, url, headers=None, content=None):
            return FakeResponse()

    monkeypatch.setattr("app.transfer.httpx.Client", FakeClient)
    with pytest.raises(PermanentError, match=r"Upload rejected \(404\) at vc.example/folder/missing.iso"):
        put_file(url="https://vc.example/folder/missing.iso", local_path=path, start=0, total=4, headers={}, verify=False)


class _Mount:
    def __init__(self, mode: str, host: str = "esxi-1", connected: bool = True):
        self.mountInfo = type("Info", (), {"accessMode": mode})()
        self.key = type(
            "Host",
            (),
            {"name": host, "runtime": type("RT", (), {"connectionState": "connected" if connected else "disconnected", "inMaintenanceMode": False})()},
        )()


def test_readonly_mounts_block_uploads():
    from app.adapters.vcenter import datastore_is_readonly, first_writable_host_name, folder_file_url

    assert datastore_is_readonly([_Mount("readOnly"), _Mount("readOnly")])
    assert not datastore_is_readonly([_Mount("readWrite")])
    assert first_writable_host_name(type("DS", (), {"host": [_Mount("readOnly"), _Mount("readWrite", "10.1.2.3")]})()) == "10.1.2.3"
    assert first_writable_host_name(type("DS", (), {"host": [_Mount("readOnly")]})()) == ""
    assert folder_file_url("esxi.lab", 443, "Linux/a.iso", "ha-datacenter", "ISO") == (
        "https://esxi.lab/folder/Linux/a.iso?dcPath=ha-datacenter&dsName=ISO"
    )
