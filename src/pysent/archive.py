"""Mapping catalogue download URLs onto a local NBS archive mount.

The catalogue advertises products as HTTP download URLs; processing needs the
local path of the SAFE ``.zip``. Both sides of that mapping live here so the
service router and the QA harness cannot drift apart.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_ARCHIVE_MARKER",
    "DEFAULT_ARCHIVE_ROOT",
    "SafeResolution",
    "extract_download_url",
    "map_archive_download_to_local",
    "resolve_archive_root",
    "resolve_safe_archive_from_uuid",
]

_DOWNLOAD_SCHEME = "WWW:DOWNLOAD-1.0-http--download"

#: The portion of a catalogue download URL after this marker is the product path
#: *relative to* the archive root, e.g.
#:   https://.../nbsArchive/S2A/2022/03/19/S2A_....zip
#:                          ^^^^^^^ marker  ^^^^^^^^^^^^^^^^^^^^^^^^ relative path
DEFAULT_ARCHIVE_MARKER = "nbsArchive/"
DEFAULT_ARCHIVE_ROOT = "/app/data/nbsArchive"


def resolve_archive_root(archive_root: str | os.PathLike[str] | None = None) -> str:
    """Local archive mount: explicit value, else ``NBS_ARCHIVE_ROOT``, else the default."""
    if archive_root:
        return str(archive_root).strip()
    return (os.environ.get("NBS_ARCHIVE_ROOT") or DEFAULT_ARCHIVE_ROOT).strip()


def extract_download_url(record: Any) -> str | None:
    """Return the ``WWW:DOWNLOAD-1.0-http--download`` URL from a CSW record."""
    references = getattr(record, "references", None) or []
    for ref in references:
        scheme = (ref.get("scheme") or ref.get("protocol") or "").strip()
        url = (ref.get("url") or "").strip()
        if scheme == _DOWNLOAD_SCHEME and url:
            return url
    return None


def map_archive_download_to_local(
    download_url: str | None,
    archive_root: str | os.PathLike[str] | None = None,
    marker: str = DEFAULT_ARCHIVE_MARKER,
) -> str | None:
    """Rewrite a catalogue download URL as a path under the local archive root.

    Returns ``None`` when there is no URL or it does not carry ``marker``. The
    returned path is *not* checked for existence - callers that care should test
    it (the SAFE may simply not be mirrored locally).
    """
    if not download_url or marker not in download_url:
        return None
    rel = download_url.split(marker, 1)[1].lstrip("/")
    return str(Path(resolve_archive_root(archive_root)) / rel)


@dataclass
class SafeResolution:
    """Everything a UUID lookup yields about a product's local SAFE archive."""

    identifier: str
    safe_path: Path
    download_url: str | None
    endpoint: str
    platform: dict[str, str]
    metadata: dict[str, str]
    exists: bool


def resolve_safe_archive_from_uuid(
    identifier: str,
    *,
    endpoint: str | None = None,
    archive_root: str | None = None,
    archive_marker: str = DEFAULT_ARCHIVE_MARKER,
) -> SafeResolution:
    """Resolve a CSW record UUID to its local SAFE ``.zip`` archive path.

    Needs the ``csw`` extra. ``archive_root`` is the local mount of the NBS
    archive (machine-specific, e.g.
    ``/lustre/storeB/NBS2/sentinel/production/NorwAREA/nbsArchive``); it falls
    back to ``NBS_ARCHIVE_ROOT``.
    """
    from .csw import DEFAULT_CSW_ENDPOINT, DEFAULT_NBS_SENTINEL_CSW_ENDPOINT, get_record_by_id
    from .profiles import detect_nbs_sentinel_platform

    identifier = (identifier or "").strip()
    if not identifier:
        raise ValueError("identifier (UUID) is required")

    endpoint = (
        (endpoint or "").strip()
        or os.environ.get("NBS_SENTINEL_CSW_ENDPOINT", "").strip()
        or DEFAULT_NBS_SENTINEL_CSW_ENDPOINT
        or DEFAULT_CSW_ENDPOINT
    )

    record = get_record_by_id(endpoint, identifier)
    if record is None:
        raise LookupError(f"No CSW record found for identifier {identifier!r} at {endpoint}")

    metadata = {
        "title": str(getattr(record, "title", "") or ""),
        "identifier": str(getattr(record, "identifier", "") or "") or identifier,
        "abstract": str(getattr(record, "abstract", "") or ""),
    }
    download_url = extract_download_url(record)
    local = map_archive_download_to_local(download_url, archive_root, archive_marker)
    safe_path = Path(local) if local else Path(download_url or "")
    platform = detect_nbs_sentinel_platform(metadata["identifier"], metadata["title"], download_url or "")

    return SafeResolution(
        identifier=identifier,
        safe_path=safe_path,
        download_url=download_url,
        endpoint=endpoint,
        platform=platform,
        metadata=metadata,
        exists=safe_path.exists() and safe_path.is_file(),
    )
