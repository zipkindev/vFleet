from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

from fastapi.testclient import TestClient

from app.adapters.demo import DemoAdapter
from app.config import Settings
from app.main import app
from app.metrics import HISTORY_DAYS, build_metric_samples, synthesize_history
from app.store import LocalStore


def test_metric_samples_include_hosts_and_datastores():
    adapter = DemoAdapter(Settings(app_mode="demo"))
    snapshot = adapter.snapshot()
    catalog = adapter.list_datastores()
    from app.models import Catalog

    samples = build_metric_samples(snapshot, Catalog(datastores=catalog))
    assert any(item.owner_key == "" and item.host_id == "" and item.datastore_id == "" for item in samples)
    assert any(item.host_id == "host-esx01" for item in samples)
    assert any(item.datastore_id == "ds-lab" for item in samples)
    host = next(item for item in samples if item.host_id == "host-esx01")
    assert 0 <= host.cpu_pct <= 100
    store = next(item for item in samples if item.datastore_id == "ds-lab")
    assert store.disk_pct > 0


def test_synthesized_history_covers_two_weeks(tmp_path: Path):
    adapter = DemoAdapter(Settings(app_mode="demo"))
    snapshot = adapter.snapshot()
    from app.models import Catalog

    catalog = Catalog(datastores=adapter.list_datastores())
    current = build_metric_samples(snapshot, catalog)
    now = datetime.now(timezone.utc)
    history = synthesize_history(current, days=HISTORY_DAYS, now=now)
    cluster = [item for item in history if item.owner_key == "" and item.host_id == "" and item.datastore_id == ""]
    assert cluster
    span = cluster[-1].ts - cluster[0].ts
    assert span >= timedelta(days=13)
    store = LocalStore(tmp_path / "metrics.db")
    store.record_metrics(history)
    points = store.load_metrics(hours=336)
    assert len(points) > 200
    host_points = store.load_metrics(hours=336, host_id="host-esx01")
    assert host_points
    disk_points = store.load_metrics(hours=336, datastore_id="ds-lab")
    assert disk_points
    assert disk_points[-1].disk_pct > 0
    window = store.load_metrics(hours=336, since=now - timedelta(hours=6), until=now)
    assert window
    assert all(now - timedelta(hours=6) <= point.ts <= now for point in window)
    store.close()


def test_metrics_api_host_datastore_and_two_week_window():
    with TestClient(app) as client:
        payload = None
        for _ in range(80):
            payload = client.get("/api/metrics?hours=336").json()
            if payload["points"] > 20:
                break
            time.sleep(0.05)
        assert payload is not None
        assert payload["hours"] == 336
        assert payload["points"] > 20
        inventory = client.get("/api/inventory").json()
        host_id = inventory["hosts"][0]["id"]
        host_payload = client.get("/api/metrics", params={"hours": 336, "host": host_id}).json()
        assert host_payload["host"] == host_id
        assert host_payload["points"] > 0
        catalog = client.get("/api/catalog").json()
        datastore_id = catalog["datastores"][0]["id"]
        disk_payload = client.get("/api/metrics", params={"hours": 336, "datastore": datastore_id}).json()
        assert disk_payload["datastore"] == datastore_id
        assert disk_payload["points"] > 0
        limited = client.get("/api/metrics?hours=1").json()
        assert limited["points"] <= payload["points"]
