from __future__ import annotations


def normalize_power_state(value: object) -> str:
    """Map vSphere SOAP values (poweredOn) to REST-style labels (POWERED_ON)."""
    text = str(value or "UNKNOWN").strip()
    key = text.replace("_", "").lower()
    mapping = {
        "poweredon": "POWERED_ON",
        "poweredoff": "POWERED_OFF",
        "suspended": "SUSPENDED",
    }
    if key in mapping:
        return mapping[key]
    upper = text.upper()
    if upper in {"POWERED_ON", "POWERED_OFF", "SUSPENDED"}:
        return upper
    return text
