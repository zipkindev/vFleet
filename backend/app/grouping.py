from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

DEFAULT_PATTERN = r"^([A-Za-z][A-Za-z0-9]+)"


def normalize_principal(value: str) -> str:
    """Strip domain/UPN noise from a vCenter userName for grouping."""
    text = (value or "").strip()
    if not text:
        return ""
    if "\\" in text:
        text = text.rsplit("\\", 1)[-1]
    if "@" in text:
        text = text.split("@", 1)[0]
    return text.strip().lower()


def owner_from_fields(
    custom_fields: Mapping[str, str],
    field_names: Sequence[str],
) -> Optional[str]:
    lowered = {key.lower(): value for key, value in custom_fields.items() if value}
    for name in field_names:
        value = lowered.get(name.lower())
        if value:
            normalized = normalize_principal(value) or value.strip()
            return normalized
    return None


def owner_from_name(name: str, pattern: str = DEFAULT_PATTERN) -> str:
    text = (name or "").strip()
    if not text:
        return "unknown"
    try:
        match = re.search(pattern, text)
    except re.error:
        match = re.search(DEFAULT_PATTERN, text)
    if match:
        group = match.group(1) if match.lastindex else match.group(0)
        token = group.strip().rstrip("-_.")
        if token:
            return token.lower()
    return text.split()[0].lower()


def resolve_owner(
    name: str,
    custom_fields: Mapping[str, str],
    field_names: Sequence[str],
    pattern: str = DEFAULT_PATTERN,
) -> Tuple[str, str]:
    from_field = owner_from_fields(custom_fields, field_names)
    if from_field:
        return from_field.lower(), "custom_field"
    return owner_from_name(name, pattern), "name_prefix"


def resolve_deployed_by(
    custom_fields: Mapping[str, str],
    field_names: Sequence[str],
    event_user: str = "",
) -> str:
    """Prefer a named owner custom field, else the create/clone event principal."""
    from_field = owner_from_fields(custom_fields, field_names)
    if from_field:
        return from_field
    return normalize_principal(event_user)


def vm_matches_owner_query(owner_key: str, deployed_by: str, custom_fields: Mapping[str, str], needle: str) -> bool:
    """True when needle matches naming-group owner, deployer, or any custom-field value."""
    want = (needle or "").strip().lower()
    if not want:
        return True
    if (owner_key or "").lower() == want:
        return True
    if normalize_principal(deployed_by) == normalize_principal(want) or (deployed_by or "").lower() == want:
        return True
    for value in custom_fields.values():
        if not value:
            continue
        if value.lower() == want or normalize_principal(value) == normalize_principal(want):
            return True
    return False


def vm_matches_search(name: str, owner_key: str, deployed_by: str, custom_fields: Mapping[str, str], needle: str) -> bool:
    text = (needle or "").strip().lower()
    if not text:
        return True
    if text in (name or "").lower():
        return True
    if text in (owner_key or "").lower():
        return True
    if text in (deployed_by or "").lower() or text in normalize_principal(deployed_by):
        return True
    for value in custom_fields.values():
        if value and text in value.lower():
            return True
    return False


def similar_prefix_groups(
    names: Iterable[str],
    min_length: int = 3,
) -> Dict[str, List[str]]:
    """Group names that share the same leading token (corp/user prefix)."""
    buckets: Dict[str, List[str]] = defaultdict(list)
    for name in names:
        key = owner_from_name(name)
        if len(key) >= min_length:
            buckets[key].append(name)
        else:
            buckets["ungrouped"].append(name)
    return dict(buckets)
