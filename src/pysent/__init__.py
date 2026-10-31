"""pysent - Sentinel-1/Sentinel-2 SAFE to GeoTIFF quicklook processing.

Submodules:

``pysent.s1``
    Sentinel-1 amplitude quicklooks from SAFE or NetCDF: GCP warp to a target
    CRS, percentile grayscale stretch, tiled+compressed GeoTIFF with overviews.
``pysent.s2``
    Sentinel-2 RGB band-combination products from SAFE: stacked-VRT warp,
    per-band stretch, tiled+compressed 8-bit RGB with overviews.
``pysent.errors``
    ``EmptySceneError`` and ``PartialFailure``, for callers that need to tell
    an empty scene or a partly failed call from other errors.
``pysent.profiles``
    Platform detection (S1 vs S2) and per-platform profile defaults.
``pysent.composite``
    Several granules combined into one map on a caller-supplied region and
    grid: the mean of the same masked, gridded product a single-granule call
    produces, with the per-cell observation count as a second band.
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

    >>> from pathlib import Path
    >>> from pysent.s2 import process_sentinel_s2_safe, S2_DEFAULT_PRODUCTS
    >>> product = "true_color_vegetation"  # ("B4", "B3", "B2")
    >>> results = process_sentinel_s2_safe(
    ...     input_dataset="/archive/S2A_MSIL1C_....zip",
    ...     output_dir=Path("/out"),
    ...     product_bands={product: S2_DEFAULT_PRODUCTS[product]},
    ...     output_names={product: "scene_true_color.tif"},
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
    "errors",
    "profiles",
    "qa",
    "s1",
    "s2",
    "s3",
    "s5p",
    # sentinel-1
    "S1_SUPPORTED_AMPLITUDE_VARIABLES",
    "build_sentinel_s1_output_filename",
    "despeckle_sentinel_s1",
    "detect_sentinel_s1_polarizations",
    "estimate_equivalent_looks",
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
    # sentinel-3
    "S3_PRODUCTS",
    "build_sentinel_s3_output_filename",
    "normalize_sentinel_s3_product_map",
    "process_sentinel_s3_safe",
    "restyle_sentinel_s3_product",
    # sentinel-5p
    "S5P_PRODUCTS",
    "build_sentinel_s5p_output_filename",
    "normalize_sentinel_s5p_product_map",
    "process_sentinel_s5p_netcdf",
    # composites
    "COMPOSITE_METHODS",
    "build_composite_output_filename",
    "composite_input_digest",
    "normalize_composite_region",
    "process_sentinel_composite",
    # errors
    "EmptyInputError",
    "EmptySceneError",
    "PartialFailure",
    "UnsupportedProductError",
    # platform / routing / archive
    "detect_nbs_sentinel_platform",
    "detect_nbs_sentinel_product",
    "resolve_safe_archive_from_uuid",
    "resolve_sentinel_pipeline",
    "supported_sentinel_product_types",
]

_SUBMODULES = {"archive", "composite", "csw", "errors", "profiles", "qa", "s1", "s2", "s3", "s5p"}

_EXPORTS = {
    "S1_SUPPORTED_AMPLITUDE_VARIABLES": "s1",
    "build_sentinel_s1_output_filename": "s1",
    "despeckle_sentinel_s1": "s1",
    "detect_sentinel_s1_polarizations": "s1",
    "estimate_equivalent_looks": "s1",
    "process_sentinel_s1_netcdf": "s1",
    "process_sentinel_s1_safe": "s1",
    "stretch_sentinel_s1_grayscale": "s1",
    "S2_DEFAULT_PRODUCTS": "s2",
    "S2_SUPPORTED_BANDS": "s2",
    "build_sentinel_s2_output_filename": "s2",
    "normalize_sentinel_s2_product_map": "s2",
    "process_sentinel_s2_safe": "s2",
    "stretch_sentinel_s2_rgb": "s2",
    "S3_PRODUCTS": "s3",
    "build_sentinel_s3_output_filename": "s3",
    "normalize_sentinel_s3_product_map": "s3",
    "process_sentinel_s3_safe": "s3",
    "restyle_sentinel_s3_product": "s3",
    "S5P_PRODUCTS": "s5p",
    "build_sentinel_s5p_output_filename": "s5p",
    "normalize_sentinel_s5p_product_map": "s5p",
    "process_sentinel_s5p_netcdf": "s5p",
    "COMPOSITE_METHODS": "composite",
    "build_composite_output_filename": "composite",
    "composite_input_digest": "composite",
    "normalize_composite_region": "composite",
    "process_sentinel_composite": "composite",
    "EmptyInputError": "errors",
    "EmptySceneError": "errors",
    "PartialFailure": "errors",
    "UnsupportedProductError": "errors",
    "detect_nbs_sentinel_platform": "profiles",
    "detect_nbs_sentinel_product": "profiles",
    "resolve_safe_archive_from_uuid": "archive",
    "resolve_sentinel_pipeline": "profiles",
    "supported_sentinel_product_types": "profiles",
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
