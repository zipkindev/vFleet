from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import Catalog, InventorySnapshot, Job, MetricPoint, StagingSession

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    progress TEXT NOT NULL,
    result TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 80,
    next_run_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL DEFAULT '',
    endpoint_fingerprint TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS jobs_status_run ON jobs (status, next_run_at);
CREATE INDEX IF NOT EXISTS jobs_idem ON jobs (idempotency_key);
CREATE TABLE IF NOT EXISTS staging (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    received INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    owner_key TEXT NOT NULL DEFAULT '',
    cpu_pct REAL NOT NULL,
    memory_pct REAL NOT NULL,
    disk_pct REAL NOT NULL,
    cpu_usage_mhz INTEGER NOT NULL DEFAULT 0,
    memory_usage_mib INTEGER NOT NULL DEFAULT 0,
    storage_bytes INTEGER NOT NULL DEFAULT 0,
    vm_count INTEGER NOT NULL DEFAULT 0,
    host_id TEXT NOT NULL DEFAULT '',
    datastore_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS metrics_ts_owner ON metrics (owner_key, ts);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def backoff_seconds(attempts: int) -> int:
    return min(300, 2 ** min(max(attempts, 1), 8))


class LocalStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.staging_dir = path.parent / "staging"
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._migrate_jobs()
            self._migrate_metrics()
            self._conn.commit()
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate_metrics(self) -> None:
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(metrics)").fetchall()}
        if "host_id" not in cols:
            self._conn.execute("ALTER TABLE metrics ADD COLUMN host_id TEXT NOT NULL DEFAULT ''")
        if "datastore_id" not in cols:
            self._conn.execute("ALTER TABLE metrics ADD COLUMN datastore_id TEXT NOT NULL DEFAULT ''")
        self._conn.execute("CREATE INDEX IF NOT EXISTS metrics_scope_ts ON metrics (owner_key, host_id, datastore_id, ts)")

    def _migrate_jobs(self) -> None:
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "endpoint_fingerprint" not in cols:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN endpoint_fingerprint TEXT NOT NULL DEFAULT ''")
        rows = self._conn.execute("SELECT id, payload FROM jobs WHERE endpoint_fingerprint=''").fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
                fingerprint = str(payload.get("_endpoint_fingerprint") or "")
            except (TypeError, ValueError):
                fingerprint = ""
            if fingerprint:
                self._conn.execute(
                    "UPDATE jobs SET endpoint_fingerprint=? WHERE id=?",
                    (fingerprint, row["id"]),
                )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS jobs_endpoint_updated ON jobs (endpoint_fingerprint, updated_at)"
        )

    def history_seeded(self) -> bool:
        return self._get_kv("metrics_history_seeded") is not None

    def mark_history_seeded(self) -> None:
        self._put_kv("metrics_history_seeded", "1")

    def _put_kv(self, key: str, payload: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, payload, _iso(_now())),
            )
            self._conn.commit()

    def _get_kv(self, key: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT value, updated_at FROM kv WHERE key=?", (key,)).fetchone()

    def save_inventory(self, snapshot: InventorySnapshot) -> None:
        self._put_kv("inventory", snapshot.model_dump_json())

    def load_inventory(self) -> Optional[InventorySnapshot]:
        row = self._get_kv("inventory")
        if row is None:
            return None
        return InventorySnapshot.model_validate_json(row["value"])

    def inventory_saved_at(self) -> Optional[datetime]:
        row = self._get_kv("inventory")
        return _parse(row["updated_at"]) if row else None

    def save_catalog(self, catalog: Catalog) -> None:
        self._put_kv("catalog", catalog.model_dump_json())

    def load_catalog(self) -> Optional[Catalog]:
        row = self._get_kv("catalog")
        if row is None:
            return None
        return Catalog.model_validate_json(row["value"])

    def save_browse(self, datastore_id: str, path: str, payload: str) -> None:
        self._put_kv(f"browse:{datastore_id}:{path}", payload)

    def load_browse(self, datastore_id: str, path: str) -> Optional[str]:
        row = self._get_kv(f"browse:{datastore_id}:{path}")
        return None if row is None else str(row["value"])

    def clear_runtime_cache(self) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM kv WHERE key IN ('inventory','catalog','metrics_history_seeded') OR key LIKE 'browse:%'"
            )
            self._conn.commit()

    def open_jobs(self) -> List[Job]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','running','retrying') ORDER BY created_at"
            ).fetchall()
        return [self._job(row) for row in rows]

    def enqueue(
        self,
        kind: str,
        title: str,
        payload: Dict[str, Any],
        idempotency_key: str = "",
    ) -> Job:
        if idempotency_key:
            existing = self.find_open(idempotency_key)
            if existing is not None:
                return existing
        now = _now()
        job = Job(
            id=str(uuid.uuid4()),
            kind=kind,
            title=title,
            status="queued",
            payload=payload,
            created_at=now,
            updated_at=now,
            next_run_at=now,
            idempotency_key=idempotency_key,
        )
        with self._lock:
            self._conn.execute(
                """INSERT INTO jobs(id, kind, title, status, payload, progress, result, error, attempts, max_attempts, next_run_at, created_at, updated_at, idempotency_key, endpoint_fingerprint)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job.id,
                    job.kind,
                    job.title,
                    job.status,
                    json.dumps(job.payload),
                    json.dumps(job.progress),
                    json.dumps(job.result),
                    job.error,
                    job.attempts,
                    job.max_attempts,
                    _iso(job.next_run_at),
                    _iso(job.created_at),
                    _iso(job.updated_at),
                    job.idempotency_key,
                    str(job.payload.get("_endpoint_fingerprint") or ""),
                ),
            )
            self._conn.commit()
        return job

    def find_open(self, idempotency_key: str) -> Optional[Job]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key=? AND status IN ('queued','running','retrying') ORDER BY created_at DESC LIMIT 1",
                (idempotency_key,),
            ).fetchone()
        return self._job(row) if row else None

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def list_jobs(self, limit: int = 80, endpoint_fingerprint: Optional[str] = None) -> List[Job]:
        with self._lock:
            if endpoint_fingerprint is None:
                rows = self._conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE endpoint_fingerprint=? ORDER BY created_at DESC LIMIT ?",
                    (endpoint_fingerprint, limit),
                ).fetchall()
        return [self._job(row) for row in rows]

    def counts(self, endpoint_fingerprint: Optional[str] = None) -> Dict[str, int]:
        with self._lock:
            where = "" if endpoint_fingerprint is None else " AND endpoint_fingerprint=?"
            params = () if endpoint_fingerprint is None else (endpoint_fingerprint,)
            queued = self._conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE status IN ('queued','retrying'){where}", params
            ).fetchone()[0]
            active = self._conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE status='running'{where}", params
            ).fetchone()[0]
        return {"queued": int(queued), "active": int(active)}

    def job_count(self, endpoint_fingerprint: Optional[str] = None) -> int:
        with self._lock:
            if endpoint_fingerprint is None:
                row = self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM jobs WHERE endpoint_fingerprint=?", (endpoint_fingerprint,)
                ).fetchone()
        return int(row[0])

    def clear_job_history(self, endpoint_fingerprint: Optional[str] = None) -> int:
        with self._lock:
            if endpoint_fingerprint is None:
                cursor = self._conn.execute(
                    "DELETE FROM jobs WHERE status IN ('succeeded','failed','cancelled')"
                )
            else:
                cursor = self._conn.execute(
                    "DELETE FROM jobs WHERE endpoint_fingerprint=? AND status IN ('succeeded','failed','cancelled')",
                    (endpoint_fingerprint,),
                )
            self._conn.commit()
            return int(cursor.rowcount)

    def clear_other_job_history(self, endpoint_fingerprint: str) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM jobs WHERE endpoint_fingerprint<>? AND status IN ('succeeded','failed','cancelled')",
                (endpoint_fingerprint,),
            )
            self._conn.commit()
            return int(cursor.rowcount)

    def delete_terminal_job(self, job_id: str, endpoint_fingerprint: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM jobs WHERE id=? AND endpoint_fingerprint=? AND status IN ('succeeded','failed','cancelled')",
                (job_id, endpoint_fingerprint),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def requeue_orphans(self) -> None:
        now = _iso(_now())
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='retrying', next_run_at=?, updated_at=? WHERE status='running'",
                (now, now),
            )
            self._conn.commit()

    def claim_next(self) -> Optional[Job]:
        now = _now()
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM jobs
                   WHERE status IN ('queued','retrying') AND (next_run_at IS NULL OR next_run_at <= ?)
                   ORDER BY created_at ASC LIMIT 1""",
                (_iso(now),),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE jobs SET status='running', updated_at=? WHERE id=? AND status IN ('queued','retrying')",
                (_iso(now), row["id"]),
            )
            self._conn.commit()
            claimed = self._conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        return self._job(claimed) if claimed else None

    def save_progress(self, job_id: str, progress: Dict[str, Any], error: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET progress=?, error=?, updated_at=? WHERE id=?",
                (json.dumps(progress), error, _iso(_now()), job_id),
            )
            self._conn.commit()

    def complete(self, job_id: str, result: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='succeeded', result=?, error='', updated_at=? WHERE id=?",
                (json.dumps(result or {}), _iso(_now()), job_id),
            )
            self._conn.commit()

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status='failed', error=?, updated_at=? WHERE id=?",
                (error, _iso(_now()), job_id),
            )
            self._conn.commit()

    def retry(self, job_id: str, error: str, attempts: int, progress: Optional[Dict[str, Any]] = None) -> None:
        nxt = _now() + timedelta(seconds=backoff_seconds(attempts))
        with self._lock:
            if progress is None:
                self._conn.execute(
                    "UPDATE jobs SET status='retrying', error=?, attempts=?, next_run_at=?, updated_at=? WHERE id=?",
                    (error, attempts, _iso(nxt), _iso(_now()), job_id),
                )
            else:
                self._conn.execute(
                    "UPDATE jobs SET status='retrying', error=?, attempts=?, next_run_at=?, progress=?, updated_at=? WHERE id=?",
                    (error, attempts, _iso(nxt), json.dumps(progress), _iso(_now()), job_id),
                )
            self._conn.commit()

    def cancel(self, job_id: str) -> Optional[Job]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            if row["status"] not in ("queued", "retrying"):
                return self._job(row)
            self._conn.execute(
                "UPDATE jobs SET status='cancelled', updated_at=? WHERE id=?",
                (_iso(_now()), job_id),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def requeue(self, job_id: str) -> Optional[Job]:
        job = self.get(job_id)
        if job is None:
            return None
        if job.status not in {"failed", "retrying", "cancelled"}:
            return job
        progress = dict(job.progress)
        if job.kind in {"migrate", "disk_convert"}:
            progress["task_id"] = ""
            progress["phase"] = "start" if job.kind == "migrate" else ""
        now = _now()
        with self._lock:
            self._conn.execute(
                """UPDATE jobs SET status='queued', error='', attempts=0, progress=?, next_run_at=?, updated_at=?
                   WHERE id=?""",
                (json.dumps(progress), _iso(now), _iso(now), job_id),
            )
            self._conn.commit()
        return self.get(job_id)

    def create_staging(self, filename: str, size: int) -> StagingSession:
        staging_id = str(uuid.uuid4())
        target = self.staging_dir / staging_id
        target.write_bytes(b"")
        with self._lock:
            self._conn.execute(
                "INSERT INTO staging(id, filename, size, received, created_at) VALUES(?,?,?,?,?)",
                (staging_id, filename, size, 0, _iso(_now())),
            )
            self._conn.commit()
        return StagingSession(id=staging_id, filename=filename, size=size, received=0, complete=size == 0)

    def staging(self, staging_id: str) -> Optional[StagingSession]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM staging WHERE id=?", (staging_id,)).fetchone()
        if row is None:
            return None
        return StagingSession(
            id=row["id"],
            filename=row["filename"],
            size=int(row["size"]),
            received=int(row["received"]),
            complete=int(row["received"]) >= int(row["size"]) and int(row["size"]) >= 0,
        )

    def staging_path(self, staging_id: str) -> Path:
        return self.staging_dir / staging_id

    def record_metrics(self, samples) -> None:
        from .metrics import HISTORY_DAYS, MetricSample

        if not samples:
            return
        now = _now()
        cutoff = _iso(now - timedelta(days=HISTORY_DAYS))
        with self._lock:
            for sample in samples:
                if not isinstance(sample, MetricSample):
                    continue
                self._conn.execute(
                    """INSERT INTO metrics(
                           ts, owner_key, cpu_pct, memory_pct, disk_pct,
                           cpu_usage_mhz, memory_usage_mib, storage_bytes, vm_count,
                           host_id, datastore_id
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        _iso(sample.ts),
                        sample.owner_key,
                        sample.cpu_pct,
                        sample.memory_pct,
                        sample.disk_pct,
                        sample.cpu_usage_mhz,
                        sample.memory_usage_mib,
                        sample.storage_bytes,
                        sample.vm_count,
                        sample.host_id,
                        sample.datastore_id,
                    ),
                )
            self._conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
            self._conn.commit()

    def load_metrics(
        self,
        hours: float = 24.0,
        owner: str = "",
        host_id: str = "",
        datastore_id: str = "",
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        max_points: int = 1200,
    ) -> List[MetricPoint]:
        owner_key = owner.strip()
        host = host_id.strip()
        datastore = datastore_id.strip()
        start = since or (_now() - timedelta(hours=hours))
        end = until or _now()
        cpu_mem = self._query_metric_rows(start, end, owner_key, host, "")
        if datastore:
            disk = self._query_metric_rows(start, end, "", "", datastore)
            points = _stitch_disk(cpu_mem, disk)
        else:
            points = cpu_mem
        return _downsample(points, max_points)

    def _query_metric_rows(
        self,
        since: datetime,
        until: datetime,
        owner_key: str,
        host_id: str,
        datastore_id: str,
    ) -> List[MetricPoint]:
        since_iso, until_iso = _iso(since), _iso(until)
        with self._lock:
            rows = self._conn.execute(
                """SELECT ts, cpu_pct, memory_pct, disk_pct, cpu_usage_mhz, memory_usage_mib, storage_bytes
                   FROM metrics
                   WHERE LOWER(owner_key) = LOWER(?) AND host_id = ? AND datastore_id = ?
                     AND ts >= ? AND ts <= ?
                   ORDER BY ts ASC""",
                (owner_key, host_id, datastore_id, since_iso, until_iso),
            ).fetchall()
        points: List[MetricPoint] = []
        for row in rows:
            points.append(
                MetricPoint(
                    ts=_parse(row["ts"]) or _now(),
                    cpu_pct=float(row["cpu_pct"]),
                    memory_pct=float(row["memory_pct"]),
                    disk_pct=float(row["disk_pct"]),
                    cpu_usage_mhz=int(row["cpu_usage_mhz"]),
                    memory_usage_mib=int(row["memory_usage_mib"]),
                    storage_bytes=int(row["storage_bytes"]),
                )
            )
        return points

    def write_staging_chunk(self, staging_id: str, start: int, data: bytes) -> StagingSession:
        session = self.staging(staging_id)
        if session is None:
            raise KeyError("unknown staging id")
        path = self.staging_path(staging_id)
        with path.open("r+b") as handle:
            handle.seek(start)
            handle.write(data)
        received = max(session.received, start + len(data))
        with self._lock:
            self._conn.execute("UPDATE staging SET received=? WHERE id=?", (received, staging_id))
            self._conn.commit()
        return self.staging(staging_id)  # type: ignore[return-value]

    def _job(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            kind=row["kind"],
            title=row["title"],
            status=row["status"],
            payload=json.loads(row["payload"] or "{}"),
            progress=json.loads(row["progress"] or "{}"),
            result=json.loads(row["result"] or "{}"),
            error=row["error"] or "",
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            next_run_at=_parse(row["next_run_at"]),
            created_at=_parse(row["created_at"]) or _now(),
            updated_at=_parse(row["updated_at"]) or _now(),
            idempotency_key=row["idempotency_key"] or "",
        )


def _minute_key(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)


def _stitch_disk(cpu_mem: List[MetricPoint], disk: List[MetricPoint]) -> List[MetricPoint]:
    if not disk:
        return cpu_mem
    if not cpu_mem:
        return disk
    disk_map = {_minute_key(point.ts): point for point in disk}
    stitched: List[MetricPoint] = []
    for point in cpu_mem:
        match = disk_map.get(_minute_key(point.ts))
        if match is None:
            stitched.append(point)
            continue
        stitched.append(
            point.model_copy(update={"disk_pct": match.disk_pct, "storage_bytes": match.storage_bytes})
        )
    return stitched


def _downsample(points: List[MetricPoint], max_points: int) -> List[MetricPoint]:
    if max_points <= 0 or len(points) <= max_points:
        return points
    step = (len(points) - 1) / (max_points - 1)
    indexes = sorted({min(len(points) - 1, int(round(i * step))) for i in range(max_points)})
    if indexes[-1] != len(points) - 1:
        indexes.append(len(points) - 1)
    return [points[index] for index in indexes]
