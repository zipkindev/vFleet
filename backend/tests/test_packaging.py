from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import _packaged_data_dir
from app.desktop import _open_job_count, _request_shutdown, _listener


def test_packaged_data_dir_windows() -> None:
    result = _packaged_data_dir("win32", {"LOCALAPPDATA": r"C:\Users\Example\AppData\Local"}, Path("home"))
    assert result == Path(r"C:\Users\Example\AppData\Local") / "vFleet"


def test_packaged_data_dir_macos() -> None:
    assert _packaged_data_dir("darwin", {}, Path("/Users/example")) == Path(
        "/Users/example/Library/Application Support/vFleet"
    )


def test_packaged_data_dir_linux_xdg() -> None:
    assert _packaged_data_dir("linux", {"XDG_DATA_HOME": "/srv/user-data"}, Path("/home/example")) == Path(
        "/srv/user-data/vfleet"
    )


def test_packaged_data_dir_linux_default() -> None:
    assert _packaged_data_dir("linux", {}, Path("/home/example")) == Path("/home/example/.local/share/vfleet")


@pytest.mark.parametrize("host", ["0.0.0.0", "::1"])
def test_desktop_listener_refuses_unsupported_bind(host: str) -> None:
    with pytest.raises(SystemExit, match="only bind to loopback"):
        _listener(host, 0)


def test_desktop_shutdown_refuses_open_jobs_unless_forced() -> None:
    store = SimpleNamespace(open_jobs=lambda: [object(), object()])
    app = SimpleNamespace(state=SimpleNamespace(store=store))
    server = SimpleNamespace(should_exit=False)

    assert _open_job_count(app) == 2
    assert _request_shutdown(app, server) == 2
    assert server.should_exit is False
    assert _request_shutdown(app, server, force=True) == 0
    assert server.should_exit is True
