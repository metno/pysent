"""pysent - Sentinel-1/Sentinel-2 SAFE to GeoTIFF quicklook processing.

Submodules:

``pysent.s1``
    Sentinel-1 amplitude quicklooks from SAFE or NetCDF: GCP warp to a target
    CRS, percentile grayscale stretch, tiled+compressed GeoTIFF with overviews.
``pysent.s2``
    Sentinel-2 RGB band-combination products from SAFE: stacked-VRT warp,
    per-band stretch, tiled+compressed 8-bit RGB with overviews.
``pysent.profiles``
    Platform detection (S1 vs S2) and per-platform profile defaults.
``pysent.archive``
    Catalogue download URL -> local NBS archive path.
``pysent.csw``
    CSW record lookup (extra: ``csw``).
``pysent.qa``
    Benchmark/quality harness: step timing, memory, stretch quality reports
    (extra: ``qa``).

Top-level names are resolved lazily, so importing :mod:`pysent.profiles` or
:mod:`pysent.csw` does not require the GDAL bindings that :mod:`pysent.s1` and
:mod:`pysent.s2` need.

    >>> from pysent.s2 import process_sentinel_s2_safe, S2_DEFAULT_PRODUCTS
    >>> results = process_sentinel_s2_safe(
    ...     input_dataset="/archive/S2A_MSIL1C_....zip",
    ...     output_dir=Path("/out"),
    ...     product_bands={"true_colour": S2_DEFAULT_PRODUCTS["true_colour"]},
    ...     output_names={"true_colour": "scene_true_colour.tif"},
    ... )
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # submodules
    "archive",
    "csw",
    "profiles",
    "qa",
    "s1",
    "s2",
    # sentinel-1
    "S1_SUPPORTED_AMPLITUDE_VARIABLES",
    "build_sentinel_s1_output_filename",
    "detect_sentinel_s1_polarizations",
    "process_sentinel_s1_netcdf",
    "process_sentinel_s1_safe",
    "stretch_sentinel_s1_grayscale",
    # sentinel-2
    "S2_DEFAULT_PRODUCTS",
    "S2_SUPPORTED_BANDS",
    "build_sentinel_s2_output_filename",
    "normalize_sentinel_s2_product_map",
    "process_sentinel_s2_safe",
    "stretch_sentinel_s2_rgb",
    # platform / archive
    "detect_nbs_sentinel_platform",
    "resolve_safe_archive_from_uuid",
]

_SUBMODULES = {"archive", "csw", "profiles", "qa", "s1", "s2"}

_EXPORTS = {
    "S1_SUPPORTED_AMPLITUDE_VARIABLES": "s1",
    "build_sentinel_s1_output_filename": "s1",
    "detect_sentinel_s1_polarizations": "s1",
    "process_sentinel_s1_netcdf": "s1",
    "process_sentinel_s1_safe": "s1",
    "stretch_sentinel_s1_grayscale": "s1",
    "S2_DEFAULT_PRODUCTS": "s2",
    "S2_SUPPORTED_BANDS": "s2",
    "build_sentinel_s2_output_filename": "s2",
    "normalize_sentinel_s2_product_map": "s2",
    "process_sentinel_s2_safe": "s2",
    "stretch_sentinel_s2_rgb": "s2",
    "detect_nbs_sentinel_platform": "profiles",
    "resolve_safe_archive_from_uuid": "archive",
}


def __getattr__(name: str) -> Any:
    """Resolve submodules and re-exported names on first access (PEP 562)."""
    if name in _SUBMODULES:
        return import_module(f".{name}", __name__)
    module_name = _EXPORTS.get(name)
    if module_name is not None:
        return getattr(import_module(f".{module_name}", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
