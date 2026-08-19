from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

DEFAULT_PATTERN = r"^([A-Za-z][A-Za-z0-9]+)"


def owner_from_fields(
    custom_fields: Mapping[str, str],
    field_names: Sequence[str],
) -> Optional[str]:
    lowered = {key.lower(): value for key, value in custom_fields.items() if value}
    for name in field_names:
        value = lowered.get(name.lower())
        if value:
            return value.strip()
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
