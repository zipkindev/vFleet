from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional

from .adapters.base import InventoryAdapter
from .config import Settings
from .errors import PermanentError, humanize_vcenter_error, is_permanent, is_transient
from .models import Catalog, CloneVmRequest, ConnectionInfo, InventorySnapshot, Job, MigrateVmRequest
from .store import LocalStore


class RelayWorker(threading.Thread):
    def __init__(self, settings: Settings, store: LocalStore, adapter_getter) -> None:
        super().__init__(daemon=True, name="vfleet-relay")
        self.settings = settings
        self.store = store
        self._adapter_getter = adapter_getter
        self._halt = threading.Event()
        self._wake = threading.Event()
        self.syncing = False
        self.last_error = ""
        self.last_sync_ok: Optional[datetime] = None
        self.reachable: Optional[bool] = None

    def adapter(self) -> InventoryAdapter:
        return self._adapter_getter()

    def stop(self) -> None:
        self._halt.set()
        self._wake.set()
        self.join(timeout=5)

    def wake(self) -> None:
        self._wake.set()

    def run(self) -> None:
        self.store.requeue_orphans()
        self.sync_now(force=True)
        while not self._halt.is_set():
            self.sync_now(force=False)
            self._heartbeat()
            self._drain()
            self._wake.wait(self.settings.relay_tick_seconds)
            self._wake.clear()

    def _record_metrics(self, adapter: InventoryAdapter, snapshot: InventorySnapshot, catalog: Optional[Catalog]) -> None:
        from .metrics import build_metric_samples

        live = build_metric_samples(snapshot, catalog)
        if not self.store.history_seeded():
            try:
                history = adapter.historical_metrics(snapshot, catalog)
                if history:
                    self.store.record_metrics(history)
            except Exception:
                history = []
            self.store.mark_history_seeded()
        self.store.record_metrics(live)

    def sync_now(self, force: bool = False) -> None:
        last = self.store.inventory_saved_at()
        if not force and last is not None:
            age = (datetime.now(timezone.utc) - last).total_seconds()
            if age < self.settings.sync_interval_seconds:
                return
        adapter = self.adapter()
        self.syncing = True
        try:
            snapshot = adapter.snapshot()
            self.store.save_inventory(snapshot)
            catalog: Optional[Catalog] = None
            try:
                networks = []
                try:
                    networks = adapter.list_networks()
                except Exception:
                    networks = []
                catalog = Catalog(
                    datastores=adapter.list_datastores(),
                    templates=adapter.list_templates(),
                    networks=networks,
                    last_sync=datetime.now(timezone.utc),
                )
                self.store.save_catalog(catalog)
            except Exception as exc:
                self.last_error = humanize_vcenter_error(exc, host=self.settings.vcenter_host)
            try:
                self._record_metrics(adapter, snapshot, catalog or self.store.load_catalog())
            except Exception:
                pass
            self.last_error = ""
            self.last_sync_ok = datetime.now(timezone.utc)
            self.reachable = True
        except Exception as exc:
            self.last_error = humanize_vcenter_error(exc, host=self.settings.vcenter_host)
            self.reachable = False
        finally:
            self.syncing = False

    def overlay(self, conn: ConnectionInfo) -> ConnectionInfo:
        counts = self.store.counts()
        saved_at = self.store.inventory_saved_at()
        age = None
        if saved_at is not None:
            age = max(0.0, (datetime.now(timezone.utc) - saved_at).total_seconds())
        stale = False
        if conn.mode == "vcenter":
            if self.reachable is False:
                conn = conn.model_copy(update={"connected": False})
            # A full inventory pull of a large vCenter can exceed sync_interval.
            # Only treat the relay as stale when the session is actually down.
            stale = self.reachable is False and not self.syncing
        message = conn.message
        if stale and saved_at is not None:
            message = self.last_error or conn.message or "vCenter unreachable; showing last local snapshot"
        return conn.model_copy(
            update={
                "stale": stale,
                "syncing": self.syncing,
                "last_error": self.last_error,
                "queued_jobs": counts["queued"],
                "active_jobs": counts["active"],
                "cache_age_seconds": age,
                "last_sync": saved_at or conn.last_sync,
                "message": message,
            }
        )

    def cached_snapshot(self) -> Optional[InventorySnapshot]:
        snapshot = self.store.load_inventory()
        if snapshot is None:
            return None
        from .adapters.demo import DemoAdapter

        adapter = self.adapter()
        if isinstance(adapter, DemoAdapter):
            live = adapter.connection()
        else:
            live = snapshot.connection.model_copy(
                update={
                    "mode": "vcenter",
                    "connected": self.reachable is not False and not self.last_error,
                    "message": self.last_error or snapshot.connection.message,
                    "host": self.settings.vcenter_host or snapshot.connection.host,
                    "user": self.settings.vcenter_user or snapshot.connection.user,
                }
            )
        snapshot.connection = self.overlay(live)
        return snapshot

    def _heartbeat(self) -> None:
        if self.syncing:
            return
        adapter = self.adapter()
        ping = getattr(adapter, "ping", None)
        if not callable(ping):
            return
        try:
            ping()
            self.reachable = True
            self.last_error = ""
        except Exception as exc:
            self.last_error = humanize_vcenter_error(exc, host=self.settings.vcenter_host)
            self.reachable = False

    def _drain(self) -> None:
        job = self.store.claim_next()
        if job is None:
            return
        try:
            result = self._execute(job)
            try:
                self.sync_now(force=True)
            except Exception:
                pass
            self.store.complete(job.id, result)
        except Exception as exc:
            attempts = job.attempts + 1
            if is_permanent(exc) or attempts >= job.max_attempts:
                self.store.fail(job.id, str(exc))
            elif is_transient(exc) or not is_permanent(exc):
                refreshed = self.store.get(job.id)
                progress = refreshed.progress if refreshed else job.progress
                self.store.retry(job.id, str(exc), attempts, progress)
            else:
                self.store.fail(job.id, str(exc))

    def _execute(self, job: Job) -> dict:
        adapter = self.adapter()
        kind = job.kind
        payload = job.payload
        progress = dict(job.progress)

        def persist(sent: int, extra: dict) -> None:
            extra = dict(extra)
            extra["bytes_sent"] = sent
            self.store.save_progress(job.id, extra)

        if kind == "power":
            results = adapter.apply_actions(payload["vm_ids"], payload["action"])
            failed = [row for row in results if not row.ok]
            if failed:
                messages = "; ".join(f"{row.name or row.vm_id}: {row.message}" for row in failed)
                if any(is_transient(Exception(row.message)) for row in failed):
                    raise Exception(messages)
                if len(failed) == len(results):
                    raise PermanentError(messages)
            return {"results": [row.model_dump() for row in results]}

        if kind == "clone":
            spec = CloneVmRequest.model_validate(payload)
            task_id = str(progress.get("task_id") or "")
            if not task_id:
                task_id = adapter.start_clone(spec)
                progress["task_id"] = task_id
                self.store.save_progress(job.id, progress)
            adapter.wait_task(task_id)
            return {"name": spec.name, "task_id": task_id}

        if kind == "migrate":
            spec = MigrateVmRequest.model_validate(payload)
            vm_ids = list(spec.vm_ids)
            index = int(progress.get("index") or 0)
            results = list(progress.get("results") or [])
            while index < len(vm_ids):
                task_id = str(progress.get("task_id") or "")
                phase = str(progress.get("phase") or "start")
                if phase == "start":
                    one = spec.model_copy(update={"vm_ids": [vm_ids[index]]})
                    task_id = adapter.start_migrate(one) or ""
                    progress["task_id"] = task_id
                    progress["index"] = index
                    progress["phase"] = "wait" if task_id else "next"
                    self.store.save_progress(job.id, progress)
                    phase = progress["phase"]
                if phase == "wait":
                    adapter.wait_task(str(progress.get("task_id") or ""))
                    progress["phase"] = "next"
                    self.store.save_progress(job.id, progress)
                results.append(
                    {
                        "vm_id": vm_ids[index],
                        "ok": True,
                        "skipped": not bool(progress.get("task_id")),
                    }
                )
                index += 1
                progress = {"index": index, "phase": "start", "task_id": "", "results": results}
                self.store.save_progress(job.id, progress)
            return {"results": results}

        if kind == "mkdir":
            adapter.mkdir(payload["datastore_id"], payload["path"])
            return payload

        if kind == "delete_file":
            adapter.delete_file(payload["datastore_id"], payload["path"])
            return payload

        if kind == "upload":
            extra = dict(progress)
            extra.update(adapter.upload_file(
                datastore_id=payload["datastore_id"],
                remote_path=payload["remote_path"],
                local_path=payload["local_path"],
                extra=extra,
                on_progress=persist,
                use_library=bool(payload.get("use_library", True)),
            ))
            self.store.save_progress(job.id, extra)
            return extra

        raise PermanentError(f"Unknown job kind {kind}")
