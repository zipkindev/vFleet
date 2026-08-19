from __future__ import annotations

ROLE_NAME = "vFleet-Migrate"

MIGRATE_PRIVILEGES = [
    ("System.Anonymous", "System anonymous"),
    ("System.View", "System view"),
    ("System.Read", "Read inventory"),
    ("Resource.HotMigrate", "vMotion (powered-on VMs)"),
    ("Resource.ColdMigrate", "Cold migrate / Thick→Thin while off"),
    ("Resource.QueryVMotion", "Query vMotion compatibility"),
    ("Datastore.Relocate", "Storage vMotion / change datastore"),
    ("Datastore.AllocateSpace", "Allocate datastore space"),
    ("Network.Assign", "Assign VM network"),
]

MIGRATE_PRIV_IDS = [item[0] for item in MIGRATE_PRIVILEGES]


def privilege_rows(granted: dict[str, bool] | None = None) -> list[dict]:
    granted = granted or {}
    return [
        {"id": priv_id, "label": label, "granted": bool(granted.get(priv_id, False))}
        for priv_id, label in MIGRATE_PRIVILEGES
        if not priv_id.startswith("System.")
    ]
