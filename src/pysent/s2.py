"""Dedicated Sentinel-2 SAFE to GeoTIFF RGB product conversion helpers."""
from __future__ import annotations

import math
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np
import rasterio
from osgeo import gdal
from rasterio.enums import ColorInterp, Resampling

from ._runtime import (
    SERIAL_MODES,
    atomic_output,
    available_cpus,
    gdal_errors,
    resolve_gdal_runtime,
    resolve_work_dir,
    run_product_jobs,
    scratch_dir,
)
from ._safe import find_safe_member, resolve_safe_root
from .errors import EmptySceneError


S2_SAFE_IMPLEMENTATION = "sentinel_s2_safe_quicklook"
S2_OVERVIEW_FACTORS: tuple[int, ...] = (2, 4, 8, 16)
S2_STRETCH_PERCENTILES: tuple[float, float] = (2.0, 98.0)
S2_DEFAULT_PRODUCTS: dict[str, tuple[str, str, str]] = {
    "true_color_vegetation": ("B4", "B3", "B2"),
    "false_color_glacier": ("B12", "B8A", "B3"),
    "false_color_vegetation": ("B8A", "B4", "B3"),
}
S2_SUPPORTED_BANDS: tuple[str, ...] = (
    "B1",
    "B2",
    "B3",
    "B4",
    "B5",
    "B6",
    "B7",
    "B8",
    "B8A",
    "B9",
    "B10",
    "B11",
    "B12",
)
# Product metadata GDAL's SENTINEL2 driver opens: MTD_MSIL1C.xml / MTD_MSIL2A.xml,
# or S2A_OPER_MTD_SAFL1C_*.xml in pre-2016 (PSD < 14) products.
_S2_METADATA_XML = re.compile(r"MTD_MSIL\w+\.xml|S2\w_\w+_MTD_SAFL\w+\.xml")


def _sanitize_name_fragment(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip())
    text = text.strip("._-")
    return text or "sentinel_s2"


def build_sentinel_s2_output_filename(
    *,
    product_name: str,
    output_file: str | None = None,
    identifier: str | None = None,
) -> str:
    product_suffix = _sanitize_name_fragment(product_name).lower()
    raw_name = Path((output_file or "").strip()).name
    if not raw_name:
        raw_name = f"{_sanitize_name_fragment(identifier or 'sentinel_s2')}.tif"

    stem = Path(raw_name).stem or "sentinel_s2"
    suffix = Path(raw_name).suffix.lower()
    if suffix not in {".tif", ".tiff"}:
        suffix = ".tif"
    if not stem.lower().endswith(f"_{product_suffix}"):
        stem = f"{stem}_{product_suffix}"
    return f"{stem}{suffix}"


def normalize_sentinel_s2_product_map(raw: object | None) -> dict[str, tuple[str, str, str]]:
    if raw is None:
        return dict(S2_DEFAULT_PRODUCTS)
    if not isinstance(raw, dict) or not raw:
        raise ValueError("products_json must decode to a non-empty object")

    normalized: dict[str, tuple[str, str, str]] = {}
    for key, value in raw.items():
        product_name = _sanitize_name_fragment(str(key or ""))
        if not product_name:
            raise ValueError("Product names must be non-empty strings")
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ValueError(f"Product '{product_name}' must map to exactly three band names")
        bands = tuple(str(item or "").strip().upper() for item in value)
        if any(band not in S2_SUPPORTED_BANDS for band in bands):
            allowed = ", ".join(S2_SUPPORTED_BANDS)
            raise ValueError(f"Product '{product_name}' contains unsupported bands. Allowed: {allowed}")
        normalized[product_name] = bands
    return normalized


def _coerce_overview_factors(factors: object) -> tuple[int, ...]:
    if not isinstance(factors, (list, tuple)):
        return S2_OVERVIEW_FACTORS
    out = [int(value) for value in factors if int(value) > 1]
    return tuple(out) or S2_OVERVIEW_FACTORS


def _coerce_positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _coerce_percentiles(percentiles: object) -> tuple[float, float]:
    if isinstance(percentiles, (list, tuple)) and len(percentiles) == 2:
        return float(percentiles[0]), float(percentiles[1])
    return S2_STRETCH_PERCENTILES


def _resolve_product_workers(requested_workers: object, output_count: int) -> int:
    if output_count <= 1:
        return 1
    if requested_workers in (None, "", 0, "0"):
        return min(output_count, available_cpus())
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

    # Every product gets all usable CPUs rather than an equal share: a product
    # keeps its threads only partly busy (I/O, single-threaded stretch), so on
    # the benchmark scenes sharing beat splitting by 20-40 % in wall time with
    # identical output (PLANNING/TODO_PLANNING_bulk_processing.md, phase 1 log).
    # Bulk runners that start several calls at once pass an explicit value.
    return str(available_cpus())


def _resolve_warp_memory_limit_mb(requested_limit: object) -> float | None:
    if requested_limit in (None, ""):
        requested_limit = os.environ.get("S2_WARP_MEMORY_LIMIT_MB")
    if requested_limit in (None, ""):
        return None
    try:
        parsed = float(requested_limit)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _resolve_sentinel_s2_dataset_ref(input_dataset: str) -> str:
    dataset_ref = (input_dataset or "").strip()
    if not dataset_ref:
        raise ValueError("input_dataset is required")
    if dataset_ref.startswith("SENTINEL2_"):
        return dataset_ref
    normalized = dataset_ref.rstrip("/")
    if _S2_METADATA_XML.fullmatch(PurePosixPath(normalized).name):
        return dataset_ref
    # GDAL opens the metadata XML, not the zip or .SAFE directory around it.
    if normalized.endswith(".SAFE") or normalized.lower().endswith(".zip"):
        safe_root = resolve_safe_root(normalized)
        metadata_xml = find_safe_member(safe_root, _S2_METADATA_XML)
        if metadata_xml is None:
            raise RuntimeError(f"No Sentinel-2 metadata XML (MTD_MSIL*.xml) found in {safe_root}")
        return metadata_xml
    if dataset_ref.startswith("/vsizip/"):
        return dataset_ref
    raise ValueError(f"Unsupported Sentinel-2 SAFE dataset reference: {dataset_ref}")


def _extract_epsg(subdataset_name: str, dataset: gdal.Dataset) -> str:
    match = re.search(r":EPSG_(\d+)$", subdataset_name)
    if match:
        return f"EPSG:{match.group(1)}"
    projection = dataset.GetProjectionRef() or ""
    match = re.search(r'AUTHORITY\["EPSG","(\d+)"\]', projection)
    if match:
        return f"EPSG:{match.group(1)}"
    return ""


def _collect_sentinel_s2_band_sources(input_dataset: str) -> dict[str, dict[str, object]]:
    root_ref = _resolve_sentinel_s2_dataset_ref(input_dataset)
    dataset = gdal.Open(root_ref, gdal.GA_ReadOnly)
    if dataset is None:
        raise RuntimeError(f"Unable to open Sentinel-2 SAFE dataset: {input_dataset}")
    subdatasets = dataset.GetMetadata("SUBDATASETS") or {}
    out: dict[str, dict[str, object]] = {}
    for key, value in subdatasets.items():
        if not key.endswith("_NAME"):
            continue
        subdataset_name = str(value or "").strip()
        if not subdataset_name:
            continue
        subdataset = gdal.Open(subdataset_name, gdal.GA_ReadOnly)
        if subdataset is None:
            continue
        geotransform = subdataset.GetGeoTransform(can_return_null=True)
        resolution = abs(float(geotransform[1])) if geotransform else None
        epsg = _extract_epsg(subdataset_name, subdataset)
        for band_index in range(1, subdataset.RasterCount + 1):
            band = subdataset.GetRasterBand(band_index)
            if band is None:
                continue
            band_name = (band.GetMetadataItem("BANDNAME") or band.GetDescription() or "").strip().upper()
            if not band_name or band_name in out:
                continue
            out[band_name] = {
                "subdataset_name": subdataset_name,
                "band_index": band_index,
                "resolution": resolution,
                "epsg": epsg,
            }
    return out


def _resolve_selected_band_sources(
    input_dataset: str,
    band_names: tuple[str, str, str],
) -> tuple[list[dict[str, object]], str | None, float | None]:
    sources = _collect_sentinel_s2_band_sources(input_dataset)
    selected: list[dict[str, object]] = []
    resolutions: list[float] = []
    epsgs: list[str] = []
    for band_name in band_names:
        source = sources.get(band_name)
        if source is None:
            raise RuntimeError(f"Sentinel-2 SAFE dataset does not expose band {band_name}")
        selected.append(dict(source, band_name=band_name))
        resolution = source.get("resolution")
        if isinstance(resolution, (int, float)) and resolution > 0:
            resolutions.append(float(resolution))
        epsg = str(source.get("epsg") or "").strip()
        if epsg:
            epsgs.append(epsg)
    target_epsg = epsgs[0] if epsgs else None
    target_resolution = min(resolutions) if resolutions else None
    return selected, target_epsg, target_resolution


def _build_sentinel_s2_stack_vrt(
    input_dataset: str,
    band_names: tuple[str, str, str],
    vrt_path: Path,
) -> tuple[str | None, float | None, list[Path]]:
    selected_sources, target_epsg, target_resolution = _resolve_selected_band_sources(input_dataset, band_names)
    single_band_vrts: list[Path] = []
    try:
        for position, source in enumerate(selected_sources, start=1):
            single_vrt = vrt_path.with_name(f"{vrt_path.stem}.{position}.{str(source['band_name']).lower()}.vrt")
            with gdal_errors():
                translated = gdal.Translate(
                    str(single_vrt),
                    str(source["subdataset_name"]),
                    options=gdal.TranslateOptions(format="VRT", bandList=[int(source["band_index"])]),
                )
            if translated is None:
                raise RuntimeError(f"Unable to build Sentinel-2 VRT for {source['band_name']}")
            translated = None
            single_band_vrts.append(single_vrt)
        with gdal_errors():
            stacked = gdal.BuildVRT(
                str(vrt_path),
                [str(path) for path in single_band_vrts],
                options=gdal.BuildVRTOptions(separate=True, resolution="highest", srcNodata=0, VRTNodata=0),
            )
        if stacked is None:
            raise RuntimeError(f"Unable to build Sentinel-2 RGB VRT for {', '.join(band_names)}")
        stacked = None
        return target_epsg, target_resolution, single_band_vrts
    except Exception:
        for path in single_band_vrts:
            path.unlink(missing_ok=True)
        raise


def _warp_sentinel_s2_rgb(
    input_dataset: str,
    band_names: tuple[str, str, str],
    warped_path: Path,
    *,
    target_epsg: str | None,
    target_resolution: float | None,
    resample_alg: str,
    block_size: int,
    gdal_num_threads: str,
    warp_memory_limit_mb: float | None,
) -> tuple[str | None, float | None]:
    vrt_path = warped_path.with_name(f"{warped_path.stem}.stack.vrt")
    resolved_epsg, resolved_resolution, child_vrts = _build_sentinel_s2_stack_vrt(input_dataset, band_names, vrt_path)
    effective_epsg = (target_epsg or resolved_epsg or "").strip() or None
    effective_resolution = float(target_resolution or resolved_resolution or 10.0)
    try:
        try:
            with gdal_errors():
                warped = gdal.Warp(
                    str(warped_path),
                    str(vrt_path),
                    options=gdal.WarpOptions(
                        format="GTiff",
                        dstSRS=effective_epsg,
                        xRes=effective_resolution,
                        yRes=effective_resolution,
                        srcNodata=0,
                        dstNodata=0,
                        multithread=True,
                        resampleAlg=resample_alg,
                        outputType=gdal.GDT_UInt16,
                        warpOptions=[f"NUM_THREADS={gdal_num_threads}"],
                        warpMemoryLimit=warp_memory_limit_mb,
                        creationOptions=[
                            "COMPRESS=LZW",
                            "TILED=YES",
                            f"BLOCKXSIZE={block_size}",
                            f"BLOCKYSIZE={block_size}",
                            "BIGTIFF=IF_SAFER",
                            "INTERLEAVE=PIXEL",
                        ],
                    ),
                )
        except RuntimeError as exc:
            raise RuntimeError(f"GDAL Sentinel-2 warp failed for {', '.join(band_names)}: {exc}") from exc
        if warped is None:
            raise RuntimeError(f"GDAL Sentinel-2 warp failed for {', '.join(band_names)}")
        warped = None
        return effective_epsg, effective_resolution
    finally:
        vrt_path.unlink(missing_ok=True)
        for path in child_vrts:
            path.unlink(missing_ok=True)


def stretch_sentinel_s2_rgb(
    data: np.ndarray,
    *,
    nodata: float | None = 0.0,
    percentiles: tuple[float, float] = S2_STRETCH_PERCENTILES,
) -> tuple[np.ndarray, dict[str, list[float]]]:
    low_pct, high_pct = percentiles
    valid = np.all(np.isfinite(data), axis=0)
    if nodata is not None:
        valid &= np.any(data != float(nodata), axis=0)
    else:
        valid &= np.any(data != 0, axis=0)

    stretched = np.zeros(data.shape, dtype=np.uint8)
    stats = {"p_low": [], "p_high": []}
    if not np.any(valid):
        return stretched, stats

    for band_index in range(data.shape[0]):
        valid_values = data[band_index][valid].astype(np.float32, copy=False)
        p_low, p_high = np.percentile(valid_values, [low_pct, high_pct])
        if not np.isfinite(p_low):
            p_low = float(np.min(valid_values))
        if not np.isfinite(p_high):
            p_high = float(np.max(valid_values))
        if p_high <= p_low:
            p_high = p_low + 1.0
        scaled = np.clip((data[band_index].astype(np.float32, copy=False) - float(p_low)) / float(p_high - p_low), 0.0, 1.0)
        stretched[band_index][valid] = np.round(scaled[valid] * 255.0).astype(np.uint8)
        stats["p_low"].append(float(p_low))
        stats["p_high"].append(float(p_high))
    return stretched, stats


def _translate_sentinel_s2_rgb(
    warped_path: Path,
    output_path: Path,
    *,
    compression: str,
    block_size: int,
    overview_factors: tuple[int, ...],
) -> None:
    with gdal_errors():
        translated = gdal.Translate(
            str(output_path),
            str(warped_path),
            options=gdal.TranslateOptions(
                format="GTiff",
                creationOptions=[
                    f"COMPRESS={compression}",
                    "TILED=YES",
                    f"BLOCKXSIZE={block_size}",
                    f"BLOCKYSIZE={block_size}",
                    "BIGTIFF=IF_SAFER",
                    "INTERLEAVE=PIXEL",
                ],
            ),
        )
        if translated is None:
            raise RuntimeError(f"Unable to translate Sentinel-2 RGB product to {output_path.name}")
        translated = None
        ds = gdal.Open(str(output_path), gdal.GA_Update)
        if ds is not None and overview_factors:
            ds.BuildOverviews("AVERAGE", list(overview_factors))
        ds = None


def _band_min_max(band: gdal.Band, description: str) -> tuple[float, float]:
    """Exact min/max of one band, ignoring NoData; :class:`EmptySceneError` if it has no valid pixel."""
    try:
        with gdal_errors():
            result = band.ComputeRasterMinMax(False)
    except RuntimeError as exc:
        # GDAL: "Failed to compute min/max, no valid pixels found in sampling."
        if "no valid pixels" in str(exc):
            raise EmptySceneError(f"{description} has no valid pixels") from exc
        raise
    if result is None or not all(math.isfinite(value) for value in result):
        raise EmptySceneError(f"{description} has no valid pixels")
    return float(result[0]), float(result[1])


def _write_stretched_sentinel_s2_rgb(
    warped_path: Path,
    output_path: Path,
    *,
    percentiles: tuple[float, float],
    block_size: int,
    overview_factors: tuple[int, ...],
    compression: str = "DEFLATE",
) -> dict[str, list[float]]:
    """Auto (per-band min/max) stretch the warped RGB to a tiled, compressed 8-bit
    GeoTIFF with internal overviews.

    The min/max is computed per band (ignoring nodata) and applied via
    ``gdal.Translate`` scale params - so, unlike a bare ``scaleParams=[[]]``, the
    output is tiled + compressed (web/COG friendly) and the per-band src range is
    returned as ``{"p_low", "p_high", "method"}`` for the job record.

    Raises :class:`pysent.errors.EmptySceneError` if a band has no valid pixel.
    """
    with gdal_errors():
        source = gdal.Open(str(warped_path), gdal.GA_ReadOnly)
    if source is None:
        raise RuntimeError(f"Unable to open warped Sentinel-2 raster: {warped_path}")
    scale_params: list[list[float]] = []
    p_low: list[float] = []
    p_high: list[float] = []
    for band_index in range(1, source.RasterCount + 1):
        band = source.GetRasterBand(band_index)
        minimum, maximum = _band_min_max(band, f"Sentinel-2 band {band_index}")
        if maximum <= minimum:
            maximum = minimum + 1.0
        scale_params.append([minimum, maximum, 0, 255])
        p_low.append(float(minimum))
        p_high.append(float(maximum))
    source = None

    with gdal_errors():
        translated = gdal.Translate(
            str(output_path),
            str(warped_path),
            options=gdal.TranslateOptions(
                format="GTiff",
                outputType=gdal.GDT_Byte,
                scaleParams=scale_params,
                creationOptions=[
                    f"COMPRESS={compression}",
                    "TILED=YES",
                    f"BLOCKXSIZE={block_size}",
                    f"BLOCKYSIZE={block_size}",
                    "BIGTIFF=IF_SAFER",
                    "INTERLEAVE=PIXEL",
                ],
            ),
        )
        if translated is None:
            raise RuntimeError(f"Unable to stretch Sentinel-2 RGB product to {output_path.name}")
        translated = None

        dataset = gdal.Open(str(output_path), gdal.GA_Update)
        if dataset is not None and overview_factors:
            dataset.BuildOverviews("AVERAGE", list(overview_factors))
        dataset = None
    return {"p_low": p_low, "p_high": p_high, "method": "minmax"}


def _write_stretched_sentinel_s2_rgb_percentile(
    warped_path: Path,
    output_path: Path,
    *,
    percentiles: tuple[float, float],
    block_size: int,
    overview_factors: tuple[int, ...],
) -> dict[str, list[float]]:
    """Per-band PERCENTILE stretch alternative to the active min/max writer.

    Outlier-robust (clouds/sunglint don't blow out the scene) - kept as the
    reference implementation pending the notebook validation of percentile vs
    min/max (QA bundle, "stretch quality report"). Wire this into
    ``_process_sentinel_s2_product`` to switch the active path to percentile.
    """
    with rasterio.open(warped_path) as src:
        data = src.read(indexes=[1, 2, 3]).astype(np.float32, copy=False)
        stretched, stats = stretch_sentinel_s2_rgb(data, nodata=src.nodata, percentiles=percentiles)
        profile = src.profile.copy()
        profile.update(
            driver="GTiff",
            dtype="uint8",
            count=3,
            nodata=None,
            compress="jpeg",
            tiled=True,
            blockxsize=block_size,
            blockysize=block_size,
            interleave="pixel",
            photometric="rgb",
        )
        with rasterio.open(output_path, "w", **profile) as dst:
            dst.write(stretched)
            dst.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
            factors = [factor for factor in overview_factors if src.width // factor >= 1 and src.height // factor >= 1]
            if factors:
                dst.build_overviews(factors, Resampling.average)
                dst.update_tags(ns="rio_overview", resampling="average")
        return stats


def _process_sentinel_s2_product(
    *,
    input_dataset: str,
    product_name: str,
    band_names: tuple[str, str, str],
    output_path: str,
    work_dir: str | None,
    target_epsg: str | None,
    target_resolution: float | None,
    resample_alg: str,
    block_size: int,
    gdal_num_threads: str,
    warp_memory_limit_mb: float | None,
    histogram_stretch: bool,
    percentiles: tuple[float, float],
    compression: str,
    overview_factors: tuple[int, ...],
) -> dict[str, Any]:
    final_path = Path(output_path)
    scratch_root = Path(work_dir) if work_dir else final_path.parent
    stretch_stats: dict[str, list[float]] | None = None
    # Intermediates live in a private scratch directory and the final file is
    # moved into place only once complete, so a failure or a kill leaves neither
    # stray intermediates nor a truncated output under the final name.
    with scratch_dir(scratch_root) as scratch, atomic_output(final_path) as partial_path:
        warped_path = scratch / f"{final_path.stem}.warp.tif"
        effective_epsg, effective_resolution = _warp_sentinel_s2_rgb(
            input_dataset,
            band_names,
            warped_path,
            target_epsg=target_epsg,
            target_resolution=target_resolution,
            resample_alg=resample_alg,
            block_size=block_size,
            gdal_num_threads=gdal_num_threads,
            warp_memory_limit_mb=warp_memory_limit_mb,
        )
        if histogram_stretch:
            stretch_stats = _write_stretched_sentinel_s2_rgb(
                warped_path,
                partial_path,
                percentiles=percentiles,
                block_size=block_size,
                overview_factors=overview_factors,
                compression=compression,
            )
        else:
            _translate_sentinel_s2_rgb(
                warped_path,
                partial_path,
                compression=compression,
                block_size=block_size,
                overview_factors=overview_factors,
            )
        warped_path.unlink(missing_ok=True)

        dataset = gdal.Open(str(partial_path), gdal.GA_Update)
        if dataset is not None:
            for band_index, color in enumerate((gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand), start=1):
                band = dataset.GetRasterBand(band_index)
                if band is not None:
                    band.SetColorInterpretation(color)
        dataset = None
    return {
        "product_name": product_name,
        "bands": list(band_names),
        "path": str(final_path),
        "output_file": final_path.name,
        "target_epsg": effective_epsg,
        "target_resolution": effective_resolution,
        "histogram_stretch": histogram_stretch,
        "stretch": stretch_stats,
    }


def _resolve_sentinel_s2_processing_settings(
    processing_options: dict[str, Any] | None,
    *,
    output_count: int,
) -> dict[str, Any]:
    processing = dict(processing_options or {})
    histogram = processing.get("histogram_stretch")
    percentiles = _coerce_percentiles(processing.get("stretch_percentiles"))
    block_size = _coerce_positive_int(processing.get("block_size"), 256)
    overview_factors = _coerce_overview_factors(processing.get("overview_factors"))
    target_epsg = str(processing.get("target_epsg") or "").strip() or None
    target_resolution = processing.get("target_resolution")
    if target_resolution not in (None, ""):
        target_resolution = float(target_resolution)
    else:
        target_resolution = None
    resample_alg = str(processing.get("resample_alg") or "bilinear")
    compression = str(processing.get("compression") or "DEFLATE")
    parallel_mode = str(processing.get("parallel_mode") or os.environ.get("S2_PARALLEL_MODE") or "threads").strip().lower()
    if parallel_mode not in SERIAL_MODES:
        # S2 has only ever fanned out over threads; "processes" keeps meaning threads here.
        parallel_mode = "threads"
    requested_parallel_workers = processing.get("parallel_workers")
    if requested_parallel_workers in (None, "", 0, "0"):
        requested_parallel_workers = os.environ.get("S2_PRODUCT_WORKERS")
    product_workers = _resolve_product_workers(requested_parallel_workers, output_count)
    requested_gdal_threads = processing.get("gdal_num_threads")
    if requested_gdal_threads in (None, ""):
        requested_gdal_threads = os.environ.get("S2_GDAL_NUM_THREADS")
    gdal_num_threads = _resolve_gdal_num_threads(requested_gdal_threads, product_workers)
    warp_memory_limit_mb = _resolve_warp_memory_limit_mb(processing.get("warp_memory_limit_mb"))
    histogram_enabled = bool(histogram)
    return {
        "target_epsg": target_epsg,
        "target_resolution": target_resolution,
        "resample_alg": resample_alg,
        "histogram_stretch": histogram_enabled,
        "percentiles": percentiles,
        "compression": compression,
        "overview_factors": overview_factors,
        "block_size": block_size,
        "parallel_mode": parallel_mode,
        "product_workers": product_workers,
        "gdal_num_threads": gdal_num_threads,
        "warp_memory_limit_mb": warp_memory_limit_mb,
        "work_dir": resolve_work_dir(processing.get("work_dir")),
        "runtime": resolve_gdal_runtime(processing, requested_gdal_threads, gdal_num_threads),
    }


def process_sentinel_s2_safe(
    *,
    input_dataset: str,
    output_dir: Path,
    product_bands: dict[str, tuple[str, str, str]],
    output_names: dict[str, str],
    processing_options: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Generate RGB band-combination GeoTIFFs from a Sentinel-2 SAFE product.

    Options for unattended and bulk runs, all in ``processing_options``:

    ``work_dir``
        Where intermediates are written (default: ``output_dir``). Each product
        gets its own temporary directory there, removed on success and failure.
        Outputs appear under their final name only once complete.
    ``gdal_num_threads``
        GDAL threads per product for the warp and, when products run one at a
        time (``serial``), also ``GDAL_NUM_THREADS`` for JP2 decoding and
        compression. Defaults to the CPUs this process may use (affinity and
        cgroup quota). Set it when running several calls at once.
    ``gdal_cachemax_mb``
        GDAL block cache for the duration of the call (default: ``GDAL_CACHEMAX``).
    ``parallel_mode``
        ``threads`` (default) or ``serial``.

    Every requested product is attempted. If one of several fails,
    :class:`pysent.errors.PartialFailure` is raised after the rest finish. A
    scene with no valid pixels raises :class:`pysent.errors.EmptySceneError`
    when ``histogram_stretch`` is on.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    settings = _resolve_sentinel_s2_processing_settings(processing_options, output_count=len(output_names))
    jobs = [
        (
            product_name,
            {
                "input_dataset": input_dataset,
                "product_name": product_name,
                "band_names": product_bands[product_name],
                "output_path": str(output_dir / output_name),
                "work_dir": settings["work_dir"],
                "target_epsg": settings["target_epsg"],
                "target_resolution": settings["target_resolution"],
                "resample_alg": settings["resample_alg"],
                "block_size": settings["block_size"],
                "gdal_num_threads": settings["gdal_num_threads"],
                "warp_memory_limit_mb": settings["warp_memory_limit_mb"],
                "histogram_stretch": settings["histogram_stretch"],
                "percentiles": settings["percentiles"],
                "compression": settings["compression"],
                "overview_factors": settings["overview_factors"],
            },
        )
        for product_name, output_name in output_names.items()
    ]
    return run_product_jobs(
        _process_sentinel_s2_product,
        jobs,
        parallel_mode=settings["parallel_mode"],
        workers=settings["product_workers"],
        runtime=settings["runtime"],
    )
