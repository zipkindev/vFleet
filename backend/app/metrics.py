from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from .models import Catalog, InventorySnapshot, OwnerUtilization


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


def _datastore_usage(catalog: Optional[Catalog]) -> tuple[float, int]:
    if catalog is None or not catalog.datastores:
        return 0.0, 0
    stores = [item for item in catalog.datastores if item.capacity_bytes > 0]
    if not stores:
        return 0.0, 0
    capacity = sum(item.capacity_bytes for item in stores)
    free = sum(item.free_bytes for item in stores)
    used = max(0, capacity - free)
    pct = (used / capacity * 100.0) if capacity else 0.0
    return pct, used


def build_metric_samples(snapshot: InventorySnapshot, catalog: Optional[Catalog]) -> List[MetricSample]:
    ts = datetime.now(timezone.utc)
    cpu_pct, cpu_usage, memory_mib, memory_usage_mib, cpu_capacity = _cluster_totals(snapshot)
    mem_pct = (memory_usage_mib / memory_mib * 100.0) if memory_mib else 0.0
    disk_pct, total_used_storage = _datastore_usage(catalog)

    samples: List[MetricSample] = [
        MetricSample(
            ts=ts,
            owner_key="",
            cpu_pct=round(cpu_pct, 2),
            memory_pct=round(mem_pct, 2),
            disk_pct=round(disk_pct, 2),
            cpu_usage_mhz=cpu_usage,
            memory_usage_mib=memory_usage_mib,
            storage_bytes=total_used_storage,
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
            MetricSample(
                ts=ts,
                owner_key=owner.owner_key,
                cpu_pct=round(owner_cpu_pct, 2),
                memory_pct=round(owner_mem_pct, 2),
                disk_pct=round(owner_disk_pct, 2),
                cpu_usage_mhz=owner.cpu_usage_mhz,
                memory_usage_mib=owner.memory_usage_mib,
                storage_bytes=storage_est,
                vm_count=owner.vm_count,
            )
        )
    return samples


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
