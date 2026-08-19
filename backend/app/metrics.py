from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

from .models import Catalog, DatastoreSummary, InventorySnapshot, OwnerUtilization


HISTORY_DAYS = 14
HISTORY_STEP_MINUTES = 30


@dataclass(frozen=True)
class MetricSample:
    ts: datetime
    owner_key: str
    cpu_pct: float
    memory_pct: float
    disk_pct: float
    cpu_usage_mhz: int
    memory_usage_mib: int
    storage_bytes: int
    vm_count: int = 0
    host_id: str = ""
    datastore_id: str = ""


def _cluster_totals(snapshot: InventorySnapshot) -> tuple[float, int, int, int, int]:
    clusters = snapshot.clusters
    if not clusters:
        return 0.0, 0, 0, 0, 0
    cpu_capacity = sum(item.cpu_capacity_mhz for item in clusters)
    cpu_usage = sum(item.cpu_usage_mhz for item in clusters)
    memory_mib = sum(item.memory_mib for item in clusters)
    memory_usage_mib = sum(item.memory_usage_mib for item in clusters)
    cpu_pct = (cpu_usage / cpu_capacity * 100.0) if cpu_capacity else 0.0
    return cpu_pct, cpu_usage, memory_mib, memory_usage_mib, cpu_capacity


def _selected_datastores(catalog: Optional[Catalog], host_id: str = "") -> List[DatastoreSummary]:
    if catalog is None or not catalog.datastores:
        return []
    stores = [item for item in catalog.datastores if item.capacity_bytes > 0]
    if host_id:
        stores = [item for item in stores if not item.host_ids or host_id in item.host_ids]
    return stores


def _datastore_usage(catalog: Optional[Catalog], host_id: str = "", datastore_id: str = "") -> tuple[float, int]:
    stores = _selected_datastores(catalog, host_id=host_id)
    if datastore_id:
        stores = [item for item in stores if item.id == datastore_id]
    if not stores:
        return 0.0, 0
    capacity = sum(item.capacity_bytes for item in stores)
    free = sum(item.free_bytes for item in stores)
    used = max(0, capacity - free)
    pct = (used / capacity * 100.0) if capacity else 0.0
    return pct, used


def _sample(
    ts: datetime,
    cpu_pct: float,
    memory_pct: float,
    disk_pct: float,
    cpu_usage_mhz: int,
    memory_usage_mib: int,
    storage_bytes: int,
    vm_count: int = 0,
    owner_key: str = "",
    host_id: str = "",
    datastore_id: str = "",
) -> MetricSample:
    return MetricSample(
        ts=ts,
        owner_key=owner_key,
        cpu_pct=round(cpu_pct, 2),
        memory_pct=round(memory_pct, 2),
        disk_pct=round(disk_pct, 2),
        cpu_usage_mhz=cpu_usage_mhz,
        memory_usage_mib=memory_usage_mib,
        storage_bytes=storage_bytes,
        vm_count=vm_count,
        host_id=host_id,
        datastore_id=datastore_id,
    )


def build_metric_samples(snapshot: InventorySnapshot, catalog: Optional[Catalog], ts: Optional[datetime] = None) -> List[MetricSample]:
    ts = ts or datetime.now(timezone.utc)
    cpu_pct, cpu_usage, memory_mib, memory_usage_mib, cpu_capacity = _cluster_totals(snapshot)
    mem_pct = (memory_usage_mib / memory_mib * 100.0) if memory_mib else 0.0
    disk_pct, total_used_storage = _datastore_usage(catalog)

    samples: List[MetricSample] = [
        _sample(
            ts,
            cpu_pct,
            mem_pct,
            disk_pct,
            cpu_usage,
            memory_usage_mib,
            total_used_storage,
            vm_count=len(snapshot.vms),
        )
    ]

    total_mem_alloc = sum(owner.memory_mib for owner in snapshot.owners) or 1
    for owner in snapshot.owners:
        storage_est = int((owner.memory_mib / total_mem_alloc) * total_used_storage)
        owner_cpu_pct = (owner.cpu_usage_mhz / cpu_capacity * 100.0) if cpu_capacity else 0.0
        owner_mem_pct = (owner.memory_usage_mib / memory_mib * 100.0) if memory_mib else 0.0
        owner_disk_pct = (storage_est / total_used_storage * 100.0) if total_used_storage else 0.0
        samples.append(
            _sample(
                ts,
                owner_cpu_pct,
                owner_mem_pct,
                owner_disk_pct,
                owner.cpu_usage_mhz,
                owner.memory_usage_mib,
                storage_est,
                vm_count=owner.vm_count,
                owner_key=owner.owner_key,
            )
        )

    for host in snapshot.hosts:
        host_cpu_cap = host.cpu_cores * host.cpu_mhz
        host_cpu_pct = host.cpu_usage_pct if host.cpu_usage_pct else ((host.cpu_usage_mhz / host_cpu_cap * 100.0) if host_cpu_cap else 0.0)
        host_mem_pct = host.memory_usage_pct if host.memory_usage_pct else (
            (host.memory_usage_mib / host.memory_mib * 100.0) if host.memory_mib else 0.0
        )
        host_disk_pct, host_storage = _datastore_usage(catalog, host_id=host.id)
        host_vms = sum(1 for vm in snapshot.vms if vm.host_id == host.id)
        samples.append(
            _sample(
                ts,
                host_cpu_pct,
                host_mem_pct,
                host_disk_pct,
                host.cpu_usage_mhz,
                host.memory_usage_mib,
                host_storage,
                vm_count=host_vms or host.vm_count,
                host_id=host.id,
            )
        )

    for store in _selected_datastores(catalog):
        used = max(0, store.capacity_bytes - store.free_bytes)
        pct = store.usage_pct if store.usage_pct else ((used / store.capacity_bytes * 100.0) if store.capacity_bytes else 0.0)
        samples.append(
            _sample(
                ts,
                0.0,
                0.0,
                pct,
                0,
                0,
                used,
                datastore_id=store.id,
            )
        )
    return samples


def synthesize_history(
    samples: Iterable[MetricSample],
    days: int = HISTORY_DAYS,
    step_minutes: int = HISTORY_STEP_MINUTES,
    now: Optional[datetime] = None,
) -> List[MetricSample]:
    """Build a 2-week trend from current samples when vCenter history is unavailable (demo / first collect)."""
    current = [item for item in samples if isinstance(item, MetricSample)]
    if not current:
        return []
    now = now or datetime.now(timezone.utc)
    steps = max(1, int((days * 24 * 60) / step_minutes))
    out: List[MetricSample] = []
    for i in range(steps):
        ts = now - timedelta(minutes=step_minutes * (steps - i))
        hour = (ts.hour + ts.minute / 60.0) % 24
        day_wave = 0.55 + 0.45 * (0.5 + 0.5 * math.sin((hour - 8) * math.pi / 12))
        week_wave = 0.9 + 0.1 * math.sin(i / max(1, (24 * 60 / step_minutes) * 3.5))
        scale = max(0.2, min(1.25, day_wave * week_wave))
        for sample in current:
            seed = hash((sample.owner_key, sample.host_id, sample.datastore_id)) % 97
            jitter = 0.96 + 0.08 * math.sin((seed + i) / 6.0)
            factor = max(0.12, min(1.4, scale * jitter))
            out.append(
                replace(
                    sample,
                    ts=ts,
                    cpu_pct=round(min(100.0, sample.cpu_pct * factor), 2),
                    memory_pct=round(min(100.0, sample.memory_pct * factor), 2),
                    disk_pct=round(min(100.0, max(sample.disk_pct * (0.92 + 0.08 * factor), sample.disk_pct * 0.85)), 2),
                    cpu_usage_mhz=int(sample.cpu_usage_mhz * factor),
                    memory_usage_mib=int(sample.memory_usage_mib * factor),
                    storage_bytes=int(sample.storage_bytes * min(1.05, 0.9 + 0.1 * factor)),
                )
            )
    return out


def build_owner_utilization(snapshot: InventorySnapshot, catalog: Optional[Catalog]) -> List[OwnerUtilization]:
    _, _, memory_mib, _, cpu_capacity = _cluster_totals(snapshot)
    _, total_used_storage = _datastore_usage(catalog)
    total_mem_alloc = sum(owner.memory_mib for owner in snapshot.owners) or 1
    total_cpu_use = sum(owner.cpu_usage_mhz for owner in snapshot.owners) or 1
    total_mem_use = sum(owner.memory_usage_mib for owner in snapshot.owners) or 1

    rows: List[OwnerUtilization] = []
    for owner in snapshot.owners:
        storage_est = int((owner.memory_mib / total_mem_alloc) * total_used_storage)
        rows.append(
            OwnerUtilization(
                owner_key=owner.owner_key,
                vm_count=owner.vm_count,
                cpu_usage_mhz=owner.cpu_usage_mhz,
                memory_usage_mib=owner.memory_usage_mib,
                storage_bytes=storage_est,
                cpu_share_pct=round(owner.cpu_usage_mhz / total_cpu_use * 100.0, 2),
                memory_share_pct=round(owner.memory_usage_mib / total_mem_use * 100.0, 2),
                storage_share_pct=round(storage_est / total_used_storage * 100.0, 2) if total_used_storage else 0.0,
                cpu_usage_pct=round(owner.cpu_usage_mhz / cpu_capacity * 100.0, 2) if cpu_capacity else 0.0,
                memory_usage_pct=round(owner.memory_usage_mib / memory_mib * 100.0, 2) if memory_mib else 0.0,
            )
        )
    rows.sort(key=lambda item: (item.cpu_share_pct + item.memory_share_pct + item.storage_share_pct), reverse=True)
    return rows
