"""Portable identifiers shared by the protocol and dispatcher skill catalogue."""
from __future__ import annotations

import re
from typing import Any

MAX_SKILL_PROFILES = 16
SKILL_PROFILE_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def validate_skill_profile_ids(value: Any) -> list[str]:
    """Validate requested IDs without resolving them to host-specific profiles."""
    if not isinstance(value, list):
        raise ValueError("skill_profiles must be an array")
    if len(value) > MAX_SKILL_PROFILES:
        raise ValueError(f"skill_profiles must contain at most {MAX_SKILL_PROFILES} IDs")
    result: list[str] = []
    for profile_id in value:
        if (not isinstance(profile_id, str) or not 1 <= len(profile_id) <= 64
                or not SKILL_PROFILE_ID_PATTERN.fullmatch(profile_id)):
            raise ValueError("skill profile IDs must be 1-64 lowercase letters/digits separated by single hyphens")
        if profile_id in result:
            raise ValueError("skill_profiles must contain unique IDs")
        result.append(profile_id)
    return result
