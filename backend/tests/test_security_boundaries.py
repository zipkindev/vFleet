from pathlib import Path

from app import main as main_module


def test_spa_fallback_never_maps_untrusted_routes_to_disk(monkeypatch, tmp_path):
    frontend = tmp_path / "frontend" / "dist"
    monkeypatch.setattr(main_module, "FRONTEND_DIST", frontend)

    response = main_module._spa_index_response("../../.env")

    assert Path(response.path) == frontend / "index.html"
