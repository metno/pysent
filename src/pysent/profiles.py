"""Sentinel platform detection and per-platform profile defaults.

Detection (:func:`detect_nbs_sentinel_platform`) is pure string matching over
whatever hints you have - identifier, title, filename, OPeNDAP URL - and is what
decides whether a product goes down the S1 or the S2 path.

The profile helpers layer optional per-platform defaults (which variables to
query, metadata defaults/overrides) on top of that. Where those overrides come
from is the *caller's* business: pass them in as ``configured``. With no
``configured`` value the environment variable
``NBS_SENTINEL_PLATFORM_PROFILES_JSON`` is consulted instead. This keeps the
library free of any particular application's configuration store.
"""
from __future__ import annotations

import copy
import json
import os
import re
from typing import Any

__all__ = [
    "DEFAULT_NBS_SENTINEL_PLATFORM_PROFILES",
    "PROFILE_ENV_VAR",
    "apply_nbs_sentinel_metadata_profile",
    "detect_nbs_sentinel_platform",
    "detect_nbs_sentinel_platform_for_dataset",
    "get_nbs_sentinel_platform_profiles",
    "normalize_nbs_sentinel_platform_profiles",
    "resolve_nbs_sentinel_platform_profile",
]

PROFILE_ENV_VAR = "NBS_SENTINEL_PLATFORM_PROFILES_JSON"
_PROFILE_ENV_VAR = PROFILE_ENV_VAR  # backwards-compatible private alias
_METADATA_KEYS = ("title", "identifier", "abstract")
_PROFILE_KEYS = ("query_variables", "metadata_defaults", "metadata_overrides")

DEFAULT_NBS_SENTINEL_PLATFORM_PROFILES: dict[str, dict[str, object]] = {
    "__default__": {
        "query_variables": [],
        "metadata_defaults": {},
        "metadata_overrides": {},
    },
    "S1": {
        "query_variables": ["Amplitude_VH", "Amplitude_VV"],
        "metadata_defaults": {},
        "metadata_overrides": {},
    },
    "S2": {
        "query_variables": ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8A", "B9", "B10", "B11", "B12"],
        "metadata_defaults": {},
        "metadata_overrides": {},
    },
}


def detect_nbs_sentinel_platform(*values: str) -> dict[str, str]:
    """Detect the Sentinel platform from any number of textual hints.

    Each value is scanned for an ``S1``/``S2`` token, optionally carrying the
    unit letter (``S1A``, ``S2B``). A three-character match wins and stops the
    scan; otherwise the first two-character match is kept.

    Returns ``{"family": "S1"|"S2"|"unknown", "platform": <token>}``.
    """
    token = ""
    for value in values:
        if not value:
            continue
        matches = re.findall(r"S[12](?:[A-Z])?", value.upper())
        if matches:
            exact = next((match for match in matches if len(match) == 3), None)
            token = exact or matches[0]
            if exact:
                break
    family = token[:2] if token[:2] in {"S1", "S2"} else "unknown"
    return {
        "family": family,
        "platform": token or family or "unknown",
    }


def detect_nbs_sentinel_platform_for_dataset(info: Any) -> dict[str, str]:
    """Detect the platform from any object carrying the usual dataset hints.

    Duck-typed on purpose: anything exposing ``identifier``/``title``/
    ``source_dataset``/``opendap_url``/``filename`` attributes works, so callers
    need not import a particular dataset model.
    """
    return detect_nbs_sentinel_platform(
        getattr(info, "identifier", "") or "",
        getattr(info, "title", "") or "",
        getattr(info, "source_dataset", "") or "",
        getattr(info, "opendap_url", "") or "",
        getattr(info, "filename", "") or "",
    )


def _normalize_metadata_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key in _METADATA_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[key] = text
    return out


def _normalize_profile(raw: Any) -> dict[str, object]:
    profile = {
        "query_variables": [],
        "metadata_defaults": {},
        "metadata_overrides": {},
    }
    if not isinstance(raw, dict):
        return profile

    query_variables = raw.get("query_variables")
    if isinstance(query_variables, (list, tuple)):
        profile["query_variables"] = [str(item).strip() for item in query_variables if str(item).strip()]

    profile["metadata_defaults"] = _normalize_metadata_map(raw.get("metadata_defaults"))
    profile["metadata_overrides"] = _normalize_metadata_map(raw.get("metadata_overrides"))
    return profile


def normalize_nbs_sentinel_platform_profiles(raw: Any) -> dict[str, dict[str, object]]:
    """Coerce an arbitrary profile mapping into the canonical shape.

    Keys are upper-cased; ``DEFAULT`` is folded onto ``__DEFAULT__``. Profiles
    that carry nothing usable are dropped.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, object]] = {}
    for key, value in raw.items():
        profile_key = str(key or "").strip().upper()
        if not profile_key:
            continue
        if profile_key == "DEFAULT":
            profile_key = "__DEFAULT__"
        normalized = _normalize_profile(value)
        if any(normalized[name] for name in _PROFILE_KEYS):
            out[profile_key] = normalized
    return out


_normalize_profiles = normalize_nbs_sentinel_platform_profiles  # backwards-compatible private alias


def _load_env_profile_overrides() -> dict[str, dict[str, object]]:
    raw = (os.environ.get(PROFILE_ENV_VAR) or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return normalize_nbs_sentinel_platform_profiles(parsed)


def get_nbs_sentinel_platform_profiles(configured: Any = None) -> dict[str, dict[str, object]]:
    """Merge caller-supplied profile overrides onto the built-in defaults.

    ``configured`` is the raw override mapping from wherever the application
    keeps its configuration. Pass ``None`` (the default) to fall back to the
    ``NBS_SENTINEL_PLATFORM_PROFILES_JSON`` environment variable.
    """
    profiles = copy.deepcopy(DEFAULT_NBS_SENTINEL_PLATFORM_PROFILES)
    overrides = (
        normalize_nbs_sentinel_platform_profiles(configured)
        if configured is not None
        else _load_env_profile_overrides()
    )

    for key, override in overrides.items():
        existing = _normalize_profile(profiles.get(key, {}))
        merged = {
            "query_variables": list(existing["query_variables"]),
            "metadata_defaults": dict(existing["metadata_defaults"]),
            "metadata_overrides": dict(existing["metadata_overrides"]),
        }
        if override["query_variables"]:
            merged["query_variables"] = list(override["query_variables"])
        merged["metadata_defaults"].update(override["metadata_defaults"])
        merged["metadata_overrides"].update(override["metadata_overrides"])
        profiles[key] = merged
    return profiles


def resolve_nbs_sentinel_platform_profile(
    platform_info: dict[str, str] | None,
    *,
    profiles: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    """Collapse the ``__default__`` -> family -> platform profile chain into one.

    Pass ``profiles`` when the application has already resolved them (so its
    configuration store is consulted once); otherwise they are resolved here
    from the built-in defaults plus the environment.
    """
    info = platform_info or {}
    family = str(info.get("family") or "unknown").strip().upper() or "UNKNOWN"
    platform = str(info.get("platform") or family).strip().upper() or family
    if profiles is None:
        profiles = get_nbs_sentinel_platform_profiles()
    merged = {
        "query_variables": [],
        "metadata_defaults": {},
        "metadata_overrides": {},
        "applied_profiles": [],
    }
    for key in ("__default__", family, platform):
        profile = profiles.get(key.upper()) if key != "__default__" else profiles.get("__default__")
        if not profile:
            continue
        merged["applied_profiles"].append(key if key == "__default__" else key.upper())
        if profile["query_variables"]:
            merged["query_variables"] = list(profile["query_variables"])
        merged["metadata_defaults"].update(profile["metadata_defaults"])
        merged["metadata_overrides"].update(profile["metadata_overrides"])
    return merged


def apply_nbs_sentinel_metadata_profile(
    metadata: dict[str, str] | None,
    profile: dict[str, object] | None,
) -> dict[str, str]:
    """Fill in ``metadata_defaults`` then force ``metadata_overrides``."""
    merged = {key: str((metadata or {}).get(key) or "").strip() for key in _METADATA_KEYS}
    defaults = (profile or {}).get("metadata_defaults")
    overrides = (profile or {}).get("metadata_overrides")

    if isinstance(defaults, dict):
        for key in _METADATA_KEYS:
            if merged.get(key):
                continue
            value = str(defaults.get(key) or "").strip()
            if value:
                merged[key] = value

    if isinstance(overrides, dict):
        for key in _METADATA_KEYS:
            value = str(overrides.get(key) or "").strip()
            if value:
                merged[key] = value
    return merged
