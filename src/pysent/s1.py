"""Dedicated Sentinel-1 SAFE and NetCDF to GeoTIFF quicklook conversion helpers."""
from __future__ import annotations

import os
import math
import re
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from multiprocessing import current_process
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from osgeo import gdal
from rasterio.enums import ColorInterp, Resampling

try:
    from numba import njit, prange  # type: ignore[reportMissingImports]

    NUMBA_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency in local venvs
    NUMBA_AVAILABLE = False

    def njit(*args, **kwargs):
        def _decorator(func):
            return func

        return _decorator

    def prange(*args):
        return range(*args)


S1_SUPPORTED_AMPLITUDE_VARIABLES: tuple[str, ...] = (
    "Amplitude_VH",
    "Amplitude_VV",
    "Amplitude_HH",
    "Amplitude_HV",
)
S1_TARGET_EPSG = "EPSG:32661"
S1_TARGET_RESOLUTION = 40.0
S1_OVERVIEW_FACTORS: tuple[int, ...] = (2, 4, 8, 16)
S1_STRETCH_PERCENTILES: tuple[float, float] = (2.0, 98.0)
S1_NETCDF_IMPLEMENTATION = "sentinel_s1_quicklook"
S1_SAFE_IMPLEMENTATION = "sentinel_s1_safe_quicklook"


@njit(cache=True, parallel=True)
def _stretch_sentinel_s1_numba(
    data: np.ndarray,
    p_low: float,
    p_high: float,
    nodata: float,
    has_nodata: bool,
) -> tuple[np.ndarray, np.ndarray]:
    rows, cols = data.shape
    gray = np.zeros((rows, cols), dtype=np.uint8)
    alpha = np.zeros((rows, cols), dtype=np.uint8)
    scale = 255.0 / max(p_high - p_low, 1e-6)

    for row in prange(rows):
        for col in range(cols):
            value = float(data[row, col])
            if not math.isfinite(value):
                continue
            if has_nodata and value == nodata:
                continue
            if value <= 0.0:
                continue

            scaled = (value - p_low) * scale
            if scaled < 0.0:
                scaled = 0.0
            elif scaled > 255.0:
                scaled = 255.0

            gray[row, col] = np.uint8(round(scaled))
            alpha[row, col] = np.uint8(255)

    return gray, alpha


def _sanitize_name_fragment(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip())
    text = text.strip("._-")
    return text or "sentinel_s1"


def _normalize_variable_suffix(variable: str) -> str:
    return _sanitize_name_fragment(variable).lower()


def build_sentinel_s1_output_filename(
    *,
    variable: str,
    output_file: str | None = None,
    identifier: str | None = None,
) -> str:
    """Return a stable GeoTIFF name for one S1 amplitude product."""

    variable_suffix = _normalize_variable_suffix(variable)
    raw_name = Path((output_file or "").strip()).name
    if not raw_name:
        raw_name = f"{_sanitize_name_fragment(identifier or 'sentinel_s1')}.tif"

    stem = Path(raw_name).stem or "sentinel_s1"
    suffix = Path(raw_name).suffix.lower()
    if suffix not in {".tif", ".tiff"}:
        suffix = ".tif"
    if not stem.lower().endswith(f"_{variable_suffix}"):
        stem = f"{stem}_{variable_suffix}"
    return f"{stem}{suffix}"


def _build_subdataset_path(input_dataset: str, variable: str) -> str:
    dataset_ref = (input_dataset or "").strip()
    if not dataset_ref:
        raise ValueError("input_dataset is required")
    if dataset_ref.startswith("NETCDF:") and dataset_ref.endswith(f":{variable}"):
        return dataset_ref
    return f"NETCDF:{dataset_ref}:{variable}"


def _resolve_safe_manifest_path(input_dataset: str) -> str:
    dataset_ref = (input_dataset or "").strip()
    if not dataset_ref:
        raise ValueError("input_dataset is required")

    if dataset_ref.endswith("manifest.safe"):
        return dataset_ref
    if dataset_ref.startswith("/vsizip/"):
        normalized = dataset_ref.rstrip("/")
        if normalized.endswith(".SAFE"):
            return f"{normalized}/manifest.safe"
        if ".zip/" in normalized:
            return normalized
        if normalized.endswith(".zip"):
            safe_dir = Path(normalized).stem
            return f"{normalized}/{safe_dir}.SAFE/manifest.safe"
    if dataset_ref.endswith(".SAFE"):
        return f"{dataset_ref.rstrip('/')}/manifest.safe"
    if dataset_ref.endswith(".zip"):
        safe_dir = Path(dataset_ref).stem
        return f"/vsizip/{dataset_ref}/{safe_dir}.SAFE/manifest.safe"
    raise ValueError(f"Unsupported Sentinel-1 SAFE dataset reference: {dataset_ref}")


def _resolve_safe_band_index(manifest_path: str, variable: str) -> int:
    expected_polarization = variable.removeprefix("Amplitude_").upper()
    dataset = gdal.Open(manifest_path, gdal.GA_ReadOnly)
    if dataset is None:
        raise RuntimeError(f"Unable to open Sentinel-1 SAFE dataset: {manifest_path}")
    for band_index in range(1, dataset.RasterCount + 1):
        band = dataset.GetRasterBand(band_index)
        if band is None:
            continue
        polarization = (band.GetMetadataItem("POLARIZATION") or "").strip().upper()
        if polarization == expected_polarization:
            return band_index
    raise RuntimeError(f"Sentinel-1 SAFE dataset does not expose polarization {expected_polarization}")


def detect_sentinel_s1_polarizations(input_dataset: str) -> list[str]:
    """Return the amplitude variables present in an S1 SAFE product.

    Reads the per-band ``POLARIZATION`` metadata from the SAFE manifest - the
    same field :func:`_resolve_safe_band_index` matches on - so the result lists
    exactly the polarisations that can be warped, e.g. ``["Amplitude_VV",
    "Amplitude_VH"]`` (IW SDV) or ``["Amplitude_HH", "Amplitude_HV"]`` (EW SDH).
    Returns an empty list if the manifest cannot be opened or exposes no
    polarisation metadata (caller decides the fallback).
    """
    try:
        manifest_path = _resolve_safe_manifest_path(input_dataset)
    except ValueError:
        return []
    try:
        dataset = gdal.Open(manifest_path, gdal.GA_ReadOnly)
    except RuntimeError:
        # GDAL may raise (rather than return None) on an unreadable path when
        # exceptions are enabled; treat as "undetectable" and let the caller fall back.
        return []
    if dataset is None:
        return []
    variables: list[str] = []
    for band_index in range(1, dataset.RasterCount + 1):
        band = dataset.GetRasterBand(band_index)
        if band is None:
            continue
        polarization = (band.GetMetadataItem("POLARIZATION") or "").strip().upper()
        if not polarization:
            continue
        variable = f"Amplitude_{polarization}"
        if variable not in variables:
            variables.append(variable)
    return variables


def _build_sentinel_s1_safe_vrt(input_dataset: str, variable: str, vrt_path: Path) -> str:
    manifest_path = _resolve_safe_manifest_path(input_dataset)
    band_index = _resolve_safe_band_index(manifest_path, variable)
    translated = gdal.Translate(
        str(vrt_path),
        manifest_path,
        options=gdal.TranslateOptions(format="VRT", bandList=[band_index]),
    )
    if translated is None:
        raise RuntimeError(f"Unable to build Sentinel-1 SAFE VRT for {variable}")
    translated = None
    return manifest_path


def _coerce_percentiles(percentiles: object) -> tuple[float, float]:
    if isinstance(percentiles, (list, tuple)) and len(percentiles) == 2:
        low_pct = float(percentiles[0])
        high_pct = float(percentiles[1])
        return low_pct, high_pct
    return S1_STRETCH_PERCENTILES


def _coerce_overview_factors(factors: object) -> tuple[int, ...]:
    if not isinstance(factors, (list, tuple)):
        return S1_OVERVIEW_FACTORS
    out = [int(value) for value in factors if int(value) > 1]
    return tuple(out) or S1_OVERVIEW_FACTORS


def _coerce_positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_product_workers(requested_workers: object, output_count: int) -> int:
    if output_count <= 1:
        return 1
    if requested_workers in (None, "", 0, "0"):
        return min(output_count, max(1, os.cpu_count() or 1))
    return min(output_count, _coerce_positive_int(requested_workers, 1))


def _resolve_gdal_num_threads(requested_threads: object, product_workers: int) -> str:
    if requested_threads not in (None, ""):
        text = str(requested_threads).strip().upper()
        if text == "ALL_CPUS":
            return text
        try:
            parsed = int(text)
        except Exception:
            return "1"
        return str(max(1, parsed))

    total_cpus = max(1, os.cpu_count() or 1)
    per_product = max(1, total_cpus // max(1, product_workers))
    return str(per_product)


def _resolve_warp_memory_limit_mb(requested_limit: object) -> float | None:
    if requested_limit in (None, ""):
        requested_limit = os.environ.get("S1_WARP_MEMORY_LIMIT_MB")
    if requested_limit in (None, ""):
        return None
    try:
        parsed = float(requested_limit)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _configure_gdal_runtime() -> None:
    cachemax_mb = os.environ.get("GDAL_CACHEMAX")
    if cachemax_mb:
        gdal.SetConfigOption("GDAL_CACHEMAX", str(cachemax_mb))


def stretch_sentinel_s1_grayscale(
    data: np.ndarray,
    *,
    nodata: float | None = 0.0,
    percentiles: tuple[float, float] = S1_STRETCH_PERCENTILES,
    use_numba: bool = False,
) -> tuple[np.ndarray, np.ndarray, dict[str, float]]:
    """Histogram-stretch one warped S1 amplitude raster to gray+alpha bytes."""

    low_pct, high_pct = percentiles
    valid = np.isfinite(data)
    if nodata is not None and math.isfinite(float(nodata)):
        valid &= data != float(nodata)
    valid &= data > 0

    gray = np.zeros(data.shape, dtype=np.uint8)
    alpha = np.zeros(data.shape, dtype=np.uint8)
    if not np.any(valid):
        return gray, alpha, {"min": 0.0, "max": 0.0, "p_low": 0.0, "p_high": 0.0}

    valid_values = data[valid].astype(np.float32, copy=False)
    p_low, p_high = np.percentile(valid_values, [low_pct, high_pct])
    if not np.isfinite(p_low):
        p_low = float(np.min(valid_values))
    if not np.isfinite(p_high):
        p_high = float(np.max(valid_values))
    if p_high <= p_low:
        p_low = float(np.min(valid_values))
        p_high = float(np.max(valid_values))
    if p_high <= p_low:
        p_high = p_low + 1.0

    if use_numba and NUMBA_AVAILABLE:
        has_nodata = nodata is not None and math.isfinite(float(nodata))
        gray, alpha = _stretch_sentinel_s1_numba(
            data.astype(np.float32, copy=False),
            float(p_low),
            float(p_high),
            float(nodata or 0.0),
            has_nodata,
        )
    else:
        scaled = np.clip((data.astype(np.float32, copy=False) - float(p_low)) / float(p_high - p_low), 0.0, 1.0)
        gray[valid] = np.round(scaled[valid] * 255.0).astype(np.uint8)
        alpha[valid] = 255

    return gray, alpha, {
        "min": float(np.min(valid_values)),
        "max": float(np.max(valid_values)),
        "p_low": float(p_low),
        "p_high": float(p_high),
    }


def _warp_sentinel_s1_amplitude(
    input_dataset: str,
    variable: str,
    warped_path: Path,
    *,
    target_epsg: str = S1_TARGET_EPSG,
    target_resolution: float = S1_TARGET_RESOLUTION,
    resample_alg: str = "average",
    block_size: int = 256,
    gdal_num_threads: str = "1",
    warp_memory_limit_mb: float | None = None,
) -> None:
    subdataset = _build_subdataset_path(input_dataset, variable)
    warped = gdal.Warp(
        str(warped_path),
        subdataset,
        options=gdal.WarpOptions(
            format="GTiff",
            dstSRS=target_epsg,
            xRes=target_resolution,
            yRes=target_resolution,
            srcNodata=0,
            dstNodata=0,
            geoloc=True,
            multithread=True,
            resampleAlg=resample_alg,
            outputType=gdal.GDT_Float32,
            warpOptions=[f"NUM_THREADS={gdal_num_threads}"],
            warpMemoryLimit=warp_memory_limit_mb,
            creationOptions=[
                "COMPRESS=LZW",
                "TILED=YES",
                f"BLOCKXSIZE={block_size}",
                f"BLOCKYSIZE={block_size}",
                "BIGTIFF=IF_SAFER",
            ],
        ),
    )
    if warped is None:
        raise RuntimeError(f"GDAL warp failed for {variable}")
    warped = None


def _warp_sentinel_s1_safe_amplitude(
    input_dataset: str,
    variable: str,
    warped_path: Path,
    *,
    target_epsg: str = S1_TARGET_EPSG,
    target_resolution: float = S1_TARGET_RESOLUTION,
    resample_alg: str = "average",
    block_size: int = 256,
    gdal_num_threads: str = "1",
    warp_memory_limit_mb: float | None = None,
    use_tps: bool = True,
) -> None:
    vrt_path = warped_path.with_name(f"{warped_path.stem}.{_normalize_variable_suffix(variable)}.vrt")
    _build_sentinel_s1_safe_vrt(input_dataset, variable, vrt_path)
    try:
        warped = gdal.Warp(
            str(warped_path),
            str(vrt_path),
            options=gdal.WarpOptions(
                format="GTiff",
                dstSRS=target_epsg,
                xRes=target_resolution,
                yRes=target_resolution,
                srcNodata=0,
                dstNodata=0,
                # The SAFE band VRT is GCP-based: thin-plate-spline (tps) is
                # accurate but the dominant warp cost; with use_tps=False GDAL
                # falls back to the much faster polynomial GCP transform.
                tps=use_tps,
                multithread=True,
                resampleAlg=resample_alg,
                outputType=gdal.GDT_Float32,
                warpOptions=[f"NUM_THREADS={gdal_num_threads}"],
                warpMemoryLimit=warp_memory_limit_mb,
                creationOptions=[
                    "COMPRESS=LZW",
                    "TILED=YES",
                    f"BLOCKXSIZE={block_size}",
                    f"BLOCKYSIZE={block_size}",
                    "BIGTIFF=IF_SAFER",
                ],
            ),
        )
        if warped is None:
            raise RuntimeError(f"GDAL SAFE warp failed for {variable}")
        warped = None
    finally:
        vrt_path.unlink(missing_ok=True)


def _write_quicklook_from_warped(
    warped_path: Path,
    output_path: Path,
    *,
    use_numba: bool = False,
    percentiles: tuple[float, float] = S1_STRETCH_PERCENTILES,
    compression: str = "jpeg",
    block_size: int = 256,
    overview_factors: tuple[int, ...] = S1_OVERVIEW_FACTORS,
    target_epsg: str = S1_TARGET_EPSG,
    target_resolution: float = S1_TARGET_RESOLUTION,
) -> dict[str, Any]:
    with rasterio.open(warped_path) as src:
        data = src.read(1).astype(np.float32, copy=False)
        gray, alpha, stretch = stretch_sentinel_s1_grayscale(
            data,
            nodata=src.nodata,
            percentiles=percentiles,
            use_numba=use_numba,
        )
        profile = src.profile.copy()
        profile.update(
            driver="GTiff",
            dtype="uint8",
            count=2,
            nodata=None,
            compress=str(compression).lower(),
            tiled=True,
            blockxsize=block_size,
            blockysize=block_size,
            interleave="pixel",
            photometric="minisblack",
        )
        with rasterio.open(output_path, "w", **profile) as dst:
            dst.write(gray, 1)
            dst.write(alpha, 2)
            dst.colorinterp = (ColorInterp.gray, ColorInterp.alpha)
            factors = [factor for factor in overview_factors if src.width // factor >= 1 and src.height // factor >= 1]
            if factors:
                dst.build_overviews(factors, Resampling.average)
                dst.update_tags(ns="rio_overview", resampling="average")
        return {
            "stretch": stretch,
            "width": int(src.width),
            "height": int(src.height),
            "target_epsg": target_epsg,
            "target_resolution": target_resolution,
            "used_numba": bool(use_numba and NUMBA_AVAILABLE),
        }


def _process_sentinel_s1_product(
    *,
    input_dataset: str,
    variable: str,
    output_path: str,
    target_epsg: str,
    target_resolution: float,
    resample_alg: str,
    block_size: int,
    gdal_num_threads: str,
    warp_memory_limit_mb: float | None,
    use_numba: bool,
    percentiles: tuple[float, float],
    compression: str,
    overview_factors: tuple[int, ...],
) -> dict[str, Any]:
    final_path = Path(output_path)
    warped_path = final_path.with_name(f"{final_path.stem}.warp.tif")
    _warp_sentinel_s1_amplitude(
        input_dataset,
        variable,
        warped_path,
        target_epsg=target_epsg,
        target_resolution=target_resolution,
        resample_alg=resample_alg,
        block_size=block_size,
        gdal_num_threads=gdal_num_threads,
        warp_memory_limit_mb=warp_memory_limit_mb,
    )
    stats = _write_quicklook_from_warped(
        warped_path,
        final_path,
        use_numba=use_numba,
        percentiles=percentiles,
        compression=compression,
        block_size=block_size,
        overview_factors=overview_factors,
        target_epsg=target_epsg,
        target_resolution=target_resolution,
    )
    warped_path.unlink(missing_ok=True)
    return {
        "variable": variable,
        "path": str(final_path),
        "output_file": final_path.name,
        **stats,
    }


def _process_sentinel_s1_safe_product(
    *,
    input_dataset: str,
    variable: str,
    output_path: str,
    target_epsg: str,
    target_resolution: float,
    resample_alg: str,
    block_size: int,
    gdal_num_threads: str,
    warp_memory_limit_mb: float | None,
    use_numba: bool,
    percentiles: tuple[float, float],
    compression: str,
    overview_factors: tuple[int, ...],
) -> dict[str, Any]:
    final_path = Path(output_path)
    warped_path = final_path.with_name(f"{final_path.stem}.warp.tif")
    _warp_sentinel_s1_safe_amplitude(
        input_dataset,
        variable,
        warped_path,
        target_epsg=target_epsg,
        target_resolution=target_resolution,
        resample_alg=resample_alg,
        block_size=block_size,
        gdal_num_threads=gdal_num_threads,
        warp_memory_limit_mb=warp_memory_limit_mb,
    )
    stats = _write_quicklook_from_warped(
        warped_path,
        final_path,
        use_numba=use_numba,
        percentiles=percentiles,
        compression=compression,
        block_size=block_size,
        overview_factors=overview_factors,
        target_epsg=target_epsg,
        target_resolution=target_resolution,
    )
    warped_path.unlink(missing_ok=True)
    return {
        "variable": variable,
        "path": str(final_path),
        "output_file": final_path.name,
        **stats,
    }


def _resolve_sentinel_s1_processing_settings(
    processing_options: dict[str, Any] | None,
    *,
    output_count: int,
) -> dict[str, Any]:
    processing = dict(processing_options or {})
    histogram = processing.get("histogram_stretch") if isinstance(processing.get("histogram_stretch"), dict) else {}
    percentiles = _coerce_percentiles(histogram.get("percentiles"))
    block_size = _coerce_positive_int(processing.get("block_size"), 256)
    overview_factors = _coerce_overview_factors(processing.get("overview_factors"))
    target_epsg = str(processing.get("target_epsg") or S1_TARGET_EPSG)
    target_resolution = float(processing.get("target_resolution") or S1_TARGET_RESOLUTION)
    resample_alg = str(processing.get("resample_alg") or "average")
    compression = str(processing.get("compression") or "JPEG")
    use_numba = _env_bool("S1_USE_NUMBA", default=False) if processing.get("use_numba") is None else bool(processing.get("use_numba"))
    parallel_mode = str(processing.get("parallel_mode") or os.environ.get("S1_PARALLEL_MODE") or "threads").strip().lower()
    if parallel_mode == "processes" and current_process().daemon:
        parallel_mode = "threads"
    requested_parallel_workers = processing.get("parallel_workers")
    if requested_parallel_workers in (None, "", 0, "0"):
        requested_parallel_workers = os.environ.get("S1_PRODUCT_WORKERS")
    product_workers = _resolve_product_workers(requested_parallel_workers, output_count)
    requested_gdal_threads = processing.get("gdal_num_threads")
    if requested_gdal_threads in (None, ""):
        requested_gdal_threads = os.environ.get("S1_GDAL_NUM_THREADS")
    gdal_num_threads = _resolve_gdal_num_threads(requested_gdal_threads, product_workers)
    warp_memory_limit_mb = _resolve_warp_memory_limit_mb(processing.get("warp_memory_limit_mb"))
    return {
        "percentiles": percentiles,
        "block_size": block_size,
        "overview_factors": overview_factors,
        "target_epsg": target_epsg,
        "target_resolution": target_resolution,
        "resample_alg": resample_alg,
        "compression": compression,
        "use_numba": use_numba,
        "parallel_mode": parallel_mode,
        "product_workers": product_workers,
        "gdal_num_threads": gdal_num_threads,
        "warp_memory_limit_mb": warp_memory_limit_mb,
    }


def process_sentinel_s1_netcdf(
    *,
    input_dataset: str,
    output_dir: Path,
    output_names: dict[str, str],
    processing_options: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Generate stretched S1 quicklook GeoTIFFs for the requested amplitude variables."""

    output_dir.mkdir(parents=True, exist_ok=True)
    _configure_gdal_runtime()
    settings = _resolve_sentinel_s1_processing_settings(processing_options, output_count=len(output_names))

    job_specs = [
        {
            "input_dataset": input_dataset,
            "variable": variable,
            "output_path": str(output_dir / output_name),
            "target_epsg": settings["target_epsg"],
            "target_resolution": settings["target_resolution"],
            "resample_alg": settings["resample_alg"],
            "block_size": settings["block_size"],
            "gdal_num_threads": settings["gdal_num_threads"],
            "warp_memory_limit_mb": settings["warp_memory_limit_mb"],
            "use_numba": settings["use_numba"],
            "percentiles": settings["percentiles"],
            "compression": settings["compression"],
            "overview_factors": settings["overview_factors"],
        }
        for variable, output_name in output_names.items()
    ]

    if settings["product_workers"] <= 1 or settings["parallel_mode"] in {"none", "off", "serial"}:
        generated = [_process_sentinel_s1_product(**spec) for spec in job_specs]
    else:
        executor_cls = ThreadPoolExecutor if settings["parallel_mode"] == "threads" else ProcessPoolExecutor
        with executor_cls(max_workers=settings["product_workers"]) as executor:
            futures = [executor.submit(_process_sentinel_s1_product, **spec) for spec in job_specs]
            generated = [future.result() for future in futures]

    return generated


def process_sentinel_s1_safe(
    *,
    input_dataset: str,
    output_dir: Path,
    output_names: dict[str, str],
    processing_options: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Generate stretched S1 quicklook GeoTIFFs from a SAFE archive or manifest path."""

    output_dir.mkdir(parents=True, exist_ok=True)
    _configure_gdal_runtime()

    # Only warp polarisations actually present in this product: an EW SDH product
    # has HH/HV (no VV/VH) and vice-versa. Dropping absent ones avoids a hard GDAL
    # failure when the requested set doesn't match the product's polarisations.
    available = detect_sentinel_s1_polarizations(input_dataset)
    if available:
        present = {variable: name for variable, name in output_names.items() if variable in available}
        if not present:
            raise RuntimeError(
                f"None of the requested Sentinel-1 variables {sorted(output_names)} are present in this "
                f"product; available polarisations: {available}"
            )
        output_names = present

    settings = _resolve_sentinel_s1_processing_settings(processing_options, output_count=len(output_names))

    job_specs = [
        {
            "input_dataset": input_dataset,
            "variable": variable,
            "output_path": str(output_dir / output_name),
            "target_epsg": settings["target_epsg"],
            "target_resolution": settings["target_resolution"],
            "resample_alg": settings["resample_alg"],
            "block_size": settings["block_size"],
            "gdal_num_threads": settings["gdal_num_threads"],
            "warp_memory_limit_mb": settings["warp_memory_limit_mb"],
            "use_numba": settings["use_numba"],
            "percentiles": settings["percentiles"],
            "compression": settings["compression"],
            "overview_factors": settings["overview_factors"],
        }
        for variable, output_name in output_names.items()
    ]

    if settings["product_workers"] <= 1 or settings["parallel_mode"] in {"none", "off", "serial"}:
        generated = [_process_sentinel_s1_safe_product(**spec) for spec in job_specs]
    else:
        executor_cls = ThreadPoolExecutor if settings["parallel_mode"] == "threads" else ProcessPoolExecutor
        with executor_cls(max_workers=settings["product_workers"]) as executor:
            futures = [executor.submit(_process_sentinel_s1_safe_product, **spec) for spec in job_specs]
            generated = [future.result() for future in futures]

    return generated