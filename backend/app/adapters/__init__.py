from __future__ import annotations

from ..config import Settings
from .base import InventoryAdapter
from .demo import DemoAdapter


def build_adapter(settings: Settings) -> InventoryAdapter:
    mode = settings.resolved_mode
    if mode in {"vcenter", "esxi"}:
        from .vcenter import VCenterAdapter

        return VCenterAdapter(settings)
    return DemoAdapter(settings)
