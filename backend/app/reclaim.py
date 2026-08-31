from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .config import Settings
from .models import OwnerReport, VirtualMachine
from .power import normalize_power_state


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def activity_age_days(vm: VirtualMachine, now: Optional[datetime] = None) -> Optional[float]:
    stamp = _aware(vm.last_activity) or _aware(vm.boot_time)
    if stamp is None:
        return None
    current = now or _now()
    return max(0.0, (current - stamp).total_seconds() / 86400.0)


def annotate_idle(vm: VirtualMachine, settings: Settings, now: Optional[datetime] = None) -> VirtualMachine:
    days = activity_age_days(vm, now)
    vm.days_idle = round(days, 1) if days is not None else None

    reasons = []
    score = 0
    powered_on = normalize_power_state(vm.power_state) == "POWERED_ON"

    if powered_on and vm.cpu_usage_pct <= settings.cpu_idle_pct:
        score += 35
        reasons.append(f"CPU {vm.cpu_usage_pct:.1f}% ≤ {settings.cpu_idle_pct:.0f}%")
    if powered_on and vm.memory_usage_pct <= settings.memory_idle_pct:
        score += 20
        reasons.append(f"memory {vm.memory_usage_pct:.1f}% ≤ {settings.memory_idle_pct:.0f}%")
    if powered_on and days is not None and days >= settings.idle_days:
        score += 30
        reasons.append(f"no vCenter activity for {days:.0f}d")
    elif powered_on and days is None:
        score += 10
        reasons.append("no recorded activity")
    if powered_on and vm.memory_mib >= 8192:
        score += 10
        reasons.append(f"{vm.memory_mib / 1024:.0f} GiB reserved")
    if powered_on and vm.cpu_count >= 4 and vm.cpu_usage_pct <= settings.cpu_idle_pct:
        score += 5
        reasons.append(f"{vm.cpu_count} vCPU mostly idle")

    vm.idle_score = score
    vm.reclaim_reason = "; ".join(reasons) if score >= 40 else None
    return vm


def build_owner_reports(
    vms: list[VirtualMachine],
    *,
    group_by: str = "owner_key",
) -> list[OwnerReport]:
    grouped: dict[str, list[VirtualMachine]] = {}
    for vm in vms:
        if group_by == "deployed_by":
            key = (vm.deployed_by or "").strip() or "unknown"
        else:
            key = vm.owner_key
        grouped.setdefault(key, []).append(vm)

    reports: list[OwnerReport] = []
    for owner, members in grouped.items():
        powered_on = sum(1 for vm in members if normalize_power_state(vm.power_state) == "POWERED_ON")
        idle = [
            vm
            for vm in members
            if vm.idle_score >= 40 and normalize_power_state(vm.power_state) == "POWERED_ON"
        ]
        if group_by == "deployed_by":
            source = "deployed_by" if owner != "unknown" else "unknown"
        else:
            source = members[0].owner_source
        reports.append(
            OwnerReport(
                owner_key=owner,
                owner_source=source,
                vm_count=len(members),
                powered_on=powered_on,
                powered_off=sum(1 for vm in members if normalize_power_state(vm.power_state) == "POWERED_OFF"),
                suspended=sum(1 for vm in members if normalize_power_state(vm.power_state) == "SUSPENDED"),
                cpu_count=sum(vm.cpu_count for vm in members),
                memory_mib=sum(vm.memory_mib for vm in members),
                cpu_usage_mhz=sum(vm.cpu_usage_mhz for vm in members),
                memory_usage_mib=sum(vm.memory_usage_mib for vm in members),
                idle_candidates=len(idle),
                reclaimable_memory_mib=sum(vm.memory_mib for vm in idle),
                vms=sorted(vm.name for vm in members),
            )
        )
    reports.sort(key=lambda row: (row.memory_mib, row.cpu_count, row.vm_count), reverse=True)
    return reports
