"""Sentinel platform detection, product routing and per-platform profile defaults.

Detection (:func:`detect_nbs_sentinel_platform`) is pure string matching over
whatever hints you have - identifier, title, filename, OPeNDAP URL - and is what
decides which pipeline a product goes down. :func:`detect_nbs_sentinel_product`
adds the product type on top, and :func:`resolve_sentinel_pipeline` turns that
into a pipeline name or a specific refusal.

Nothing here opens a file, so a caller can route and reject a job for free.

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
    "SENTINEL_FAMILIES",
    "SENTINEL_PRODUCT_TYPES",
    "apply_nbs_sentinel_metadata_profile",
    "detect_nbs_sentinel_platform",
    "detect_nbs_sentinel_platform_for_dataset",
    "detect_nbs_sentinel_product",
    "detect_nbs_sentinel_product_for_dataset",
    "get_nbs_sentinel_platform_profiles",
    "normalize_nbs_sentinel_platform_profiles",
    "resolve_nbs_sentinel_platform_profile",
    "resolve_sentinel_pipeline",
    "supported_sentinel_product_types",
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


SENTINEL_FAMILIES = ("S1", "S2", "S3", "S5P")

# A whole token, not preceded or followed by a letter or digit, so underscore,
# slash, dot, dash, space and the ends of the string are all boundaries.
#
# The anchors are the point. An unanchored ``S[12][A-Z]?`` finds "S1" inside the
# processing-centre token of a Sentinel-3 title
# (``..._1440_PS1_O_NT_002``), which classified 193 of 630 sampled Sentinel-3
# records (31 %) as Sentinel-1 or Sentinel-2. Since anything not recognised as
# S2 was sent down the S1 path, those records were processed as SAR. Anchored,
# all 2,650 audited records of all four families classify correctly.
#
# Sentinel-5P carries no unit letter, so ``S5P`` is the whole token.
_PLATFORM_TOKEN = re.compile(r"(?<![A-Z0-9])S(?:5P|[123][A-Z]?)(?![A-Z0-9])")


def _family_of_token(token: str) -> str:
    if token.startswith("S5P"):
        return "S5P"
    return token[:2] if token[:2] in SENTINEL_FAMILIES else "unknown"


def detect_nbs_sentinel_platform(*values: str) -> dict[str, str]:
    """Detect the Sentinel platform from any number of textual hints.

    Each value is scanned for a whole ``S1``/``S2``/``S3``/``S5P`` token,
    optionally carrying the unit letter (``S1A``, ``S3B``; Sentinel-5P has
    none). A three-character match wins and stops the scan; otherwise the
    first two-character match is kept.

    Returns ``{"family": "S1"|"S2"|"S3"|"S5P"|"unknown", "platform": <token>}``.
    """
    token = ""
    for value in values:
        if not value:
            continue
        matches = _PLATFORM_TOKEN.findall(value.upper())
        if matches:
            exact = next((match for match in matches if len(match) == 3), None)
            token = exact or matches[0]
            if exact:
                break
    family = _family_of_token(token)
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


def _dataset_hints(info: Any) -> tuple[str, ...]:
    return (
        getattr(info, "identifier", "") or "",
        getattr(info, "title", "") or "",
        getattr(info, "source_dataset", "") or "",
        getattr(info, "opendap_url", "") or "",
        getattr(info, "filename", "") or "",
    )


# Product type as the product *title* spells it. The title is authoritative;
# the archive path is only a fallback, because it abbreviates (``TR-L2-NO2``).
_TITLE_S5P = re.compile(r"S5P_(?P<stream>[A-Z]{4})_(?P<type>.{10})_")
_TITLE_S3 = re.compile(r"S3[A-D]_(?P<instrument>[A-Z]{2})_(?P<level>[0-9])_(?P<type>.{6})_")
_TITLE_S1 = re.compile(r"S1[A-D]_(?P<mode>[A-Z]{2})_(?P<type>[A-Z_]{4})_")
_TITLE_S2 = re.compile(r"S2[A-D]_(?P<type>MSIL[0-9A-Z]{2})")
# Archive layout, e.g. .../nbsArchive/S5P/2026/10/09/TR-L2-NO2/... or OL-L2-WFR,
# SY-L2-VG1. The type may itself contain dashes: TR-L2-AER-AI, TR-L2-O3-TCL.
_PATH_TYPE = re.compile(
    r"(?<![A-Z0-9])(?P<instrument>[A-Z]{2})-L(?P<level>[0-9])-(?P<type>[A-Z0-9]+(?:-[A-Z0-9]+)*)(?![A-Z0-9])"
)

# The Sentinel-5P directory names, mapped to the type as a product *title*
# spells it. A rule cannot derive these: the archive writes ``TR-L2-O3-PR`` for
# a type whose real name is ``L2__O3__PR``, with two underscores, and
# ``TR-L2-AER-AI`` for ``L2__AER_AI``, with one. Listed explicitly and checked
# against the archive's own directory listing, because getting it wrong is not
# cosmetic: ``TR-L2-O3-TCL`` would otherwise reduce to ``L2__O3____`` and a
# deliberately deferred product would be processed as a supported one.
_S5P_ARCHIVE_TYPES = {
    "NO2": "L2__NO2___",
    "CO": "L2__CO____",
    "HCHO": "L2__HCHO__",
    "SO2": "L2__SO2___",
    "O3": "L2__O3____",
    "O3-PR": "L2__O3__PR",
    "O3-TCL": "L2__O3_TCL",
    "AER-AI": "L2__AER_AI",
    "AER-LH": "L2__AER_LH",
    "CH4": "L2__CH4___",
    "CLOUD": "L2__CLOUD_",
    "NP-BD3": "L2__NP_BD3",
    "NP-BD6": "L2__NP_BD6",
    "NP-BD7": "L2__NP_BD7",
}
_S5P_STREAMS = ("NRTI", "OFFL", "RPRO")
_S3_INSTRUMENTS = ("OL", "SL", "SY", "SR")


def detect_nbs_sentinel_product(*values: str) -> dict[str, str]:
    """Detect family, platform, product type and (Sentinel-5P) processing stream.

    Adds to :func:`detect_nbs_sentinel_platform` the product type as the title
    spells it, normalised the way the archive and the documentation do:

    =========  ===================================  ==================
    Family     Example title                        ``product_type``
    =========  ===================================  ==================
    S1         ``S1A_IW_GRDH_1SDV_2026...``         ``GRDH``
    S2         ``S2A_MSIL1C_2022...``               ``MSIL1C``
    S3         ``S3A_OL_2_WFR____2026...``          ``OL_2_WFR``
    S5P        ``S5P_OFFL_L2__NO2____2026...``      ``L2__NO2___``
    =========  ===================================  ==================

    Sentinel-5P also gets ``stream`` (``NRTI``, ``OFFL`` or ``RPRO``), which
    decides how current the data is and, in practice, how likely the file is to
    be a zero-byte stub. The other families get ``""``.

    The *product baseline* is deliberately not parsed here. The name carries a
    baseline field, but it does not reliably match the real one, so it has to
    be read from the manifest instead.

    Any hint may carry the type; the title wins, then the archive path. Values
    are scanned in the order given, so pass the title early.

    Returns ``{"family", "platform", "product_type", "stream"}``, with
    ``product_type`` and ``stream`` empty when nothing recognisable was found.
    """
    detected = detect_nbs_sentinel_platform(*values)
    product_type = ""
    stream = ""

    for value in values:
        if not value:
            continue
        text = value.upper()
        match = _TITLE_S5P.search(text)
        if match and match.group("stream") in _S5P_STREAMS:
            stream = match.group("stream")
            product_type = match.group("type")
            break
        match = _TITLE_S3.search(text)
        if match and match.group("instrument") in _S3_INSTRUMENTS:
            product_type = f"{match.group('instrument')}_{match.group('level')}_{match.group('type').rstrip('_')}"
            break
        match = _TITLE_S1.search(text)
        if match:
            product_type = match.group("type").rstrip("_")
            break
        match = _TITLE_S2.search(text)
        if match:
            product_type = match.group("type")
            break

    if not product_type:
        for value in values:
            if not value:
                continue
            match = _PATH_TYPE.search(value.upper())
            if not match:
                continue
            instrument, level, short = match.group("instrument", "level", "type")
            if instrument == "TR":
                # The archive abbreviates the Sentinel-5P type. Only the names
                # it actually uses are accepted: an unknown one is left for the
                # caller to see as "type not recognised" rather than guessed at
                # and silently routed to the wrong pipeline.
                product_type = _S5P_ARCHIVE_TYPES.get(short, "")
                path_family = "S5P"
            elif instrument in _S3_INSTRUMENTS:
                product_type = f"{instrument}_{level}_{short.replace('-', '_')}"
                path_family = "S3"
            else:
                continue
            if not product_type:
                continue
            # These directory names belong to one family each, so a path with no
            # filename in it still routes instead of being refused as unknown.
            if detected["family"] == "unknown":
                detected = {"family": path_family, "platform": path_family}
            break

    return {**detected, "product_type": product_type, "stream": stream}


def detect_nbs_sentinel_product_for_dataset(info: Any) -> dict[str, str]:
    """:func:`detect_nbs_sentinel_product` on any object carrying dataset hints.

    Duck-typed exactly like :func:`detect_nbs_sentinel_platform_for_dataset`.
    """
    return detect_nbs_sentinel_product(*_dataset_hints(info))


# Which product types this release routes where, and why the others are refused.
#
# ``ready``       a pipeline exists now.
# ``planned``     in the plan, not implemented yet - it must refuse loudly
#                 rather than be routed somewhere that cannot read it.
# ``unsupported`` deliberately out of scope; the note says why.
#
# Anything absent from this table is unsupported with a generic message. Later
# phases flip their own rows from ``planned`` to ``ready``; nothing else needs
# to change for a new product type to become routable.
SENTINEL_PRODUCT_TYPES: dict[tuple[str, str], tuple[str, str, str]] = {
    # family, type              pipeline  status         note
    ("S1", "GRDH"): ("s1", "ready", ""),
    ("S1", "GRDM"): ("s1", "ready", ""),
    ("S1", "GRD"): ("s1", "ready", ""),
    ("S1", "SLC"): ("s1", "unsupported", "single-look complex data needs a different pipeline"),
    ("S2", "MSIL1C"): ("s2", "ready", ""),
    ("S2", "MSIL2A"): ("s2", "unsupported", "surface reflectance has its own masks and scaling"),
    ("S5P", "L2__NO2___"): ("s5p", "ready", ""),
    ("S5P", "L2__CO____"): ("s5p", "ready", ""),
    ("S5P", "L2__HCHO__"): ("s5p", "ready", ""),
    ("S5P", "L2__SO2___"): ("s5p", "ready", ""),
    ("S5P", "L2__O3____"): ("s5p", "ready", ""),
    ("S5P", "L2__AER_AI"): ("s5p", "ready", ""),
    ("S5P", "L2__CH4___"): ("s5p", "ready", ""),
    ("S5P", "L2__CLOUD_"): ("s5p", "ready", ""),
    ("S5P", "L2__AER_LH"): ("s5p", "ready", ""),
    ("S5P", "L2__O3__PR"): ("s5p", "ready", ""),
    ("S5P", "L2__O3_TCL"): ("s5p", "unsupported", "a gridded product with a different shape, and most records have no download reference"),
    ("S5P", "L2__NP_BD3"): ("s5p", "unsupported", "VIIRS co-registration, not a map"),
    ("S5P", "L2__NP_BD6"): ("s5p", "unsupported", "VIIRS co-registration, not a map"),
    ("S5P", "L2__NP_BD7"): ("s5p", "unsupported", "VIIRS co-registration, not a map"),
    ("S3", "SY_2_VG1"): ("s3", "ready", ""),
    ("S3", "SY_2_V10"): ("s3", "ready", ""),
    ("S3", "OL_2_WFR"): ("s3", "planned", "phase 4"),
    ("S3", "OL_2_LFR"): ("s3", "planned", "phase 4"),
    ("S3", "OL_2_WRR"): ("s3", "planned", "phase 4"),
    ("S3", "OL_2_LRR"): ("s3", "planned", "phase 4"),
    ("S3", "SL_2_LST"): ("s3", "planned", "phase 5"),
    ("S3", "SL_2_WST"): ("s3", "planned", "phase 5"),
    ("S3", "SL_2_AOD"): ("s3", "planned", "phase 5"),
    ("S3", "SY_2_AOD"): ("s3", "planned", "phase 5"),
    ("S3", "SY_2_VGP"): ("s3", "unsupported", "355 Mpx per band; needs a decimated read first"),
    ("S3", "OL_1_EFR"): ("s3", "unsupported", "radiance only; the reflectance conversion is not validated"),
    ("S3", "OL_1_ERR"): ("s3", "unsupported", "radiance only; the reflectance conversion is not validated"),
    ("S3", "SY_2_SYN"): ("s3", "unsupported", "40 members on several grids"),
    ("S3", "SL_1_RBT"): ("s3", "unsupported", "99 members on several grids"),
    ("S3", "SL_2_FRP"): ("s3", "unsupported", "fire points are vector data, not a raster"),
    ("S3", "SR_2_WAT"): ("s3", "unsupported", "along-track altimetry is vector data, not a raster"),
}

_FAMILY_LABELS = {"S1": "Sentinel-1", "S2": "Sentinel-2", "S3": "Sentinel-3", "S5P": "Sentinel-5P"}


def supported_sentinel_product_types() -> list[str]:
    """The product types a caller can process right now, as ``"S2 MSIL1C"`` strings."""
    return [
        f"{family} {product_type}"
        for (family, product_type), (_, status, _) in SENTINEL_PRODUCT_TYPES.items()
        if status == "ready"
    ]


def resolve_sentinel_pipeline(info: Any) -> str:
    """Return the pipeline name for a product, or refuse it with a reason.

    ``info`` may be the dict from :func:`detect_nbs_sentinel_product`, a plain
    string (a title, filename or archive path), or any object carrying the
    usual dataset attributes.

    Returns ``"s1"``, ``"s2"``, ``"s3"`` or ``"s5p"``: the name of the pipeline
    that handles this product, which is also the name of the pysent module.

    Raises :class:`pysent.errors.UnsupportedProductError` when the family
    cannot be identified, when the product type is out of scope, and when the
    type is planned but not implemented in this release. In particular an
    unidentified product is **refused, never routed to Sentinel-1** - that
    fall-through is what sent 31 % of Sentinel-3 records down the SAR path.
    """
    from .errors import UnsupportedProductError

    if isinstance(info, dict):
        detected = info if "product_type" in info else {**info, "product_type": "", "stream": ""}
    elif isinstance(info, str):
        detected = detect_nbs_sentinel_product(info)
    else:
        detected = detect_nbs_sentinel_product_for_dataset(info)

    family = detected.get("family") or "unknown"
    product_type = detected.get("product_type") or ""
    supported = ", ".join(supported_sentinel_product_types()) or "nothing"

    if family not in SENTINEL_FAMILIES:
        raise UnsupportedProductError(
            "could not tell which Sentinel family this product belongs to, so it will not be "
            f"processed; pass a title, filename or archive path that names one of {', '.join(SENTINEL_FAMILIES)}. "
            f"Supported product types: {supported}"
        )

    label = _FAMILY_LABELS[family]
    if not product_type:
        raise UnsupportedProductError(
            f"recognised {label} but not its product type, so it will not be processed; "
            f"pass the product title. Supported product types: {supported}"
        )

    entry = SENTINEL_PRODUCT_TYPES.get((family, product_type))
    if entry is None:
        raise UnsupportedProductError(
            f"{label} {product_type} is not a product type pysent knows; "
            f"supported product types: {supported}"
        )

    pipeline, status, note = entry
    if status == "ready":
        return pipeline
    if status == "planned":
        raise UnsupportedProductError(
            f"{label} {product_type} is planned ({note}) but not implemented in this release; "
            f"supported product types: {supported}"
        )
    raise UnsupportedProductError(
        f"{label} {product_type} is not supported: {note}. Supported product types: {supported}"
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
