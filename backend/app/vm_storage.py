from __future__ import annotations

from typing import Any, Iterable, List, Tuple

from .models import VirtualDiskSummary

Provisioning = str  # thin | thick | mixed | unknown
DISK_TRANSFORMS = {"thin", "thick", "lazy_zeroed_thick", "eager_zeroed_thick"}


def normalize_disk_transform(value: str | None) -> str:
    text = (value or "").strip().lower()
    if text in {"", "keep", "unchanged", "current"}:
        return ""
    aliases = {
        "lazyzeroedthick": "lazy_zeroed_thick",
        "lazy-zeroed-thick": "lazy_zeroed_thick",
        "zeroedthick": "lazy_zeroed_thick",
        "eagerzeroedthick": "eager_zeroed_thick",
        "eager-zeroed-thick": "eager_zeroed_thick",
    }
    text = aliases.get(text, text)
    if text not in DISK_TRANSFORMS:
        raise ValueError(
            "disk_provisioning must be keep, thin, thick, lazy_zeroed_thick, or eager_zeroed_thick"
        )
    return text


def apply_disk_transform(provisioned_bytes: int, transform: str) -> tuple[int, Provisioning]:
    kind = normalize_disk_transform(transform)
    if not kind:
        return provisioned_bytes, "unknown"
    if kind == "thin":
        return int(provisioned_bytes * 0.22), "thin"
    return int(provisioned_bytes * 0.86), "thick"


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def disk_capacity_bytes(device: Any) -> int:
    capacity = _int(getattr(device, "capacityInBytes", 0))
    if capacity:
        return capacity
    capacity_kb = _int(getattr(device, "capacityInKB", 0))
    return capacity_kb * 1024 if capacity_kb else 0


def is_virtual_disk(device: Any) -> bool:
    name = type(device).__name__.rsplit(".", 1)[-1]
    if name == "VirtualDisk":
        return True
    return disk_capacity_bytes(device) > 0 and hasattr(device, "backing")


def classify_backing(backing: Any) -> Provisioning:
    if backing is None:
        return "unknown"
    if bool(getattr(backing, "thinProvisioned", False)):
        return "thin"
    return "thick"


def detailed_backing_type(backing: Any) -> Provisioning:
    kind = classify_backing(backing)
    if kind != "thick":
        return kind
    if bool(getattr(backing, "eagerlyScrub", False)):
        return "eager_zeroed_thick"
    return "lazy_zeroed_thick"


def _parent_depth(backing: Any) -> int:
    depth = 0
    seen: set[int] = set()
    current = getattr(backing, "parent", None)
    while current is not None and id(current) not in seen and depth < 64:
        seen.add(id(current))
        depth += 1
        current = getattr(current, "parent", None)
    return depth


def disk_summaries(devices: Iterable[Any] | None) -> List[VirtualDiskSummary]:
    rows: List[VirtualDiskSummary] = []
    for device in devices or []:
        if not is_virtual_disk(device):
            continue
        backing = getattr(device, "backing", None)
        datastore = getattr(backing, "datastore", None) if backing is not None else None
        backing_name = type(backing).__name__.rsplit(".", 1)[-1] if backing is not None else ""
        lowered = backing_name.lower()
        rows.append(
            VirtualDiskSummary(
                key=int(getattr(device, "key", 0) or 0),
                label=str(getattr(getattr(device, "deviceInfo", None), "label", "") or "Virtual disk"),
                capacity_bytes=disk_capacity_bytes(device),
                file_name=str(getattr(backing, "fileName", "") or ""),
                datastore_id=str(
                    (datastore._GetMoId() if datastore is not None and hasattr(datastore, "_GetMoId") else "") or ""
                ),
                datastore_name=str(getattr(datastore, "name", "") or ""),
                provisioning=detailed_backing_type(backing),
                disk_mode=str(getattr(backing, "diskMode", "") or ""),
                backing_type=backing_name,
                parent_depth=_parent_depth(backing),
                rdm="rawdisk" in lowered or bool(getattr(backing, "deviceName", "")),
                encrypted=getattr(backing, "keyId", None) is not None,
                sharing=str(getattr(backing, "sharing", "") or ""),
            )
        )
    return rows


def summarize_disks(
    devices: Iterable[Any] | None,
    committed: int = 0,
    uncommitted: int = 0,
) -> Tuple[int, int, Provisioning]:
    used = max(0, _int(committed))
    provisioned = used + max(0, _int(uncommitted))
    kinds: set[str] = set()
    capacity_total = 0
    for device in devices or []:
        if not is_virtual_disk(device):
            continue
        capacity_total += disk_capacity_bytes(device)
        kind = classify_backing(getattr(device, "backing", None))
        if kind != "unknown":
            kinds.add(kind)
    if not provisioned:
        provisioned = capacity_total
    if kinds == {"thin"}:
        provisioning: Provisioning = "thin"
    elif kinds == {"thick"}:
        provisioning = "thick"
    elif kinds == {"thin", "thick"}:
        provisioning = "mixed"
    else:
        provisioning = "unknown"
    return used, provisioned, provisioning
