"""Fast catalogue thumbnails of Sentinel-1 and Sentinel-2 SAFE products.

A thumbnail is a few hundred pixels on a side, so it does not need the scene's
full resolution: these functions read only the coarsest data that still covers
the thumbnail, and apply the same stretch the quicklook products use.

``sentinel_s2_thumbnail``
    Reads each band from the JPEG 2000 file's own reduced-resolution level
    (a 10 m band's 1/16 level is 160 m, 687 pixels across a tile) on the
    tile's own grid: nothing is warped. The per-band percentile stretch and
    gamma of :mod:`pysent.s2`, then averaged down to the thumbnail size.
``sentinel_s1_thumbnail``
    Reads every n-th row of each measurement TIFF at full width, a few
    rows at a time from several threads (the strips are one row each, and
    on network storage each read is a round trip), averages them across
    range into a coarse grid, warps that small grid with the product's
    GCPs, then applies the dB percentile stretch of :mod:`pysent.s1`.

Both return the bytes and the alpha on the same grid, with the stretch numbers,
so a caller can write them as a PNG (:func:`write_thumbnail_png`) or combine
them first (a VV/VH composite, say).
"""
from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
from osgeo import gdal

from ._runtime import atomic_output, gdal_errors
from ._safe import resolve_safe_root
from .errors import EmptySceneError
from .s1 import (
    S1_STRETCH_METHOD,
    S1_STRETCH_PERCENTILES,
    S1_TARGET_EPSG,
    _resolve_safe_manifest_path,
)
from .s2 import (
    S2_STRETCH_GAMMA,
    S2_STRETCH_METHOD,
    S2_STRETCH_PERCENTILES,
    S2_SUPPORTED_BANDS,
    _canonical_band_name,
    _collect_sentinel_s2_band_sources,
    _stretch_lut,
    resolve_sentinel_s2_preset,
)


__all__ = [
    "THUMBNAIL_SIZE",
    "sentinel_s1_thumbnail",
    "sentinel_s2_thumbnail",
    "write_thumbnail_png",
]

THUMBNAIL_SIZE = 256
# The stretch is computed on a grid at least this many times the thumbnail,
# then the stretched bytes are averaged down: the order the quicklook path
# works in (stretch the product, then shrink it), so the picture matches.
_OVERSAMPLE = 2
_S1_READ_THREADS = 16
_S2_READ_THREADS = 12
_COPY_CHUNK = 1 << 20
_S1_ROWS_PER_TASK = 8
_S1_STATS_COLUMNS = 32
_S1_ROW_OVERSAMPLE = 2


def _fit(width: int, height: int, size: int) -> tuple[int, int]:
    """``size`` on the long side, the aspect kept."""
    scale = size / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _shrink(arrays: Sequence[np.ndarray], width: int, height: int) -> list[np.ndarray]:
    """Average byte arrays down to ``width`` x ``height`` (GDAL's ``average``)."""
    rows, cols = arrays[0].shape
    memory = gdal.GetDriverByName("MEM").Create("", cols, rows, len(arrays), gdal.GDT_Byte)
    for index, array in enumerate(arrays, start=1):
        memory.GetRasterBand(index).WriteArray(np.asarray(array, dtype=np.uint8))
    with gdal_errors():
        small = gdal.Translate("", memory, format="MEM", width=width, height=height, resampleAlg="average")
    out = [small.GetRasterBand(index).ReadAsArray() for index in range(1, len(arrays) + 1)]
    small = memory = None
    return out


def _scaled_geotransform(geotransform: Sequence[float], cols: int, rows: int, width: int, height: int) -> tuple[float, ...]:
    x0, dx, rx, y0, ry, dy = geotransform
    sx, sy = cols / width, rows / height
    return (x0, dx * sx, rx * sy, y0, ry * sx, dy * sy)


def _reduced_side(band: gdal.Band, wanted: int) -> int:
    """The smallest level of ``band`` (full or overview) at least ``wanted`` pixels wide."""
    sides = [band.XSize] + [band.GetOverview(i).XSize for i in range(band.GetOverviewCount())]
    fitting = [side for side in sides if side >= wanted]
    return min(fitting) if fitting else band.XSize


_S2_BAND_JP2 = re.compile(r"_(B\d\d|B8A)(?:_(\d+)m)?\.jp2$", re.IGNORECASE)


def _s2_listed_band_files(input_dataset: str) -> dict[str, str] | None:
    """Each band's JPEG 2000 file, found by listing the SAFE's one granule.

    Faster than opening the product with GDAL's SENTINEL2 driver, which parses
    the metadata and opens every resolution's subdataset. A Level-2A band that
    comes at several resolutions gives its finest. ``None`` when the input is
    not a zip or ``.SAFE`` with exactly one granule (pre-2016 products hold
    several); the caller then asks the driver.
    """
    ref = (input_dataset or "").strip().rstrip("/")
    if not (ref.endswith(".SAFE") or ref.lower().endswith(".zip")):
        return None
    root = resolve_safe_root(ref)
    granules = [entry.rstrip("/") for entry in gdal.ReadDir(f"{root}/GRANULE") or [] if entry not in (".", "..")]
    if len(granules) != 1:
        return None
    image_dir = f"{root}/GRANULE/{granules[0]}/IMG_DATA"
    found: dict[str, tuple[int, str]] = {}
    for entry in gdal.ReadDirRecursive(image_dir) or []:
        match = _S2_BAND_JP2.search(entry)
        if match is None:
            continue
        name = _canonical_band_name(match.group(1))
        resolution = int(match.group(2) or 0)
        if name not in found or resolution < found[name][0]:
            found[name] = (resolution, f"{image_dir}/{entry}")
    return {name: path for name, (_, path) in found.items()} or None


def _s2_band_files(input_dataset: str) -> dict[str, str]:
    listed = _s2_listed_band_files(input_dataset)
    if listed is not None:
        return listed
    from .vrt import _s2_band_file

    sources = _collect_sentinel_s2_band_sources(input_dataset)
    return {
        name: _s2_band_file(str(source["subdataset_name"]), name)
        for name, source in sources.items()
        if name in S2_SUPPORTED_BANDS
    }


def _deflated(path: str) -> bool:
    """Whether ``path`` is a zip member stored compressed (not ``STORED``)."""
    if not path.startswith("/vsizip/"):
        return False
    method = (gdal.GetFileMetadata(path, "ZIP") or {}).get("COMPRESSION_METHOD", "0")
    return not method.startswith("0")


def _copy_to_memory(path: str) -> str:
    """``path`` copied into a ``/vsimem/`` file with forward reads only; its name."""
    name = f"/vsimem/pysent-thumbnail-{uuid.uuid4().hex}.jp2"
    with gdal_errors():
        source = gdal.VSIFOpenL(path, "rb")
        try:
            target = gdal.VSIFOpenL(name, "wb")
            try:
                while chunk := gdal.VSIFReadL(1, _COPY_CHUNK, source):
                    gdal.VSIFWriteL(chunk, 1, len(chunk), target)
            finally:
                gdal.VSIFCloseL(target)
        except BaseException:
            gdal.Unlink(name)
            raise
        finally:
            gdal.VSIFCloseL(source)
    return name


def _read_reduced(paths: Sequence[str], cols: int, rows: int) -> list[np.ndarray]:
    """Every file read whole into ``cols`` x ``rows``: one read per file, the files at once.

    Each read decodes on one thread: a thumbnail worker runs several tasks on as
    many CPUs, and letting each read decode on all of them made six thumbnails
    at once slower (17.5 s against 14.3 s on six CPUs). Splitting a read into
    windows on their own handles was slower everywhere, cold or warm.

    A band zipped compressed (the reprocessed archive, baseline N0500, deflates
    its JPEG 2000 files) is first copied into memory with forward reads: the
    decoder goes back to the codestream's start for every tile, and every
    backward seek in a deflated member inflates it again from its start (2.5 s
    for a band, against 0.15 s to copy it and 0.55 s to decode the copy). The
    bytes decoded are the same either way. A stored member is read in place, so
    a cold read fetches only what the level needs.
    """
    out = [np.zeros((rows, cols), dtype=np.uint16) for _ in paths]

    def read(index: int) -> None:
        gdal.SetThreadLocalConfigOption("GDAL_NUM_THREADS", "1")
        path = paths[index]
        copied = _deflated(path)
        try:
            if copied:
                path = _copy_to_memory(path)
            with gdal_errors():
                dataset = gdal.Open(path, gdal.GA_ReadOnly)
                # A buffer the size of one of the file's own levels makes GDAL decode that
                # level and nothing finer; "average" matters only for a band whose levels do
                # not line up with it (a 60 m band read on the 10 m bands' level).
                out[index][:] = dataset.GetRasterBand(1).ReadAsArray(
                    buf_xsize=cols, buf_ysize=rows, resample_alg=gdal.GRIORA_Average,
                )
            dataset = None
        finally:
            if copied and path != paths[index]:
                gdal.Unlink(path)
            gdal.SetThreadLocalConfigOption("GDAL_NUM_THREADS", None)

    with ThreadPoolExecutor(max_workers=min(len(paths), _S2_READ_THREADS)) as pool:
        list(pool.map(read, range(len(paths))))
    return out


def _erode(mask: np.ndarray) -> np.ndarray:
    """``mask`` without the pixels that touch a ``False`` one (4-neighbours)."""
    inner = mask.copy()
    inner[1:] &= mask[:-1]
    inner[:-1] &= mask[1:]
    inner[:, 1:] &= mask[:, :-1]
    inner[:, :-1] &= mask[:, 1:]
    return inner


def _median3(array: np.ndarray) -> np.ndarray:
    """3 x 3 median, the border repeated."""
    padded = np.pad(array, 1, mode="edge")
    rows, cols = array.shape
    window = np.stack([padded[i:i + rows, j:j + cols] for i in range(3) for j in range(3)])
    return np.median(window, axis=0)


def _s2_reduced_range(
    array: np.ndarray, inner: np.ndarray, percentiles: tuple[float, float], name: str
) -> tuple[float, float]:
    """The stretch range of one band read at a reduced JPEG 2000 level.

    A reduced level is the wavelet's low-pass band, which undershoots beside a
    sharp dark edge: a lake shore in the near infrared reads several hundred
    numbers darker than the water does at full resolution, and those pixels
    took the 0.5 % point (919 against the quicklook's 1227 on a lake-rich
    tile). A 3 x 3 median removes that undershoot, so the low point comes from
    it (1232); the high point does not, because the bright tail is small real
    features (cloud, snow) that a median would remove too (4018 against 4827,
    plain 4896). Measured on five tiles: low within 0-115, high within 1.5-3 %
    of the quicklook's own range.
    """
    values = array[inner]
    if values.size == 0:
        values = array[array > 0]  # too small to erode: use what there is
    if values.size == 0:
        raise EmptySceneError(f"Sentinel-2 band {name} has no valid pixels")
    smoothed = _median3(array)[inner] if inner.any() else values
    low = float(np.percentile(smoothed, percentiles[0]))
    high = float(np.percentile(values, percentiles[1]))
    if high <= low:
        low, high = float(values.min()), float(values.max())
    if high <= low:
        high = low + 1.0
    return low, high


def sentinel_s2_thumbnail(
    input_dataset: str,
    *,
    bands: str | Sequence[str] = "false_color_vegetation",
    size: int = THUMBNAIL_SIZE,
    stretch_percentiles: tuple[float, float] = S2_STRETCH_PERCENTILES,
    stretch_gamma: float = S2_STRETCH_GAMMA,
) -> dict[str, Any]:
    """An RGB thumbnail of a Sentinel-2 SAFE product (zip, ``.SAFE`` or metadata XML).

    ``bands`` is a preset name (:data:`pysent.s2.S2_BAND_PRESETS` or
    :data:`pysent.s2.S2_DEFAULT_PRODUCTS`) or three band names. Each band is
    read from its JPEG 2000 file's smallest reduced-resolution level that is
    still at least twice ``size`` across, on the tile's own grid and CRS (the
    quicklook product is not reprojected either). Each band is stretched from
    its own ``stretch_percentiles`` of the valid pixels at that level, through
    ``stretch_gamma``, into 1-255 (0 is NoData), as
    :func:`pysent.s2.process_sentinel_s2_safe` stretches its products; the
    bytes are then averaged down to ``size`` on the long side.

    Returns ``rgb`` (3 x rows x cols, uint8), ``alpha`` (uint8, 255 where any
    band is valid, averaged at the edges), ``width``, ``height``,
    ``geotransform``, ``crs`` (WKT), ``bands``, ``read_side`` (the pixels
    across the level read) and ``stretch`` (``p_low``, ``p_high`` per band, in
    digital numbers, with the method, percentiles and gamma). Level-1C and
    Level-2A are read alike: the Level-2A offset is a constant per band and
    cancels in the stretch. Raises :class:`pysent.errors.EmptySceneError` when
    a band has no valid pixel.
    """
    files = _s2_band_files(input_dataset)
    band_names = resolve_sentinel_s2_preset(bands, list(files))
    paths = [files[name] for name in band_names]
    with gdal_errors():
        datasets = [gdal.Open(path, gdal.GA_ReadOnly) for path in paths]
    # The tile's grid from its finest band; every band covers the same extent.
    finest = max(datasets, key=lambda dataset: dataset.RasterXSize)
    if any(dataset.GetRasterBand(1).DataType != gdal.GDT_UInt16 for dataset in datasets):
        raise RuntimeError("Sentinel-2 bands are expected as UInt16")
    cols = _reduced_side(finest.GetRasterBand(1), _OVERSAMPLE * size)
    rows = max(1, round(finest.RasterYSize * cols / finest.RasterXSize))
    geotransform = finest.GetGeoTransform()
    full_cols, full_rows = finest.RasterXSize, finest.RasterYSize
    crs = finest.GetProjectionRef()
    datasets = finest = None
    data = _read_reduced(paths, cols, rows)

    valid = np.zeros((rows, cols), dtype=bool)
    for array in data:
        valid |= array > 0
    # The statistics leave out the pixels next to NoData: at a reduced level they
    # are part NoData, part scene, and on a tile the swath only partly covers they
    # took the low percentile from ~1200 to ~30 (a washed-out picture).
    inner = _erode(valid)
    ranges = []
    stretched = []
    for name, array in zip(band_names, data):
        low, high = _s2_reduced_range(array, inner, stretch_percentiles, name)
        ranges.append((low, high))
        lut = _stretch_lut(low, high, float(stretch_gamma), 65536, 0)
        stretched.append(lut[array])
    alpha = np.where(valid, 255, 0).astype(np.uint8)

    width, height = _fit(cols, rows, size)
    *rgb, alpha = _shrink([*stretched, alpha], width, height)
    return {
        "bands": list(band_names),
        "rgb": np.stack(rgb),
        "alpha": alpha,
        "width": width,
        "height": height,
        "geotransform": _scaled_geotransform(geotransform, full_cols, full_rows, width, height),
        "crs": crs,
        "read_side": cols,
        "stretch": {
            "p_low": [low for low, _ in ranges],
            "p_high": [high for _, high in ranges],
            "method": S2_STRETCH_METHOD,
            "percentiles": [float(value) for value in stretch_percentiles],
            "gamma": float(stretch_gamma),
        },
    }


def _s1_measurements(manifest_path: str) -> tuple[gdal.Dataset, dict[str, str]]:
    """The SAFE dataset (for its GCPs) and each polarisation's measurement TIFF."""
    with gdal_errors():
        safe = gdal.Open(manifest_path, gdal.GA_ReadOnly)
    if safe is None:
        raise RuntimeError(f"Unable to open Sentinel-1 SAFE dataset: {manifest_path}")
    tiffs = [name for name in safe.GetFileList() or [] if "/measurement/" in name and name.lower().endswith((".tiff", ".tif"))]
    found: dict[str, str] = {}
    for band_index in range(1, safe.RasterCount + 1):
        polarization = (safe.GetRasterBand(band_index).GetMetadataItem("POLARIZATION") or "").strip().upper()
        matches = [name for name in tiffs if f"-{polarization.lower()}-" in name.rsplit("/", 1)[-1]]
        if polarization and matches:
            found[f"Amplitude_{polarization}"] = matches[0]
    return safe, found


def _read_rows(path: str, rows: np.ndarray, threads: int) -> np.ndarray:
    """Full-width rows of a TIFF, several reads in flight at once."""
    local = threading.local()

    def read(chunk: np.ndarray) -> list[np.ndarray]:
        band = getattr(local, "band", None)
        if band is None:
            with gdal_errors():
                local.dataset = gdal.Open(path, gdal.GA_ReadOnly)
            band = local.band = local.dataset.GetRasterBand(1)
        width = band.XSize
        with gdal_errors():
            return [band.ReadAsArray(0, int(row), width, 1)[0] for row in chunk]

    chunks = [rows[start:start + _S1_ROWS_PER_TASK] for start in range(0, rows.size, _S1_ROWS_PER_TASK)]
    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        lines = [line for chunk in pool.map(read, chunks) for line in chunk]
    return np.stack(lines)


def _block_mean(lines: np.ndarray, step: int) -> np.ndarray:
    """Mean amplitude of each ``step`` columns, over valid (non-zero) samples; 0 where none."""
    rows, width = lines.shape
    cols = width // step
    blocks = lines[:, : cols * step].reshape(rows, cols, step).astype(np.float32)
    count = (blocks > 0).sum(axis=2)
    total = blocks.sum(axis=2)  # invalid samples are 0 and add nothing
    return np.where(count > 0, total / np.maximum(count, 1), 0.0).astype(np.float32)


def _s1_range(
    grid: np.ndarray, percentiles: tuple[float, float], method: str, variable: str
) -> dict[str, Any]:
    """The stretch range of one polarisation, from the rows read averaged in short blocks.

    How much the amplitude is averaged before the percentiles are taken moves
    them: a mean over more samples has less speckle, so its tails are shorter.
    The quicklook takes them on its 160 m grid; means of 32 samples across range
    on the rows read land within 0.35 dB of its 1/99 % points on six
    polarisations of three scenes, where the warped thumbnail grid itself (each
    cell a mean of hundreds) was up to 2.7 dB off on a scene full of small lakes.
    """
    values = grid[grid > 0].astype(np.float64)
    if values.size == 0:
        raise EmptySceneError(f"Sentinel-1 {variable} has no valid pixels")
    if method == "db":
        values = 20.0 * np.log10(values)
    low, high = (float(value) for value in np.percentile(values, list(percentiles)))
    if high <= low:
        low, high = float(values.min()), float(values.max())
    if high <= low:
        high = low + 1.0
    return {
        "min": float(values.min()),
        "max": float(values.max()),
        "p_low": low,
        "p_high": high,
        "unit": "dB" if method == "db" else "amplitude",
        "method": method,
        "percentiles": [float(value) for value in percentiles],
    }


def _s1_stretch(amplitude: np.ndarray, stretch: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """Bytes and validity of an amplitude grid on a given range, as
    :func:`pysent.s1.stretch_sentinel_s1_grayscale` maps them."""
    valid = np.isfinite(amplitude) & (amplitude > 0)
    values = amplitude.astype(np.float32, copy=False)
    if stretch["unit"] == "dB":
        values = np.where(valid, 20.0 * np.log10(np.where(valid, values, 1.0)), 0.0)
    low, high = stretch["p_low"], stretch["p_high"]
    scaled = np.clip((values - low) / (high - low), 0.0, 1.0)
    gray = np.zeros(amplitude.shape, dtype=np.uint8)
    gray[valid] = np.round(scaled[valid] * 255.0).astype(np.uint8)
    return gray, valid


def sentinel_s1_thumbnail(
    input_dataset: str,
    *,
    polarizations: Sequence[str] | None = None,
    size: int = THUMBNAIL_SIZE,
    stretch_percentiles: tuple[float, float] = S1_STRETCH_PERCENTILES,
    stretch_method: str = S1_STRETCH_METHOD,
    target_epsg: str = S1_TARGET_EPSG,
    use_tps: bool = False,
    threads: int = _S1_READ_THREADS,
) -> dict[str, Any]:
    """Grayscale thumbnails of a Sentinel-1 GRD SAFE product, one per polarisation, on one grid.

    ``polarizations`` names the amplitude variables (``["Amplitude_VV",
    "Amplitude_VH"]``); by default every one the product has. About
    ``2 * size`` rows of each measurement TIFF are read, evenly spaced, at
    full width, by ``threads`` threads; each row is averaged across range in
    blocks into a grid about ``2 * size`` on a side (the mean amplitude of the
    non-zero samples). That grid is warped to ``target_epsg`` with the
    product's GCPs - the polynomial transform unless ``use_tps`` - at twice
    ``size``, stretched as :func:`pysent.s1.stretch_sentinel_s1_grayscale`
    does (``stretch_method``, ``stretch_percentiles``: dB and 1-99 by default,
    as the quicklook products) on a range taken from the rows read averaged
    32 samples at a time (closer to the quicklook's own range than the
    thumbnail grid, see :func:`_s1_range`), then averaged down to ``size`` on
    the long side.

    Returns ``gray`` ({variable: uint8 array}), ``alpha`` (uint8, valid in
    every polarisation), ``stretch`` ({variable: ``p_low``, ``p_high``,
    ``unit``, ...}), ``composite``, ``width``, ``height``, ``geotransform``,
    ``crs`` (WKT), ``warp_transform`` and ``rows_read``.

    ``composite`` is set when the variables hold a dual-polarisation pair
    (VV+VH or HH+HV) and the stretch is in dB: ``rgb`` (3 x rows x cols) with
    red the co-polarisation, green the cross-polarisation and blue their
    ratio in dB, ``bands`` and ``ratio_stretch``; ``None`` otherwise. It is
    built before the averaging down, as from the quicklook products.
    Raises :class:`pysent.errors.EmptySceneError` when a polarisation has no
    valid pixel.
    """
    manifest_path = _resolve_safe_manifest_path(input_dataset)
    safe, measurements = _s1_measurements(manifest_path)
    if polarizations is None:
        variables = list(measurements)
    else:
        variables = [str(variable) for variable in polarizations]
        missing = [variable for variable in variables if variable not in measurements]
        if missing:
            raise RuntimeError(
                f"this product has no {', '.join(missing)}; it has {', '.join(measurements) or 'no measurement'}"
            )
    if not variables:
        raise RuntimeError(f"no Sentinel-1 measurement found in {input_dataset}")
    width, height = safe.RasterXSize, safe.RasterYSize
    gcps = safe.GetGCPs()
    gcp_projection = safe.GetGCPProjection()
    safe = None

    # Row i of the coarse grid is the middle row of source rows [i*step, (i+1)*step);
    # column j is the mean of source columns [j*col_step, (j+1)*col_step).
    step = max(1, height // (_S1_ROW_OVERSAMPLE * size))
    col_step = max(1, width // (_OVERSAMPLE * size))
    rows = np.arange(height // step) * step + step // 2
    method = str(stretch_method or S1_STRETCH_METHOD).strip().lower()
    if method not in {"db", "linear"}:
        raise ValueError(f"stretch_method must be 'db' or 'linear', not {stretch_method!r}")
    coarse, stretches = {}, {}
    # Both polarisations in flight together: the reads are round trips, not CPU.
    with ThreadPoolExecutor(max_workers=len(variables)) as pool:
        lines = dict(zip(variables, pool.map(
            lambda variable: _read_rows(measurements[variable], rows, max(1, threads // len(variables))),
            variables,
        )))
    samples = {}
    for variable in variables:
        sampled = lines.pop(variable)
        samples[variable] = _block_mean(sampled, _S1_STATS_COLUMNS)
        stretches[variable] = _s1_range(samples[variable], stretch_percentiles, method, variable)
        coarse[variable] = _block_mean(sampled, col_step)
    grid_rows, grid_cols = coarse[variables[0]].shape

    memory = gdal.GetDriverByName("MEM").Create("", grid_cols, grid_rows, len(variables), gdal.GDT_Float32)
    memory.SetGCPs(
        [gdal.GCP(gcp.GCPX, gcp.GCPY, gcp.GCPZ, gcp.GCPPixel / col_step, gcp.GCPLine / step) for gcp in gcps],
        gcp_projection,
    )
    for index, variable in enumerate(variables, start=1):
        band = memory.GetRasterBand(index)
        band.SetNoDataValue(0)
        band.WriteArray(coarse[variable])
    with gdal_errors():
        # The output grid's extent and pixel size, chosen by GDAL from the GCPs.
        probe = gdal.Warp("", memory, format="VRT", dstSRS=target_epsg, tps=use_tps)
    out_width, out_height = _fit(probe.RasterXSize, probe.RasterYSize, _OVERSAMPLE * size)
    probe = None
    with gdal_errors():
        warped = gdal.Warp(
            "",
            memory,
            format="MEM",
            dstSRS=target_epsg,
            width=out_width,
            height=out_height,
            tps=use_tps,
            srcNodata=0,
            dstNodata=0,
            resampleAlg="average",
            outputType=gdal.GDT_Float32,
        )
    memory = None
    geotransform = warped.GetGeoTransform()
    crs = warped.GetProjectionRef()
    amplitudes = {variable: warped.GetRasterBand(index).ReadAsArray() for index, variable in enumerate(variables, start=1)}
    warped = None

    grays = {}
    valid = np.ones((out_height, out_width), dtype=bool)
    for variable in variables:
        gray, inside = _s1_stretch(amplitudes[variable], stretches[variable])
        if not inside.any():
            raise EmptySceneError(f"Sentinel-1 {variable} has no valid pixels")
        grays[variable] = gray
        valid &= inside
    alpha = np.where(valid, 255, 0).astype(np.uint8)
    pair = _s1_dual_pair(variables) if method == "db" else None
    composite = None
    if pair is not None:
        composite = _s1_composite(grays, valid, samples, stretches, pair, stretch_percentiles)
    width, height = _fit(out_width, out_height, size)
    small = _shrink([*(grays[variable] for variable in variables), alpha], width, height)
    if composite is not None:
        composite["rgb"] = np.stack(_shrink(composite["rgb"], width, height))
    return {
        "variables": variables,
        "gray": dict(zip(variables, small[:-1])),
        "alpha": small[-1],
        "composite": composite,
        "stretch": stretches,
        "width": width,
        "height": height,
        "geotransform": _scaled_geotransform(geotransform, out_width, out_height, width, height),
        "crs": crs,
        "warp_transform": "thin-plate-spline" if use_tps else "polynomial-gcp",
        "rows_read": int(rows.size),
    }


_S1_DUAL_PAIRS = (("Amplitude_VV", "Amplitude_VH"), ("Amplitude_HH", "Amplitude_HV"))


def _s1_dual_pair(variables: Sequence[str]) -> tuple[str, str] | None:
    for co, cross in _S1_DUAL_PAIRS:
        if co in variables and cross in variables:
            return co, cross
    return None


def _decoded_db(gray: np.ndarray, stretch: dict[str, Any]) -> np.ndarray:
    """A stretched byte back in dB: the clipped value it stands for."""
    return stretch["p_low"] + gray.astype(np.float32) / 255.0 * (stretch["p_high"] - stretch["p_low"])


def _s1_composite(
    grays: dict[str, np.ndarray],
    valid: np.ndarray,
    samples: dict[str, np.ndarray],
    stretches: dict[str, dict[str, Any]],
    pair: tuple[str, str],
    percentiles: tuple[float, float],
) -> dict[str, Any]:
    """Red the co-polarisation, green the cross-polarisation, blue their ratio in dB.

    The ratio is taken from the two stretched products (each clipped to its
    own range), as a composite drawn from the quicklook GeoTIFFs takes it, and
    stretched to its own ``percentiles``. Those come from the same 32-sample
    means the two ranges come from: taken on the thumbnail grid, where every
    cell is already a mean of hundreds, they were narrow enough to move the
    blue channel by 29 levels on average on a lake-rich scene (6 with these).
    """
    co, cross = pair
    both = (samples[co] > 0) & (samples[cross] > 0)
    if not both.any():
        raise EmptySceneError(f"Sentinel-1 {co} and {cross} have no valid pixel in common")

    def clipped_db(values: np.ndarray, stretch: dict[str, Any]) -> np.ndarray:
        return np.clip(20.0 * np.log10(values), stretch["p_low"], stretch["p_high"])

    sample_ratio = clipped_db(samples[co][both], stretches[co]) - clipped_db(samples[cross][both], stretches[cross])
    low, high = (float(value) for value in np.percentile(sample_ratio, list(percentiles)))
    if high <= low:
        high = low + 1.0
    ratio = _decoded_db(grays[co], stretches[co]) - _decoded_db(grays[cross], stretches[cross])
    blue = np.zeros(valid.shape, dtype=np.uint8)
    blue[valid] = np.round(np.clip((ratio[valid] - low) / (high - low), 0.0, 1.0) * 255.0).astype(np.uint8)
    return {
        "bands": [co, cross, f"{co}/{cross.removeprefix('Amplitude_')}"],
        "rgb": [np.where(valid, grays[co], 0), np.where(valid, grays[cross], 0), blue],
        "ratio_stretch": {"p_low": low, "p_high": high, "unit": "dB", "percentiles": [float(v) for v in percentiles]},
    }


def write_thumbnail_png(bands: Sequence[np.ndarray], alpha: np.ndarray | None, output_path: str | Path) -> Path:
    """Write byte bands (one gray or three RGB) plus an optional alpha as a PNG, atomically."""
    arrays = [np.asarray(band, dtype=np.uint8) for band in bands]
    if alpha is not None:
        arrays.append(np.asarray(alpha, dtype=np.uint8))
    rows, cols = arrays[0].shape
    memory = gdal.GetDriverByName("MEM").Create("", cols, rows, len(arrays), gdal.GDT_Byte)
    for index, array in enumerate(arrays, start=1):
        memory.GetRasterBand(index).WriteArray(array)
    if alpha is not None:
        memory.GetRasterBand(len(arrays)).SetColorInterpretation(gdal.GCI_AlphaBand)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_output(path) as partial, gdal_errors():
        gdal.GetDriverByName("PNG").CreateCopy(str(partial), memory)
    memory = None
    for leftover in path.parent.glob(f"{path.name}.aux.xml"):
        leftover.unlink(missing_ok=True)
    return path

