"""CSW record lookup for NBS Sentinel products.

Requires the optional ``csw`` extra (``pip install pysent[csw]``) because it
pulls in OWSLib; the rest of pysent has no need for it. The OWSLib import is
therefore deferred to call time so ``import pysent.csw`` never hard-fails.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_CSW_ENDPOINT",
    "DEFAULT_NBS_SENTINEL_CSW_ENDPOINT",
    "check_quicklooks",
    "extract_archive_path",
    "get_record_by_id",
    "resolve_nbs_nc_directory",
]

logger = logging.getLogger(__name__)

#: Generic CSW endpoint; empty unless ``CSW_ENDPOINT`` is set.
DEFAULT_CSW_ENDPOINT = os.environ.get("CSW_ENDPOINT", "").strip()
#: NBS Sentinel catalogue endpoint.
DEFAULT_NBS_SENTINEL_CSW_ENDPOINT = os.environ.get("NBS_SENTINEL_CSW_ENDPOINT", "https://nbs.csw.met.no").strip()

_DOWNLOAD_SCHEME = "WWW:DOWNLOAD-1.0-http--download"
_ARCHIVE_MARKER = "nbsArchive/"


def _catalogue(endpoint: str):
    try:
        from owslib.csw import CatalogueServiceWeb
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise ImportError(
            "pysent.csw needs OWSLib; install it with `pip install pysent[csw]`."
        ) from exc
    return CatalogueServiceWeb(endpoint)


def get_record_by_id(endpoint: str, record_id: str) -> Any | None:
    """Fetch a single CSW record by identifier, or ``None`` if absent."""
    csw = _catalogue(endpoint)
    csw.getrecordbyid(id=[record_id])
    if record_id in csw.records:
        return csw.records[record_id]
    return None


def extract_archive_path(record: Any, *, marker: str = _ARCHIVE_MARKER) -> str | None:
    """Return the archive-relative product path from a record's download link.

    Looks for the ``WWW:DOWNLOAD-1.0-http--download`` reference, splits its URL
    on ``nbsArchive/`` and drops the ``.zip`` suffix - e.g.
    ``S1A/2026/04/11/IW/S1A_IW_GRDH_1SDV_...``.
    """
    references = getattr(record, "references", None)
    if not references:
        logger.warning("The record has no 'references' key.")
        return None

    found_url = None
    for ref in references:
        if ref.get("scheme") != _DOWNLOAD_SCHEME:
            continue
        found_url = ref.get("url")
        logger.debug("Found download link: %s", found_url)
        if found_url and marker in found_url:
            extracted_path = found_url.split(marker)[-1].removesuffix(".zip")
            logger.debug("Extracted_path: %s", extracted_path)
            return extracted_path

    if not found_url:
        logger.warning("No reference matched the specific scheme value.")
    return None


def resolve_nbs_nc_directory(env_val: str | None = None) -> str:
    """Pick the first NBS NetCDF root that exists and is non-empty."""
    configured = os.environ.get("NBS_DATA_ROOT", "").strip()
    candidates = [
        "/app/data/NBS",
        "/usr/share/lustre/storeB/NBS",
        "/usr/share/lustre/storeA/NBS",
    ]
    for candidate in [env_val, configured]:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_dir() and any(path.iterdir()):
            return str(path)

    for candidate in candidates:
        path = Path(candidate)
        if path.is_dir() and any(path.iterdir()):
            return str(path)

    return env_val or configured or candidates[0]


def check_quicklooks(base_dir: str | Path, extracted_path: str | Path) -> list[str] | None:
    """List pre-rendered quicklook GeoTIFFs for a product, if any exist.

    The archive stores them under a ``ql`` directory injected between the
    product's parent path and its name.
    """
    product = Path(extracted_path)
    full_path = Path(base_dir) / product.parent / "ql" / product.name

    if not (full_path.exists() and full_path.is_dir()):
        logger.warning("Directory does not exist: %s", full_path)
        return None

    # Matches .tif, .tiff, .TIF, .TIFF
    geotiff_list = [str(f.resolve()) for f in full_path.glob("*.[tT][iI][fF]*") if f.is_file()]
    if not geotiff_list:
        logger.warning("Directory exists, but no GeoTIFFs found in: %s", full_path)
        return None
    return geotiff_list
