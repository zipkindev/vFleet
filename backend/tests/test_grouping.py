from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.grouping import (
    normalize_principal,
    owner_from_name,
    resolve_deployed_by,
    resolve_owner,
    similar_prefix_groups,
    vm_matches_owner_query,
)
from app.models import VirtualMachine
from app.reclaim import annotate_idle, build_owner_reports


def test_owner_from_name_default():
    assert owner_from_name("atlasdemo-win11-lab") == "atlasdemo"
    assert owner_from_name("novademo_rhel_lab01") == "novademo"
    assert owner_from_name("oriondemo.ml-gpu02") == "oriondemo"


def test_custom_field_wins():
    owner, source = resolve_owner(
        "shared-nexus-proxy",
        {"Owner": "platform"},
        ["Owner", "User"],
    )
    assert owner == "platform"
    assert source == "custom_field"


def test_normalize_principal_strips_domain():
    assert normalize_principal(r"EXAMPLE\emberdemo") == "emberdemo"
    assert normalize_principal("emberdemo@example.test") == "emberdemo"


def test_resolve_deployed_by_prefers_custom_field():
    assert resolve_deployed_by({"Owner": "platform"}, ["Owner"], r"EXAMPLE\admin") == "platform"


def test_resolve_deployed_by_uses_event_user():
    assert resolve_deployed_by({}, ["Owner"], r"EXAMPLE\quasardemo") == "quasardemo"


def test_owner_query_matches_deployed_by():
    assert vm_matches_owner_query("windows", "emberdemo", {}, "emberdemo")
    assert vm_matches_owner_query("windows", "emberdemo", {}, "windows")
    assert not vm_matches_owner_query("windows", "emberdemo", {}, "other")


def test_owner_reports_group_by_deployed_by():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    vms = [
        VirtualMachine(
            id="vm-1",
            name="windows-lab",
            power_state="POWERED_ON",
            cpu_count=2,
            memory_mib=4096,
            owner_key="windows",
            owner_source="name_prefix",
            deployed_by="emberdemo",
            last_activity=now,
        ),
        VirtualMachine(
            id="vm-2",
            name="vm-debug",
            power_state="POWERED_OFF",
            cpu_count=2,
            memory_mib=2048,
            owner_key="vm",
            owner_source="name_prefix",
            deployed_by="emberdemo",
        ),
        VirtualMachine(
            id="vm-3",
            name="quasardemo-box",
            power_state="POWERED_ON",
            cpu_count=4,
            memory_mib=8192,
            owner_key="quasardemo",
            owner_source="name_prefix",
            deployed_by="quasardemo",
            last_activity=now,
        ),
    ]
    by_prefix = build_owner_reports(vms)
    assert {row.owner_key for row in by_prefix} == {"windows", "vm", "quasardemo"}
    by_deployer = build_owner_reports(vms, group_by="deployed_by")
    emberdemo = next(row for row in by_deployer if row.owner_key == "emberdemo")
    assert emberdemo.vm_count == 2
    assert emberdemo.owner_source == "deployed_by"
    assert set(emberdemo.vms) == {"windows-lab", "vm-debug"}


def test_similar_prefix_groups():
    groups = similar_prefix_groups(
        ["atlasdemo-win11-lab", "atlasdemo-ubuntu-dev", "novademo-rhel-lab01", "ab"]
    )
    assert groups["atlasdemo"] == ["atlasdemo-win11-lab", "atlasdemo-ubuntu-dev"]
    assert groups["novademo"] == ["novademo-rhel-lab01"]
    assert groups["ungrouped"] == ["ab"]


def test_idle_score_flags_abandoned_heavy_vm():
    settings = Settings(idle_days=14, cpu_idle_pct=8, memory_idle_pct=20)
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    vm = VirtualMachine(
        id="vm-1",
        name="zephyrdemo-win-lab",
        power_state="POWERED_ON",
        cpu_count=8,
        memory_mib=32768,
        cpu_usage_mhz=100,
        cpu_usage_pct=2,
        memory_usage_mib=800,
        memory_usage_pct=4,
        owner_key="zephyrdemo",
        owner_source="name_prefix",
        last_activity=now - timedelta(days=40),
    )
    annotate_idle(vm, settings, now)
    assert vm.idle_score >= 60
    assert vm.reclaim_reason is not None
