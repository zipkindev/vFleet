from pathlib import Path

import httpx

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
    assert progress[-1] == 10
