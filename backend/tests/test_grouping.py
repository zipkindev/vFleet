from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.grouping import owner_from_name, resolve_owner, similar_prefix_groups
from app.models import VirtualMachine
from app.reclaim import annotate_idle


def test_owner_from_name_default():
    assert owner_from_name("mzipkin-win11-lab") == "mzipkin"
    assert owner_from_name("jdoe_rhel_lab01") == "jdoe"
    assert owner_from_name("achen.ml-gpu02") == "achen"


def test_custom_field_wins():
    owner, source = resolve_owner(
        "shared-nexus-proxy",
        {"Owner": "platform"},
        ["Owner", "User"],
    )
    assert owner == "platform"
    assert source == "custom_field"


def test_similar_prefix_groups():
    groups = similar_prefix_groups(
        ["mzipkin-win11-lab", "mzipkin-ubuntu-dev", "jdoe-rhel-lab01", "ab"]
    )
    assert groups["mzipkin"] == ["mzipkin-win11-lab", "mzipkin-ubuntu-dev"]
    assert groups["jdoe"] == ["jdoe-rhel-lab01"]
    assert groups["ungrouped"] == ["ab"]


def test_idle_score_flags_abandoned_heavy_vm():
    settings = Settings(idle_days=14, cpu_idle_pct=8, memory_idle_pct=20)
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    vm = VirtualMachine(
        id="vm-1",
        name="kwong-win-lab",
        power_state="POWERED_ON",
        cpu_count=8,
        memory_mib=32768,
        cpu_usage_mhz=100,
        cpu_usage_pct=2,
        memory_usage_mib=800,
        memory_usage_pct=4,
        owner_key="kwong",
        owner_source="name_prefix",
        last_activity=now - timedelta(days=40),
    )
    annotate_idle(vm, settings, now)
    assert vm.idle_score >= 60
    assert vm.reclaim_reason is not None
