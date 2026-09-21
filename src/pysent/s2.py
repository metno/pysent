"""Dedicated Sentinel-2 SAFE to GeoTIFF RGB product conversion helpers."""
from __future__ import annotations

import math
import os
import re
import warnings
from collections.abc import Mapping, Sequence
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
    gdal_runtime,
    resolve_gdal_runtime,
    resolve_work_dir,
    run_product_jobs,
    scratch_dir,
)
from ._safe import find_safe_member, resolve_safe_root
from .errors import EmptySceneError


S2_SAFE_IMPLEMENTATION = "sentinel_s2_safe_quicklook"
S2_OVERVIEW_FACTORS: tuple[int, ...] = (2, 4, 8, 16)
S2_STRETCH_PERCENTILES: tuple[float, float] = (0.5, 99.5)
S2_STRETCH_METHOD = "percentile"
# Gamma < 1 lifts the mid-tones. Together with the 0.5/99.5 clip this is what a
# Sentinel-2 scene needs to read well without blowing out cloud tops; min/max
# alone renders a hazy scene almost black (see the phase 3 session log).
S2_STRETCH_GAMMA = 0.7
# Valid pixels start at 1 so that 0 means NoData and nothing else: a valid pixel
# must never come out transparent.
S2_VALID_FLOOR = 1
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


# Options that only mean something while the stretch is on.
_STRETCH_KEYS = ("stretch_percentiles", "stretch_method", "stretch_gamma")


def _resolve_stretch_options(processing: dict[str, Any]) -> tuple[tuple[float, float], str, float]:
    """Percentiles, method and gamma, accepting the Sentinel-1 spelling as well.

    Sentinel-1 takes ``histogram_stretch={"percentiles": ...}`` while Sentinel-2
    takes a bool plus ``stretch_percentiles``; both are accepted here, and in
    :mod:`pysent.s1`, so an option written for one platform is not silently
    dropped by the other.
    """
    requested = processing.get("stretch_percentiles")
    histogram = processing.get("histogram_stretch")
    if requested in (None, "") and isinstance(histogram, dict) and "percentiles" in histogram:
        warnings.warn(
            'histogram_stretch={"percentiles": ...} is deprecated for Sentinel-2; '
            "pass stretch_percentiles=(low, high) instead",
            DeprecationWarning,
            stacklevel=4,
        )
        requested = histogram.get("percentiles")
    percentiles = _coerce_percentiles(requested)
    method = str(processing.get("stretch_method") or S2_STRETCH_METHOD).strip().lower()
    if method not in {"percentile", "minmax"}:
        raise ValueError(f"stretch_method must be 'percentile' or 'minmax', not {method!r}")
    if method == "minmax" and requested not in (None, ""):
        warnings.warn(
            "stretch_percentiles has no effect with stretch_method='minmax'",
            RuntimeWarning,
            stacklevel=3,
        )
    gamma = processing.get("stretch_gamma")
    gamma = S2_STRETCH_GAMMA if gamma in (None, "") else float(gamma)
    if gamma <= 0:
        raise ValueError(f"stretch_gamma must be positive, not {gamma!r}")
    return percentiles, method, gamma


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


def _select_band_sources(
    sources: dict[str, dict[str, object]],
    band_names: Sequence[str],
) -> tuple[list[dict[str, object]], str | None, float | None]:
    """Pick the requested bands out of an already collected scene, with their common grid."""
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


def _resolve_selected_band_sources(
    input_dataset: str,
    band_names: Sequence[str],
) -> tuple[list[dict[str, object]], str | None, float | None]:
    return _select_band_sources(_collect_sentinel_s2_band_sources(input_dataset), band_names)


def _build_sentinel_s2_stack_vrt(
    input_dataset: str,
    band_names: Sequence[str],
    vrt_path: Path,
    sources: dict[str, dict[str, object]] | None = None,
) -> tuple[str | None, float | None, list[Path]]:
    if sources is None:
        sources = _collect_sentinel_s2_band_sources(input_dataset)
    selected_sources, target_epsg, target_resolution = _select_band_sources(sources, band_names)
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
            raise RuntimeError(f"Unable to build Sentinel-2 band VRT for {', '.join(band_names)}")
        stacked = None
        return target_epsg, target_resolution, single_band_vrts
    except Exception:
        for path in single_band_vrts:
            path.unlink(missing_ok=True)
        raise


def _warp_sentinel_s2_bands(
    input_dataset: str,
    band_names: Sequence[str],
    warped_path: Path,
    *,
    target_epsg: str | None,
    target_resolution: float | None,
    resample_alg: str,
    block_size: int,
    gdal_num_threads: str,
    warp_memory_limit_mb: float | None,
    sources: dict[str, dict[str, object]] | None = None,
    compression: str = "LZW",
    interleave: str = "PIXEL",
) -> tuple[str | None, float | None]:
    """Warp the given bands of a SAFE product into one stacked GeoTIFF."""
    vrt_path = warped_path.with_name(f"{warped_path.stem}.stack.vrt")
    resolved_epsg, resolved_resolution, child_vrts = _build_sentinel_s2_stack_vrt(
        input_dataset, band_names, vrt_path, sources
    )
    effective_epsg = (target_epsg or resolved_epsg or "").strip() or None
    effective_resolution = float(target_resolution or resolved_resolution or 10.0)
    creation_options = [
        "TILED=YES",
        f"BLOCKXSIZE={block_size}",
        f"BLOCKYSIZE={block_size}",
        "BIGTIFF=IF_SAFER",
        f"INTERLEAVE={interleave}",
    ]
    if compression and compression.upper() not in {"NONE", "NO"}:
        creation_options.insert(0, f"COMPRESS={compression.upper()}")
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
                        creationOptions=creation_options,
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
    """Warp one product's three bands into an LZW-compressed GeoTIFF."""
    return _warp_sentinel_s2_bands(
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


# Percentiles are read from a decimated grid rather than every pixel: an exact
# pass costs about as much as the warp itself, while 4 million samples put the
# 0.5/99.5 points within a digital number of the exact answer.
_PERCENTILE_SAMPLE_SIDE = 2048


def _band_percentiles(
    band: gdal.Band,
    description: str,
    percentiles: tuple[float, float],
) -> tuple[float, float]:
    """Percentile range of one band, ignoring NoData, from a decimated read."""
    width = min(band.XSize, _PERCENTILE_SAMPLE_SIDE)
    height = min(band.YSize, _PERCENTILE_SAMPLE_SIDE)
    with gdal_errors():
        sample = band.ReadAsArray(buf_xsize=width, buf_ysize=height)  # nearest: real pixel values
    if sample is None:
        raise RuntimeError(f"Unable to read {description}")
    nodata = band.GetNoDataValue()
    values = sample[np.isfinite(sample)] if sample.dtype.kind == "f" else sample.ravel()
    if nodata is not None:
        values = values[values != nodata]
    if values.size == 0:
        # Too sparse to sample, or genuinely empty: let the exact pass decide.
        return _band_min_max(band, description)
    low, high = (float(value) for value in np.percentile(values, list(percentiles)))
    if high <= low:  # a near-flat band: use its full range instead
        return _band_min_max(band, description)
    return low, high


def _stretch_range(
    band: gdal.Band,
    description: str,
    *,
    method: str,
    percentiles: tuple[float, float],
) -> tuple[float, float]:
    """The source range a band is stretched from."""
    if method == "minmax":
        return _band_min_max(band, description)
    return _band_percentiles(band, description, percentiles)


_LUT_DTYPES = {gdal.GDT_Byte: 256, gdal.GDT_UInt16: 65536}


def _stretch_lut(low: float, high: float, gamma: float, size: int, nodata: float | None) -> np.ndarray:
    """Map every possible source value to its output byte, NoData to 0."""
    values = np.arange(size, dtype=np.float32)
    norm = np.clip((values - low) / (high - low), 0.0, 1.0)
    if gamma != 1.0:
        norm = norm ** gamma
    lut = np.rint(norm * (255 - S2_VALID_FLOOR) + S2_VALID_FLOOR).astype(np.uint8)
    if nodata is not None and float(nodata).is_integer() and 0 <= int(nodata) < size:
        lut[int(nodata)] = 0  # the one value that stays transparent
    return lut


def _write_stretched_with_lut(
    source: gdal.Dataset,
    output_path: Path,
    ranges: list[tuple[float, float]],
    *,
    gamma: float,
    creation_options: list[str],
    strip_rows: int = 1024,
) -> None:
    """Apply the per-band curves through a lookup table, a strip at a time.

    Same result as ``gdal.Translate`` with scale and exponent (within one digital
    number) at a third of the cost, because a table lookup replaces a ``pow()``
    per pixel, and it never holds more than a strip of the raster in memory.
    """
    width, height = source.RasterXSize, source.RasterYSize
    with gdal_errors():
        target = gdal.GetDriverByName("GTiff").Create(
            str(output_path), width, height, source.RasterCount, gdal.GDT_Byte, creation_options
        )
    if target is None:
        raise RuntimeError(f"Unable to create {output_path.name}")
    target.SetGeoTransform(source.GetGeoTransform())
    target.SetProjection(source.GetProjectionRef())
    luts = []
    for index, (low, high) in enumerate(ranges, start=1):
        band = source.GetRasterBand(index)
        size = _LUT_DTYPES[band.DataType]
        luts.append(_stretch_lut(low, high, gamma, size, band.GetNoDataValue()))
        target.GetRasterBand(index).SetNoDataValue(0)
    with gdal_errors():
        for row in range(0, height, strip_rows):
            rows = min(strip_rows, height - row)
            for index in range(1, source.RasterCount + 1):
                chunk = source.GetRasterBand(index).ReadAsArray(0, row, width, rows)
                target.GetRasterBand(index).WriteArray(luts[index - 1][chunk], 0, row)
    target = None


def _write_stretched_with_translate(
    warped_path: Path,
    output_path: Path,
    ranges: list[tuple[float, float]],
    *,
    gamma: float,
    creation_options: list[str],
) -> None:
    """The same stretch for source types a lookup table cannot cover (float, 32-bit)."""
    with gdal_errors():
        translated = gdal.Translate(
            str(output_path),
            str(warped_path),
            options=gdal.TranslateOptions(
                format="GTiff",
                outputType=gdal.GDT_Byte,
                scaleParams=[[low, high, S2_VALID_FLOOR, 255] for low, high in ranges],
                exponents=[gamma] * len(ranges) if gamma != 1.0 else None,
                creationOptions=creation_options,
            ),
        )
    if translated is None:
        raise RuntimeError(f"Unable to stretch Sentinel-2 RGB product to {output_path.name}")


def _write_stretched_sentinel_s2_rgb(
    warped_path: Path,
    output_path: Path,
    *,
    percentiles: tuple[float, float] = S2_STRETCH_PERCENTILES,
    block_size: int,
    overview_factors: tuple[int, ...],
    compression: str = "DEFLATE",
    method: str = S2_STRETCH_METHOD,
    gamma: float = S2_STRETCH_GAMMA,
) -> dict[str, Any]:
    """Stretch the warped RGB to a tiled, compressed 8-bit GeoTIFF with overviews.

    Each band is scaled from its source range - percentiles by default, or the
    exact min/max with ``method="minmax"`` - into ``[1, 255]``, through a gamma
    curve that lifts the mid-tones. **0 is reserved for NoData**, so no valid
    pixel can come out transparent.

    The per-band range is returned for the job record. Raises
    :class:`pysent.errors.EmptySceneError` if a band has no valid pixel.
    """
    with gdal_errors():
        source = gdal.Open(str(warped_path), gdal.GA_ReadOnly)
    if source is None:
        raise RuntimeError(f"Unable to open warped Sentinel-2 raster: {warped_path}")
    method = str(method or S2_STRETCH_METHOD).strip().lower()
    gamma = float(gamma)
    ranges: list[tuple[float, float]] = []
    for band_index in range(1, source.RasterCount + 1):
        band = source.GetRasterBand(band_index)
        low, high = _stretch_range(
            band, f"Sentinel-2 band {band_index}", method=method, percentiles=percentiles
        )
        ranges.append((low, high if high > low else low + 1.0))

    creation_options = [
        f"COMPRESS={compression}",
        "TILED=YES",
        f"BLOCKXSIZE={block_size}",
        f"BLOCKYSIZE={block_size}",
        "BIGTIFF=IF_SAFER",
        "INTERLEAVE=PIXEL",
    ]
    if all(source.GetRasterBand(index).DataType in _LUT_DTYPES for index in range(1, source.RasterCount + 1)):
        _write_stretched_with_lut(source, output_path, ranges, gamma=gamma, creation_options=creation_options)
    else:
        source = None
        _write_stretched_with_translate(
            warped_path, output_path, ranges, gamma=gamma, creation_options=creation_options
        )
    source = None

    with gdal_errors():
        dataset = gdal.Open(str(output_path), gdal.GA_Update)
        if dataset is not None and overview_factors:
            dataset.BuildOverviews("AVERAGE", list(overview_factors))
        dataset = None

    stats: dict[str, Any] = {
        "p_low": [low for low, _ in ranges],
        "p_high": [high for _, high in ranges],
        "method": method,
    }
    if method != "minmax":
        stats["percentiles"] = [float(value) for value in percentiles]
    if gamma != 1.0:
        stats["gamma"] = gamma
    return stats


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
    product_name: str,
    band_names: tuple[str, str, str],
    output_path: str,
    stack: dict[str, Any] | BaseException,
    block_size: int,
    histogram_stretch: bool,
    percentiles: tuple[float, float],
    compression: str,
    overview_factors: tuple[int, ...],
    stretch_method: str = S2_STRETCH_METHOD,
    stretch_gamma: float = S2_STRETCH_GAMMA,
) -> dict[str, Any]:
    """Write one RGB product by selecting its three bands from the scene's warped stack."""
    if isinstance(stack, BaseException):
        raise stack  # the shared warp failed; every product of that grid reports it
    final_path = Path(output_path)
    stack_path = Path(str(stack["stack_path"]))
    # A band-subset VRT costs nothing to build and lets the writers work unchanged.
    subset_path = stack_path.with_name(f"{stack_path.stem}.{_sanitize_name_fragment(product_name).lower()}.vrt")
    stretch_stats: dict[str, Any] | None = None
    try:
        with gdal_errors():
            selected = gdal.Translate(
                str(subset_path),
                str(stack_path),
                options=gdal.TranslateOptions(format="VRT", bandList=list(stack["band_indexes"])),
            )
        if selected is None:
            raise RuntimeError(f"Unable to select bands {', '.join(band_names)} from the Sentinel-2 stack")
        selected = None

        # The final file is moved into place only once complete, so a failure or a
        # kill leaves no truncated output under the final name.
        with atomic_output(final_path) as partial_path:
            if histogram_stretch:
                stretch_stats = _write_stretched_sentinel_s2_rgb(
                    subset_path,
                    partial_path,
                    percentiles=percentiles,
                    block_size=block_size,
                    overview_factors=overview_factors,
                    compression=compression,
                    method=stretch_method,
                    gamma=stretch_gamma,
                )
            else:
                _translate_sentinel_s2_rgb(
                    subset_path,
                    partial_path,
                    compression=compression,
                    block_size=block_size,
                    overview_factors=overview_factors,
                )
            dataset = gdal.Open(str(partial_path), gdal.GA_Update)
            if dataset is not None:
                for band_index, color in enumerate((gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand), start=1):
                    band = dataset.GetRasterBand(band_index)
                    if band is not None:
                        band.SetColorInterpretation(color)
            dataset = None
    finally:
        subset_path.unlink(missing_ok=True)
    return {
        "product_name": product_name,
        "bands": list(band_names),
        "path": str(final_path),
        "output_file": final_path.name,
        "target_epsg": stack["target_epsg"],
        "target_resolution": stack["target_resolution"],
        "histogram_stretch": histogram_stretch,
        "stretch": stretch_stats,
    }


def _sentinel_s2_band_union(product_bands: Mapping[str, tuple[str, str, str]], product_names: Sequence[str]) -> tuple[str, ...]:
    """Every band the given products need, in first-use order."""
    union: list[str] = []
    for name in product_names:
        for band in product_bands[name]:
            if band not in union:
                union.append(band)
    return tuple(union)


def _prepare_sentinel_s2_stacks(
    input_dataset: str,
    product_bands: Mapping[str, tuple[str, str, str]],
    product_names: Sequence[str],
    scratch: Path,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Warp the bands the scene needs once per output grid.

    All the default products share bands (B3 is in all three), and each product
    used to decode and warp its own copy. Products are grouped by the grid they
    would be warped to - the same ``(CRS, resolution)`` each would have resolved
    on its own - so grouping cannot change any output.

    One exception: when the request is coarser than the scene's own resolution,
    GDAL warps from source overviews, and which overview it reads depends on the
    bands in the stack. Such products keep a stack of their own, so their output
    stays exactly what a per-product warp produced.

    Returns a mapping of product name to its stack (path, band indexes, grid),
    or to the exception that prevented it, so each product keeps its own status.
    """
    try:
        sources = _collect_sentinel_s2_band_sources(input_dataset)
    except Exception as exc:
        return dict.fromkeys(product_names, exc)

    prepared: dict[str, Any] = {}
    grids: dict[tuple[str | None, float], list[str]] = {}
    for name in product_names:
        try:
            _, resolved_epsg, resolved_resolution = _select_band_sources(sources, product_bands[name])
        except Exception as exc:
            prepared[name] = exc
            continue
        epsg = (settings["target_epsg"] or resolved_epsg or "").strip() or None
        resolution = float(settings["target_resolution"] or resolved_resolution or 10.0)
        native = float(resolved_resolution or resolution)
        # Downsampling reads source overviews, whose choice depends on the stack's
        # bands: keep such a product on its own so its output does not change.
        key: tuple = (epsg, resolution) if resolution <= native else (epsg, resolution, name)
        grids.setdefault(key, []).append(name)

    for index, (grid, names) in enumerate(grids.items(), start=1):
        epsg, resolution = grid[0], grid[1]
        band_names = _sentinel_s2_band_union(product_bands, names)
        stack_path = scratch / f"stack{index}.warp.tif"
        try:
            _warp_sentinel_s2_bands(
                input_dataset,
                band_names,
                stack_path,
                target_epsg=epsg,
                target_resolution=resolution,
                resample_alg=settings["resample_alg"],
                block_size=settings["block_size"],
                gdal_num_threads=settings["gdal_num_threads"],
                warp_memory_limit_mb=settings["warp_memory_limit_mb"],
                sources=sources,
                compression=settings["intermediate_compression"],
                interleave="BAND",
            )
        except Exception as exc:
            prepared.update(dict.fromkeys(names, exc))
            continue
        for name in names:
            prepared[name] = {
                "stack_path": str(stack_path),
                "band_indexes": [band_names.index(band) + 1 for band in product_bands[name]],
                "target_epsg": epsg,
                "target_resolution": resolution,
            }
    return prepared


def _resolve_sentinel_s2_processing_settings(
    processing_options: dict[str, Any] | None,
    *,
    output_count: int,
) -> dict[str, Any]:
    processing = dict(processing_options or {})
    histogram = processing.get("histogram_stretch")
    percentiles, method, gamma = _resolve_stretch_options(processing)
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
    # The scene stack is read once per product and deleted; compressing it costs
    # more time than the extra scratch space is usually worth.
    intermediate_compression = str(processing.get("intermediate_compression") or "NONE")
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
    if not histogram_enabled and any(key in processing for key in _STRETCH_KEYS):
        warnings.warn(
            "stretch options were given but histogram_stretch is off, so they do nothing",
            RuntimeWarning,
            stacklevel=3,
        )
    return {
        "target_epsg": target_epsg,
        "target_resolution": target_resolution,
        "resample_alg": resample_alg,
        "histogram_stretch": histogram_enabled,
        "percentiles": percentiles,
        "stretch_method": method,
        "stretch_gamma": gamma,
        "compression": compression,
        "intermediate_compression": intermediate_compression,
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

    The bands the requested products need are decoded and warped **once per
    scene** (grouped by output grid), then each product is cut from that stack.

    Options for unattended and bulk runs, all in ``processing_options``:

    ``work_dir``
        Where intermediates are written (default: ``output_dir``). The scene's
        stack lives in a temporary directory there, removed on success and
        failure. Budget about 2 bytes per pixel per band needed: roughly 1.2 GB
        for the three default products of a 10 m scene. Outputs appear under
        their final name only once complete.
    ``intermediate_compression``
        Compression of that stack (default: none, the fastest). Set e.g.
        ``"LZW"`` when scratch space matters more than time.
    ``gdal_num_threads``
        GDAL threads per product for the warp and, when products run one at a
        time (``serial``), also ``GDAL_NUM_THREADS`` for JP2 decoding and
        compression. Defaults to the CPUs this process may use (affinity and
        cgroup quota). Set it when running several calls at once.
    ``gdal_cachemax_mb``
        GDAL block cache for the duration of the call (default: ``GDAL_CACHEMAX``).
    ``parallel_mode``
        ``threads`` (default) or ``serial``, for the per-product stretch.

    Every requested product is attempted. If one of several fails,
    :class:`pysent.errors.PartialFailure` is raised after the rest finish. A
    scene with no valid pixels raises :class:`pysent.errors.EmptySceneError`
    when ``histogram_stretch`` is on.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    settings = _resolve_sentinel_s2_processing_settings(processing_options, output_count=len(output_names))
    scratch_root = Path(settings["work_dir"]) if settings["work_dir"] else output_dir
    product_names = list(output_names)

    with scratch_dir(scratch_root) as scratch:
        with gdal_runtime(**settings["runtime"]):
            stacks = _prepare_sentinel_s2_stacks(input_dataset, product_bands, product_names, scratch, settings)
        jobs = [
            (
                product_name,
                {
                    "product_name": product_name,
                    "band_names": product_bands[product_name],
                    "output_path": str(output_dir / output_names[product_name]),
                    "stack": stacks[product_name],
                    "block_size": settings["block_size"],
                    "histogram_stretch": settings["histogram_stretch"],
                    "percentiles": settings["percentiles"],
                    "stretch_method": settings["stretch_method"],
                    "stretch_gamma": settings["stretch_gamma"],
                    "compression": settings["compression"],
                    "overview_factors": settings["overview_factors"],
                },
            )
            for product_name in product_names
        ]
        return run_product_jobs(
            _process_sentinel_s2_product,
            jobs,
            parallel_mode=settings["parallel_mode"],
            workers=settings["product_workers"],
            runtime=settings["runtime"],
        )
