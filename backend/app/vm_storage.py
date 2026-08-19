from __future__ import annotations

from typing import Any, Iterable, Tuple

Provisioning = str  # thin | thick | mixed | unknown
DISK_TRANSFORMS = {"thin", "thick"}


def normalize_disk_transform(value: str | None) -> str:
    text = (value or "").strip().lower()
    if text in {"", "keep", "unchanged", "current"}:
        return ""
    if text in {"eagerzeroedthick", "eager_zeroed_thick"}:
        return "thick"
    if text not in DISK_TRANSFORMS:
        raise ValueError("disk_provisioning must be keep, thin, or thick")
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
