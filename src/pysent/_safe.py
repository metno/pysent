"""Locate the files GDAL opens inside a SAFE product, zipped or unpacked.

Neither a ``.zip`` nor a ``.SAFE`` directory is itself something GDAL can open:
the Sentinel-2 driver wants the ``MTD_MSIL*.xml`` metadata file and the SAFE
driver wants ``manifest.safe``. These helpers find them by listing the product
rather than by assuming the ``.SAFE`` directory is named after the zip, so a
renamed zip still resolves.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from osgeo import gdal


def _list_dir(path: str) -> list[str]:
    return [entry.rstrip("/") for entry in (gdal.ReadDir(path) or []) if entry not in (".", "..")]


def resolve_safe_root(dataset_ref: str) -> str:
    """Return the ``.SAFE`` directory of a zip or SAFE reference.

    A zip (plain or ``/vsizip/``-prefixed) resolves to the ``.SAFE`` directory
    it contains, as a ``/vsizip/`` path; anything else is returned unchanged.
    """
    ref = dataset_ref.rstrip("/")
    if not ref.lower().endswith(".zip"):
        return ref
    container = ref if ref.startswith("/vsizip/") else f"/vsizip/{ref}"
    expected = f"{PurePosixPath(ref).stem}.SAFE"
    safe_dirs = [entry for entry in _list_dir(container) if entry.upper().endswith(".SAFE")]
    if expected not in safe_dirs and len(safe_dirs) == 1:
        return f"{container}/{safe_dirs[0]}"
    # Listing failed, or several candidates: fall back to the naming convention.
    return f"{container}/{expected}"


def find_safe_member(directory: str, pattern: re.Pattern[str]) -> str | None:
    """Return the path of the first entry in ``directory`` whose name fully matches ``pattern``."""
    for entry in sorted(_list_dir(directory)):
        if pattern.fullmatch(entry):
            return f"{directory}/{entry}"
    return None
