from types import SimpleNamespace

from app.vm_storage import classify_backing, summarize_disks


def _disk(capacity_gb: int, thin: bool, backing: bool = True) -> SimpleNamespace:
    gib = 1024 ** 3
    return SimpleNamespace(
        capacityInBytes=capacity_gb * gib,
        capacityInKB=capacity_gb * 1024 * 1024,
        backing=SimpleNamespace(thinProvisioned=thin, fileName="disk.vmdk") if backing else None,
    )


def test_summarize_thin_and_thick_and_mixed():
    used, provisioned, kind = summarize_disks([_disk(80, True)], committed=12 * 1024**3, uncommitted=68 * 1024**3)
    assert kind == "thin"
    assert used == 12 * 1024**3
    assert provisioned == 80 * 1024**3

    _, _, thick = summarize_disks([_disk(40, False)], committed=40 * 1024**3, uncommitted=0)
    assert thick == "thick"

    _, _, mixed = summarize_disks([_disk(40, True), _disk(80, False)])
    assert mixed == "mixed"


def test_summarize_falls_back_to_disk_capacity():
    used, provisioned, kind = summarize_disks([_disk(60, True)])
    assert used == 0
    assert provisioned == 60 * 1024**3
    assert kind == "thin"


def test_classify_missing_thin_flag_as_thick():
    assert classify_backing(SimpleNamespace()) == "thick"
    assert classify_backing(None) == "unknown"


def test_ignores_non_disks():
    nic = SimpleNamespace(backing=SimpleNamespace(thinProvisioned=False))
    _, provisioned, kind = summarize_disks([nic, _disk(20, True)])
    assert kind == "thin"
    assert provisioned == 20 * 1024**3
