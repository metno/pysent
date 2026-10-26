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


def reject_empty_input(dataset_ref: str) -> None:
    """Raise :class:`~pysent.errors.EmptyInputError` if the input is a zero-byte file.

    The NBS archive carries zero-byte stubs in place of files that never
    arrived - a third of sampled near-real-time products - and a driver opening
    one reports a parse failure that reads like a corrupt product. Checking the
    size first turns that into one clear sentence.

    Only a file that is definitely empty is rejected: anything that cannot be
    stat'd (a directory, a remote path a driver will handle, a virtual file
    system that reports nothing) is left alone for GDAL to open as before.
    """
    from .errors import EmptyInputError

    if not (dataset_ref or "").strip():
        # 17 % of sampled Sentinel-3 catalogue records carry no reference at
        # all, so the caller resolves an archive path and gets nothing back.
        raise EmptyInputError(
            "no input dataset was given, so there is nothing to process. A catalogue record "
            "without a download reference cannot be ingested; check the record has one."
        )

    ref = dataset_ref.rstrip("/")
    if ref.startswith("/vsizip/"):
        ref = ref[len("/vsizip/"):].split(".zip", 1)[0] + ".zip"
    stat = gdal.VSIStatL(ref)
    if stat is not None and stat.IsDirectory():
        return
    if stat is not None and stat.size == 0:
        raise EmptyInputError(
            f"{ref} is an empty file (0 bytes), so there is nothing to process. "
            "The archive stores a zero-byte placeholder when a product never arrived; "
            "try a later version of the product or another processing stream."
        )


def resolve_safe_root(dataset_ref: str) -> str:
    """Return the ``.SAFE`` directory of a zip or SAFE reference.

    A zip (plain or ``/vsizip/``-prefixed) resolves to the ``.SAFE`` directory
    it contains, as a ``/vsizip/`` path; anything else is returned unchanged.

    A zero-byte input is rejected here, before any driver sees it.
    """
    reject_empty_input(dataset_ref)
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
