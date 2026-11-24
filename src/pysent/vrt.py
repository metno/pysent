"""Sentinel-1/2 products as GDAL VRTs over the SAFE zip: no pixels copied.

The GeoTIFF functions in :mod:`pysent.s1` and :mod:`pysent.s2` decode a scene,
stretch it and write every pixel again. The builders here produce the same
picture as a VRT: a few kilobytes of XML whose sources are the files inside the
SAFE zip (opened through ``/vsizip/``, nothing unpacked), with pysent's stretch
written into it as fixed numbers. A reader that understands GDAL - a map
server, QGIS, ``gdal_translate`` - decodes and stretches on every read.

The stretch needs scene-wide statistics (Sentinel-2: per-band percentiles,
Sentinel-1: dB percentiles), and a VRT cannot compute them, so they are computed
once here, exactly as the GeoTIFF path computes them, and baked in:

``build_sentinel_s2_vrt``
    The product's three bands on the tile's own grid (no warp: the tile is
    already in UTM; 20 m bands are read at 10 m by nearest neighbour, as the
    GeoTIFF path does). Each band's percentile/gamma curve becomes a ``<LUT>``
    into ``[1, 255]``, 0 staying NoData. Zoomed out, GDAL reads the JPEG 2000
    resolution levels, so no overviews are needed. Writes only the VRT. An NBS
    netCDF works as the source too, its half-pixel coordinate shift undone.

``build_sentinel_s2_all_bands_vrt``
    Every spectral band of the scene in one VRT, each with its own stretch
    LUT, on the same grid. A band combination is then only a choice of three
    bands (a map server's ``BANDS=``): the named ones are
    :data:`pysent.s2.S2_BAND_PRESETS`, and :func:`sentinel_s2_vrt_band_indices`
    turns a preset or a free triplet into band numbers, refusing a band the
    product does not have. The three bands of a preset read the same pixels as
    that preset's own :func:`build_sentinel_s2_vrt`.

``build_sentinel_s1_vrt``
    A warped VRT over the SAFE measurement band (the GCP warp of the GeoTIFF
    path, kept inline), stretched to gray by a ``<LUT>`` on the amplitude. A
    SAFE measurement TIFF has no overviews and one-line strips, so a zoomed-out
    read would decode the whole scene: the builder warps the scene once and
    writes a sidecar, the coarse levels (x4 and up by default) as the VRT's
    external overview file ``<name>.vrt.ovr`` and the full-resolution alpha as
    ``<name>.vrt.alpha.tif``. Finer than the first overview, a read warps the
    gray band from the SAFE at request time.

Neither builder knows anything about a map server: the caller decides where
the VRT lives and how it is served.
"""
from __future__ import annotations

import math
import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import numpy as np
from osgeo import gdal

from ._runtime import atomic_output, gdal_errors, provenance
from .errors import EmptySceneError

VRT_OUTPUT = "vrt"
# The first overview level written beside an S1 VRT, and the coarser ones. x4 is
# about 7 % of the full-resolution pixels; finer levels are warped on request.
S1_VRT_OVERVIEW_FACTORS: tuple[int, ...] = (4, 8, 16)
S1_VRT_OVERVIEW_COMPRESSION = "DEFLATE"
# Threads of the warp a fine-zoom read runs; one measured faster than six on a
# request-sized window (2.3 s against 2.8 s for a 2048 px window to 512 px).
S1_VRT_SERVE_THREADS = "1"


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _step_lut(values: np.ndarray, outputs: np.ndarray) -> str:
    """A VRT ``<LUT>`` that reproduces a step function exactly at integer inputs.

    GDAL interpolates linearly between LUT entries, so each step is written as
    two entries, the last input of the old level and the first of the new one;
    no integer lies between them. Inputs below the first entry take its output,
    inputs above the last take the last output.
    """
    changes = np.flatnonzero(np.diff(outputs)) + 1
    keep = np.unique(np.concatenate(([0], changes - 1, changes, [len(values) - 1])))
    return ",".join(_lut_pair(values[index], outputs[index]) for index in keep)


def _lut_pair(value: float, output: int) -> str:
    return f"{_number(value)}:{int(output)}"


def _number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _sub(parent: ET.Element, tag: str, text: object | None = None, **attrs: object) -> ET.Element:
    element = ET.SubElement(parent, tag, {key: str(value) for key, value in attrs.items()})
    if text is not None:
        element.text = str(text)
    return element


def _dataset_element(width: int, height: int, srs_wkt: str, geotransform: Sequence[float]) -> ET.Element:
    root = ET.Element("VRTDataset", rasterXSize=str(width), rasterYSize=str(height))
    _sub(root, "SRS", srs_wkt, dataAxisToSRSAxisMapping="1,2")
    _sub(root, "GeoTransform", ", ".join(repr(float(value)) for value in geotransform))
    return root


def _add_metadata(root: ET.Element, stamp: dict[str, str]) -> None:
    metadata = _sub(root, "Metadata")
    for key, value in stamp.items():
        _sub(metadata, "MDI", value, key=key)


def _write_vrt(root: ET.Element, output_path: Path) -> None:
    """Write the VRT under its final name only once complete."""
    ET.indent(root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_output(output_path) as partial:
        partial.write_text(ET.tostring(root, encoding="unicode") + "\n", encoding="utf-8")
        with gdal_errors():
            check = gdal.Open(str(partial))  # refuse to publish XML GDAL cannot open
        if check is None:
            raise RuntimeError(f"GDAL cannot open the VRT written for {output_path.name}")
        check = None


def _output_path(output_path: str | Path) -> Path:
    path = Path(output_path)
    if path.suffix.lower() != ".vrt":
        raise ValueError(f"output_path must end in .vrt, not {path.name!r}")
    return path


# --------------------------------------------------------------------------- #
# Sentinel-2
# --------------------------------------------------------------------------- #
def _s2_band_file(subdataset_name: str, band_name: str) -> str:
    """The JPEG 2000 file of one band inside the SAFE, as GDAL's SENTINEL2 driver lists it."""
    with gdal_errors():
        dataset = gdal.Open(subdataset_name, gdal.GA_ReadOnly)
    if dataset is None:
        raise RuntimeError(f"Unable to open Sentinel-2 subdataset {subdataset_name}")
    code = band_name if len(band_name) == 3 else f"B{int(band_name[1:]):02d}"
    pattern = re.compile(rf"_{code}(_\d+m)?\.jp2$", re.IGNORECASE)
    matches = [name for name in (dataset.GetFileList() or []) if pattern.search(name)]
    if not matches:
        raise RuntimeError(f"No JPEG 2000 file for {band_name} in {subdataset_name}")
    return matches[0]


def _s2_band_entry(
    name: str,
    file_name: str,
    geotransform: Sequence[float] | None = None,
    *,
    data_types: tuple[int, ...] = (gdal.GDT_UInt16,),
) -> dict[str, Any]:
    with gdal_errors():
        dataset = gdal.Open(file_name, gdal.GA_ReadOnly)
    band = dataset.GetRasterBand(1)
    if band.DataType not in data_types:
        expected = " or ".join(gdal.GetDataTypeName(value) for value in data_types)
        raise RuntimeError(f"Sentinel-2 band {name} is {gdal.GetDataTypeName(band.DataType)}, not {expected}")
    return {
        "name": name,
        "file": file_name,
        "width": dataset.RasterXSize,
        "height": dataset.RasterYSize,
        "block": band.GetBlockSize(),
        "data_type": gdal.GetDataTypeName(band.DataType),
        "geotransform": tuple(geotransform or dataset.GetGeoTransform()),
        "srs": dataset.GetProjectionRef(),
    }


def _is_netcdf(input_dataset: str) -> bool:
    text = str(input_dataset or "").strip()
    return text.lower().endswith(".nc") or text.upper().startswith("NETCDF:")


def _epsg_of(wkt: str) -> str | None:
    from osgeo import osr

    srs = osr.SpatialReference()
    if not wkt or srs.ImportFromWkt(wkt) != 0:
        return None
    srs.AutoIdentifyEPSG()
    code = srs.GetAuthorityCode(None)
    return f"EPSG:{code}" if code else None


def _s2_netcdf_band(input_dataset: str, band_name: str) -> dict[str, Any]:
    """One band of an NBS Sentinel-2 netCDF, on the tile's true grid.

    The NBS netCDF holds the SAFE's pixels unchanged (the 20 m and 60 m bands
    repeated to 10 m), but its ``x``/``y`` coordinates are the tile's corner
    values rather than pixel centres. A CF reader, GDAL included, therefore
    places the image half a pixel north-west of the tile. Sentinel-2 tile
    corners lie on the 60 m grid, so an origin half a pixel off the pixel grid
    is that shift, and it is undone here; any other origin is used as read.
    """
    text = str(input_dataset).strip()
    path = text.split(":", 1)[1].rsplit(":", 1)[0].strip('"') if text.upper().startswith("NETCDF:") else text
    file_name = f'NETCDF:"{path}":{band_name}'
    try:
        with gdal_errors():
            dataset = gdal.Open(file_name, gdal.GA_ReadOnly)
    except RuntimeError as exc:
        # On a read-only mount HDF5's file locking fails and GDAL reports a missing file.
        raise RuntimeError(
            f"Unable to open {file_name} ({exc}); on a read-only file system set HDF5_USE_FILE_LOCKING=FALSE"
        ) from exc
    x0, dx, _, y0, _, dy = dataset.GetGeoTransform()
    dataset = None
    half = (dx / 2.0, dy / 2.0)
    if math.isclose(math.remainder(x0, dx), half[0], abs_tol=1e-6) or math.isclose(
        math.remainder(x0, dx), -half[0], abs_tol=1e-6
    ):
        x0, y0 = x0 + half[0], y0 + half[1]
    return _s2_band_entry(band_name, file_name, (x0, dx, 0.0, y0, 0.0, dy))


def _s2_resolve_options(processing_options: dict[str, Any] | None) -> tuple[tuple[float, float], str, float]:
    from .s2 import _resolve_stretch_options

    processing = dict(processing_options or {})
    if processing.get("histogram_stretch") is False:
        raise ValueError("a Sentinel-2 VRT is always stretched to 8 bit; histogram_stretch=False is not supported")
    if processing.get("preset") not in (None, "", "full"):
        raise ValueError("a Sentinel-2 VRT is on the tile's own grid; presets do not apply")
    return _resolve_stretch_options(processing)


def _s2_mask_lut(classes: Sequence[int]) -> str:
    """SCL class to 0 (masked) or 1 (kept); a ``mul`` pixel function applies it."""
    values = np.arange(256)
    return _step_lut(values, np.where(np.isin(values, list(classes)), 0, 1))


def _s2_band_element(
    root: ET.Element,
    position: int,
    data_type: str,
    mask: dict[str, Any] | None,
) -> ET.Element:
    """A plain band, or with a mask the product of the band and the mask's 0/1 lookup.

    ``mul`` is one of GDAL's built-in pixel functions: no Python runs on read.
    """
    if mask is None:
        return _sub(root, "VRTRasterBand", dataType=data_type, band=position)
    element = _sub(root, "VRTRasterBand", dataType=data_type, band=position, subClass="VRTDerivedRasterBand")
    _sub(element, "PixelFunctionType", "mul")
    _sub(element, "SourceTransferType", data_type)
    return element


def _s2_add_mask_source(element: ET.Element, mask: dict[str, Any], width: int, height: int,
                        classes: Sequence[int], *, full_resolution: bool) -> None:
    source = _sub(element, "ComplexSource")
    _sub(source, "SourceFilename", mask["file"], relativeToVRT="0")
    if full_resolution:
        options = _sub(source, "OpenOptions")
        _sub(options, "OOI", "NONE", key="OVERVIEW_LEVEL")
    _sub(source, "SourceBand", 1)
    if not full_resolution:
        _sub(source, "SourceProperties", RasterXSize=mask["width"], RasterYSize=mask["height"],
             DataType=mask["data_type"], BlockXSize=mask["block"][0], BlockYSize=mask["block"][1])
    _sub(source, "SrcRect", xOff=0, yOff=0, xSize=mask["width"], ySize=mask["height"])
    _sub(source, "DstRect", xOff=0, yOff=0, xSize=width, ySize=height)
    _sub(source, "LUT", _s2_mask_lut(classes))


def _s2_stats_vrt(
    bands: list[dict[str, Any]],
    width: int,
    height: int,
    srs: str,
    geotransform,
    mask: dict[str, Any] | None = None,
    classes: Sequence[int] = (),
) -> str:
    """An in-memory UInt16 stack of the bands, read at full resolution only.

    The GeoTIFF path samples the warped full-resolution stack; opening the JPEG
    2000 files without their resolution levels makes the same decimated read
    pick the same pixels, so the percentiles come out identical. With a cloud
    mask the masked pixels read 0 (NoData), as they do in the GeoTIFF path's
    masked stack, and leave the percentiles.
    """
    root = _dataset_element(width, height, srs, geotransform)
    for position, band in enumerate(bands, start=1):
        element = _s2_band_element(root, position, "UInt16", mask)
        _sub(element, "NoDataValue", 0)
        source = _sub(element, "ComplexSource")
        _sub(source, "SourceFilename", band["file"], relativeToVRT="0")
        options = _sub(source, "OpenOptions")
        _sub(options, "OOI", "NONE", key="OVERVIEW_LEVEL")
        _sub(source, "SourceBand", 1)
        _sub(source, "SrcRect", xOff=0, yOff=0, xSize=band["width"], ySize=band["height"])
        _sub(source, "DstRect", xOff=0, yOff=0, xSize=width, ySize=height)
        _sub(source, "NODATA", 0)
        if mask is not None:
            _s2_add_mask_source(element, mask, width, height, classes, full_resolution=True)
    path = f"/vsimem/pysent_s2_stats_{uuid.uuid4().hex}.vrt"
    gdal.FileFromMemBuffer(path, ET.tostring(root, encoding="unicode"))
    return path


def _s2_netcdf_band_names(input_dataset: str) -> list[str]:
    """The spectral bands an NBS Sentinel-2 netCDF holds, in band order."""
    from .s2 import S2_SUPPORTED_BANDS

    text = str(input_dataset).strip()
    path = text.split(":", 1)[1].rsplit(":", 1)[0].strip('"') if text.upper().startswith("NETCDF:") else text
    try:
        with gdal_errors():
            dataset = gdal.Open(path, gdal.GA_ReadOnly)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Unable to open {path} ({exc}); on a read-only file system set HDF5_USE_FILE_LOCKING=FALSE"
        ) from exc
    variables = {
        str(value).rsplit(":", 1)[-1]
        for key, value in (dataset.GetMetadata("SUBDATASETS") or {}).items()
        if key.endswith("_NAME")
    }
    return [band for band in S2_SUPPORTED_BANDS if band in variables]


def _s2_scene_bands(
    input_dataset: str,
    band_names: Sequence[str] | None,
    classes: Sequence[int],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None, float]:
    """The band files a VRT reads (``band_names``, or every spectral band the product has), and the SCL file if masked."""
    from .s2 import (
        S2_SCL_BAND,
        S2_SUPPORTED_BANDS,
        _collect_sentinel_s2_band_sources,
        _require_scl,
        _select_band_sources,
    )

    mask = None
    if _is_netcdf(input_dataset):
        if classes:
            raise ValueError("cloud_mask needs the SAFE product's scene classification (SCL); a netCDF source has none here")
        names = list(band_names) if band_names is not None else _s2_netcdf_band_names(input_dataset)
        bands = [_s2_netcdf_band(input_dataset, band) for band in names]
        target_epsg = _epsg_of(bands[0]["srs"]) if bands else None
    else:
        sources = _collect_sentinel_s2_band_sources(input_dataset)
        if classes:
            _require_scl(sources)
        names = list(band_names) if band_names is not None else [band for band in S2_SUPPORTED_BANDS if band in sources]
        selected, target_epsg, _ = _select_band_sources(sources, names)
        bands = [
            _s2_band_entry(str(source["band_name"]), _s2_band_file(str(source["subdataset_name"]), str(source["band_name"])))
            for source in selected
        ]
        if classes:
            scl = sources[S2_SCL_BAND]
            mask = _s2_band_entry(
                S2_SCL_BAND, _s2_band_file(str(scl["subdataset_name"]), S2_SCL_BAND),
                data_types=(gdal.GDT_Byte, gdal.GDT_UInt16),
            )
    if not bands:
        raise RuntimeError(f"No Sentinel-2 spectral band found in {input_dataset}")
    resolution = min(band["geotransform"][1] for band in bands)
    return bands, mask, target_epsg, float(resolution)


def _s2_grid(bands: list[dict[str, Any]], mask: dict[str, Any] | None) -> tuple[int, int, tuple[float, ...], str]:
    """The finest band's grid; every band (and the mask) must cover the same square."""
    finest = min(bands, key=lambda band: band["geotransform"][1])
    width, height = finest["width"], finest["height"]
    geotransform, srs = finest["geotransform"], finest["srs"]
    for band in bands + ([mask] if mask is not None else []):
        # Every band of a tile covers the same square; a mismatch means the
        # nearest-neighbour upsampling would shift it.
        span = (band["width"] * band["geotransform"][1], band["height"] * band["geotransform"][5])
        if band["geotransform"][0] != geotransform[0] or band["geotransform"][3] != geotransform[3] or \
                span != (width * geotransform[1], height * geotransform[5]):
            raise RuntimeError(f"Sentinel-2 band {band['name']} is not on the tile's grid")
    return width, height, geotransform, srs


def _s2_ranges(
    bands: list[dict[str, Any]],
    grid: tuple[int, int, tuple[float, ...], str],
    mask: dict[str, Any] | None,
    classes: Sequence[int],
    *,
    method: str,
    percentiles: tuple[float, float],
    workers: int = 1,
) -> list[tuple[float, float]]:
    """Each band's stretch range, from its own pixels only: the same band gives the same range in any VRT.

    With ``workers`` > 1 the bands are read in parallel threads (GDAL decodes
    without the GIL), each through its own handle on the in-memory stack.
    """
    from concurrent.futures import ThreadPoolExecutor

    from .s2 import _stretch_range

    width, height, geotransform, srs = grid
    stats_path = _s2_stats_vrt(bands, width, height, srs, geotransform, mask, classes)

    def one(index: int) -> tuple[float, float]:
        with gdal_errors():
            dataset = gdal.Open(stats_path)
        low, high = _stretch_range(
            dataset.GetRasterBand(index + 1), f"Sentinel-2 band {bands[index]['name']}",
            method=method, percentiles=percentiles,
        )
        return low, high if high > low else low + 1.0

    try:
        if workers > 1 and len(bands) > 1:
            with ThreadPoolExecutor(max_workers=min(workers, len(bands))) as pool:
                return list(pool.map(one, range(len(bands))))
        return [one(index) for index in range(len(bands))]
    finally:
        gdal.Unlink(stats_path)


def _s2_stamp(method: str, percentiles, gamma: float, target_epsg, resolution: float, classes) -> dict[str, str]:
    stamp = provenance(
        stretch_method=method,
        stretch_percentiles=tuple(percentiles) if method == "percentile" else None,
        stretch_gamma=gamma,
        target_epsg=target_epsg,
        target_resolution=float(resolution),
        preset="full",
        output=VRT_OUTPUT,
        cloud_mask=classes or None,
    )
    stamp.pop("TIFFTAG_SOFTWARE", None)  # a TIFF tag means nothing in a VRT
    return stamp


def _s2_add_stretched_band(
    root: ET.Element,
    position: int,
    band: dict[str, Any],
    stretch_range: tuple[float, float],
    gamma: float,
    color: str,
    grid: tuple[int, int, tuple[float, ...], str],
    mask: dict[str, Any] | None,
    classes: Sequence[int],
) -> None:
    """One Byte band: the band's file through its stretch LUT, nearest-upsampled to the grid."""
    from .s2 import _stretch_lut

    width, height = grid[0], grid[1]
    low, high = stretch_range
    lut = _stretch_lut(low, high, gamma, 65536, 0)
    element = _s2_band_element(root, position, "Byte", mask)
    _sub(element, "Description", band["name"])
    _sub(element, "NoDataValue", 0)
    _sub(element, "ColorInterp", color)
    source = _sub(element, "ComplexSource")
    _sub(source, "SourceFilename", band["file"], relativeToVRT="0")
    _sub(source, "SourceBand", 1)
    _sub(source, "SourceProperties", RasterXSize=band["width"], RasterYSize=band["height"],
         DataType="UInt16", BlockXSize=band["block"][0], BlockYSize=band["block"][1])
    _sub(source, "SrcRect", xOff=0, yOff=0, xSize=band["width"], ySize=band["height"])
    _sub(source, "DstRect", xOff=0, yOff=0, xSize=width, ySize=height)
    _sub(source, "NODATA", 0)
    _sub(source, "LUT", _step_lut(np.arange(1, 65536), lut[1:]))
    if mask is not None:
        _s2_add_mask_source(element, mask, width, height, classes, full_resolution=False)


def _s2_stretch_report(ranges, method: str, percentiles, gamma: float) -> dict[str, Any]:
    return {
        "p_low": [low for low, _ in ranges],
        "p_high": [high for _, high in ranges],
        "method": method,
        **({"percentiles": [float(value) for value in percentiles]} if method == "percentile" else {}),
        "gamma": gamma,
    }


def build_sentinel_s2_vrt(
    *,
    input_dataset: str,
    product: str | Sequence[str],
    output_path: str | Path,
    processing_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one Sentinel-2 RGB product as a VRT over the SAFE zip.

    ``product`` is a name from :data:`pysent.s2.S2_DEFAULT_PRODUCTS` or
    :data:`pysent.s2.S2_BAND_PRESETS`, or three band names. The picture is the one
    :func:`pysent.s2.process_sentinel_s2_safe` writes with
    ``histogram_stretch=True``: the same percentiles (``stretch_percentiles``,
    default 0.5-99.5, or ``stretch_method="minmax"``), the same gamma
    (``stretch_gamma``, default 0.7), valid pixels in ``[1, 255]`` and 0 for
    NoData, on the tile's own grid at the finest band's resolution.

    ``cloud_mask`` (Level-2A, off by default) is the option of
    :func:`pysent.s2.process_sentinel_s2_safe`: the pixels whose scene
    classification is one of the classes read 0, through GDAL's built-in
    ``mul`` pixel function over the SCL file, and the percentiles are taken
    without them.

    Only ``output_path`` (ending in ``.vrt``) is written. Returns ``path``,
    ``product_name``, ``bands``, ``target_epsg``, ``resolution``, ``width``,
    ``height``, ``sources`` (the files the VRT reads), ``stretch`` (the
    numbers baked in), ``cloud_mask`` (the classes and the SCL file, or
    ``None``), ``radiometry`` (:func:`pysent.s2.read_sentinel_s2_radiometry`,
    ``None`` for a netCDF or unreadable metadata) and ``provenance`` (the
    ``PYSENT_*`` tags, also in the VRT's metadata). Raises :class:`pysent.errors.EmptySceneError` if a band
    has no valid pixel.
    """
    from .s2 import (
        S2_BAND_PRESETS,
        S2_DEFAULT_PRODUCTS,
        _radiometry_or_none,
        normalize_sentinel_s2_product_map,
        resolve_sentinel_s2_cloud_mask,
    )

    final_path = _output_path(output_path)
    if isinstance(product, str):
        band_names = S2_DEFAULT_PRODUCTS.get(product) or S2_BAND_PRESETS.get(product)
        if band_names is None:
            raise ValueError(f"unknown Sentinel-2 product {product!r}; pass three band names instead")
        product_name = product
    else:
        product_name = "_".join(str(band).lower() for band in product)
        band_names = normalize_sentinel_s2_product_map({product_name: list(product)})[product_name]
    percentiles, method, gamma = _s2_resolve_options(processing_options)
    classes = resolve_sentinel_s2_cloud_mask((processing_options or {}).get("cloud_mask"))

    bands, mask, target_epsg, resolution = _s2_scene_bands(input_dataset, band_names, classes)
    grid = _s2_grid(bands, mask)
    ranges = _s2_ranges(bands, grid, mask, classes, method=method, percentiles=percentiles)
    stamp = _s2_stamp(method, percentiles, gamma, target_epsg, resolution, classes)

    root = _dataset_element(grid[0], grid[1], grid[3], grid[2])
    _add_metadata(root, stamp)
    for position, (band, stretch_range, color) in enumerate(zip(bands, ranges, ("Red", "Green", "Blue")), start=1):
        _s2_add_stretched_band(root, position, band, stretch_range, gamma, color, grid, mask, classes)
    _write_vrt(root, final_path)

    return {
        "product_name": product_name,
        "bands": list(band_names),
        "path": str(final_path),
        "output_file": final_path.name,
        "target_epsg": target_epsg,
        "resolution": resolution,
        "width": grid[0],
        "height": grid[1],
        "sources": [band["file"] for band in bands],
        "stretch": _s2_stretch_report(ranges, method, percentiles, gamma),
        "cloud_mask": {"classes": list(classes), "source": mask["file"]} if mask is not None else None,
        "radiometry": None if _is_netcdf(input_dataset) else _radiometry_or_none(input_dataset),
        "provenance": stamp,
    }


def build_sentinel_s2_all_bands_vrt(
    *,
    input_dataset: str,
    output_path: str | Path,
    processing_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write every spectral band of a Sentinel-2 scene as one Byte VRT; combinations are band selections.

    One VRT per scene, whatever the number of combinations: band ``i`` is the
    product's ``i``-th spectral band in :data:`pysent.s2.S2_SUPPORTED_BANDS`
    order (13 for Level-1C, 12 for Level-2A, which has no ``B10``), each
    through its own scene stretch baked in as a LUT, all on the tile's grid at
    the finest resolution (10 m; coarser bands nearest-upsampled). A reader
    picks three bands (a map server's ``BANDS=``, ``gdal_translate -b``); the
    picture of a preset is the one :func:`build_sentinel_s2_vrt` writes for the
    same three bands, pixel for pixel: a band's stretch depends on that band
    alone. (A free triplet of only 20 m or 60 m bands is the exception: its own
    VRT is on that coarser grid, and its percentiles are sampled there.)

    ``processing_options`` take the stretch options and ``cloud_mask`` of
    :func:`build_sentinel_s2_vrt`, and ``workers`` (default: the CPUs, at
    most one per band) for the threads that read the scene statistics, the
    whole cost of the call.

    Only ``output_path`` (ending in ``.vrt``) is written. Returns ``path``,
    ``bands`` (the band names in VRT order), ``band_index`` (name to 1-based
    VRT band), ``presets`` (each :data:`pysent.s2.S2_BAND_PRESETS` entry the
    product can show, as 1-based VRT bands, red first), ``target_epsg``,
    ``resolution``, ``width``, ``height``, ``sources``, ``stretch`` (per band,
    in VRT order), ``cloud_mask``, ``radiometry`` and ``provenance``, as
    :func:`build_sentinel_s2_vrt`. Pick a combination with
    :func:`sentinel_s2_vrt_band_indices`.
    """
    from ._runtime import available_cpus
    from .s2 import S2_BAND_PRESETS, _radiometry_or_none, resolve_sentinel_s2_cloud_mask

    final_path = _output_path(output_path)
    processing = dict(processing_options or {})
    workers = processing.pop("workers", None)
    workers = max(1, int(workers)) if workers not in (None, "") else available_cpus()
    percentiles, method, gamma = _s2_resolve_options(processing)
    classes = resolve_sentinel_s2_cloud_mask(processing.get("cloud_mask"))

    bands, mask, target_epsg, resolution = _s2_scene_bands(input_dataset, None, classes)
    grid = _s2_grid(bands, mask)
    ranges = _s2_ranges(bands, grid, mask, classes, method=method, percentiles=percentiles, workers=workers)
    stamp = _s2_stamp(method, percentiles, gamma, target_epsg, resolution, classes)

    root = _dataset_element(grid[0], grid[1], grid[3], grid[2])
    _add_metadata(root, stamp)
    for position, (band, stretch_range) in enumerate(zip(bands, ranges), start=1):
        _s2_add_stretched_band(root, position, band, stretch_range, gamma, "Undefined", grid, mask, classes)
    _write_vrt(root, final_path)

    names = [band["name"] for band in bands]
    band_index = {name: position for position, name in enumerate(names, start=1)}
    return {
        "bands": names,
        "band_index": band_index,
        "presets": {
            preset: [band_index[band] for band in triplet]
            for preset, triplet in S2_BAND_PRESETS.items()
            if all(band in band_index for band in triplet)
        },
        "path": str(final_path),
        "output_file": final_path.name,
        "target_epsg": target_epsg,
        "resolution": resolution,
        "width": grid[0],
        "height": grid[1],
        "sources": [band["file"] for band in bands],
        "stretch": _s2_stretch_report(ranges, method, percentiles, gamma),
        "cloud_mask": {"classes": list(classes), "source": mask["file"]} if mask is not None else None,
        "radiometry": None if _is_netcdf(input_dataset) else _radiometry_or_none(input_dataset),
        "provenance": stamp,
    }


def sentinel_s2_vrt_band_indices(vrt: str | Path | Sequence[str], preset: str | Sequence[str]) -> list[int]:
    """The 1-based bands of an all-bands VRT that show ``preset``, red first.

    ``vrt`` is the VRT's path, or the ``bands`` list
    :func:`build_sentinel_s2_all_bands_vrt` returned; ``preset`` a name from
    :data:`pysent.s2.S2_BAND_PRESETS` or three band names, validated against
    the bands the VRT holds (:func:`pysent.s2.resolve_sentinel_s2_preset`).
    """
    from .s2 import resolve_sentinel_s2_preset

    if isinstance(vrt, (str, Path)):
        with gdal_errors():
            dataset = gdal.Open(str(vrt))
        names = [dataset.GetRasterBand(index).GetDescription() for index in range(1, dataset.RasterCount + 1)]
        dataset = None
    else:
        names = list(vrt)
    triplet = resolve_sentinel_s2_preset(preset, names)
    return [names.index(band) + 1 for band in triplet]


# --------------------------------------------------------------------------- #
# Sentinel-1
# --------------------------------------------------------------------------- #
def _s1_resolve_options(processing_options: dict[str, Any] | None) -> dict[str, Any]:
    from ._runtime import apply_preset
    from .s1 import (
        S1_PRESETS,
        S1_STRETCH_METHOD,
        S1_TARGET_EPSG,
        S1_TARGET_RESOLUTION,
        _resolve_stretch_percentiles,
    )

    processing = apply_preset(processing_options, S1_PRESETS)
    method = str(processing.get("stretch_method") or S1_STRETCH_METHOD).strip().lower()
    if method not in {"db", "linear"}:
        raise ValueError(f"stretch_method must be 'db' or 'linear', not {method!r}")
    speckle = str(processing.get("speckle_filter") or "none").strip().lower()
    if speckle not in {"none", "off", "false"}:
        raise ValueError("a speckle filter cannot be expressed in a VRT; speckle_filter must be 'none'")
    factors = processing.get("overview_factors")
    if factors is None:
        factors = S1_VRT_OVERVIEW_FACTORS
    factors = tuple(sorted({int(value) for value in factors if int(value) > 1}))
    if not factors:
        raise ValueError("overview_factors needs at least one factor above 1")
    return {
        "preset": str((processing_options or {}).get("preset") or "full").strip().lower(),
        "method": method,
        "percentiles": _resolve_stretch_percentiles(processing),
        "target_epsg": str(processing.get("target_epsg") or S1_TARGET_EPSG),
        "target_resolution": float(processing.get("target_resolution") or S1_TARGET_RESOLUTION),
        "resample_alg": str(processing.get("resample_alg") or "average"),
        "use_tps": bool(processing.get("use_tps", True)),
        "gdal_num_threads": str(processing.get("gdal_num_threads") or "ALL_CPUS"),
        "serve_num_threads": str(processing.get("serve_num_threads") or S1_VRT_SERVE_THREADS),
        "overview_factors": factors,
        "overview_compression": str(processing.get("overview_compression") or S1_VRT_OVERVIEW_COMPRESSION),
    }


def _s1_warped_vrt(manifest_path: str, band_index: int, settings: dict[str, Any], threads: str) -> gdal.Dataset:
    """The GCP warp of :func:`pysent.s1.process_sentinel_s1_safe`, as an in-memory warped VRT."""
    with gdal_errors():
        warped = gdal.Warp(
            "",
            manifest_path,
            options=gdal.WarpOptions(
                format="VRT",
                srcBands=[band_index],
                dstBands=[1],
                dstSRS=settings["target_epsg"],
                xRes=settings["target_resolution"],
                yRes=settings["target_resolution"],
                srcNodata=0,
                dstNodata=0,
                tps=settings["use_tps"],
                multithread=True,
                resampleAlg=settings["resample_alg"],
                outputType=gdal.GDT_Float32,
                warpOptions=[f"NUM_THREADS={threads}"],
            ),
        )
    if warped is None:
        raise RuntimeError(f"Unable to build the Sentinel-1 warped VRT for {manifest_path}")
    return warped


def _s1_lut(p_low: float, p_high: float, method: str) -> str:
    """Amplitude -> gray byte, the curve of :func:`pysent.s1.stretch_sentinel_s1_grayscale`.

    The gray level steps from ``k-1`` to ``k`` where the scaled value crosses
    ``k - 0.5``; in amplitude that point is ``10**(dB/20)`` for the dB stretch.
    Each step is two entries, the float32 just below the crossing and the
    crossing itself, so the linear interpolation GDAL does between entries
    cannot produce an in-between level.
    """
    levels = np.arange(1, 256, dtype=np.float64)
    crossings = p_low + (levels - 0.5) / 255.0 * (p_high - p_low)
    if method == "db":
        crossings = 10.0 ** (crossings / 20.0)
    pairs = []
    for level, crossing in zip(levels.astype(int), crossings):
        if crossing <= 0 or not math.isfinite(crossing):
            continue  # a linear stretch whose low end is below zero amplitude
        pairs.append(_lut_pair(float(np.nextafter(np.float32(crossing), np.float32(0))), level - 1))
        pairs.append(_lut_pair(float(np.float32(crossing)), level))
    return ",".join(pairs) if pairs else "0:0"


def _s1_overview_file(gray: np.ndarray, alpha: np.ndarray, overview_path: Path, settings: dict[str, Any]) -> None:
    """Write the VRT's external overview file: stretched gray + alpha at the overview factors.

    The levels are the ones :func:`pysent.s1.process_sentinel_s1_safe` puts in
    its GeoTIFF, pixel for pixel: GDAL builds each AVERAGE level from the one
    before it, so the chain starts at x2 as the GeoTIFF's does, in memory, and
    only the levels from the first requested factor on are copied out.
    """
    from .s1 import S1_OVERVIEW_FACTORS

    factors = settings["overview_factors"]
    chain = sorted({factor for factor in S1_OVERVIEW_FACTORS if factor < factors[0]} | set(factors))
    height, width = gray.shape
    scratch = f"/vsimem/pysent_s1_ovr_{uuid.uuid4().hex}.tif"
    options = ["INTERLEAVE=PIXEL", "ALPHA=YES", "PHOTOMETRIC=MINISBLACK", "TILED=YES", "BLOCKXSIZE=256", "BLOCKYSIZE=256"]
    try:
        with gdal_errors():
            mem = gdal.GetDriverByName("GTiff").Create(scratch, width, height, 2, gdal.GDT_Byte, options)
            mem.GetRasterBand(1).WriteArray(gray)
            mem.GetRasterBand(2).WriteArray(alpha)
            mem.BuildOverviews("AVERAGE", chain)
            mem = None
            first = gdal.OpenEx(scratch, open_options=[f"OVERVIEW_LEVEL={chain.index(factors[0])}"])
            compression = settings["overview_compression"].upper()
            creation = options + ["COPY_SRC_OVERVIEWS=YES", "BIGTIFF=IF_SAFER", f"COMPRESS={compression}"]
            if compression == "DEFLATE":
                creation.append("PREDICTOR=2")
            with atomic_output(overview_path) as partial:
                copied = gdal.GetDriverByName("GTiff").CreateCopy(str(partial), first, options=creation)
                if copied is None:
                    raise RuntimeError(f"Unable to write {overview_path.name}")
                copied = None
            first = None
    finally:
        if gdal.VSIStatL(scratch) is not None:
            gdal.Unlink(scratch)


def _s1_alpha_file(alpha: np.ndarray, alpha_path: Path, geotransform, srs: str) -> None:
    """Write the full-resolution alpha (0 / 255) beside the VRT.

    Reading alpha from the warped SAFE would warp the scene a second time on
    every request (GDAL reads a VRT band by band); a mask of a swath is a few
    hundred kB once compressed.
    """
    height, width = alpha.shape
    with atomic_output(alpha_path) as partial, gdal_errors():
        dataset = gdal.GetDriverByName("GTiff").Create(
            str(partial), width, height, 1, gdal.GDT_Byte,
            ["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512"],
        )
        dataset.SetGeoTransform(geotransform)
        dataset.SetProjection(srs)
        dataset.GetRasterBand(1).WriteArray(alpha)
        dataset = None


def build_sentinel_s1_vrt(
    *,
    input_dataset: str,
    polarisation: str,
    output_path: str | Path,
    processing_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one Sentinel-1 amplitude product as a VRT over the SAFE zip, plus its sidecar.

    ``polarisation`` is ``VV``, ``VH``, ``HH`` or ``HV`` (``Amplitude_VV``
    spellings work too). The picture is the one
    :func:`pysent.s1.process_sentinel_s1_safe` writes: the same GCP warp
    (``use_tps``, ``target_epsg``, ``target_resolution``, ``resample_alg``;
    defaults EPSG:32661 at 40 m, ``average``), the same stretch
    (``stretch_method`` ``db`` or ``linear``, ``stretch_percentiles`` default
    1-99, computed over the whole warped scene) and the same gray + alpha bands.
    No speckle filter (it cannot be expressed in a VRT).

    The scene is warped once here (``gdal_num_threads``) to compute the
    statistics and the sidecar, two files next to ``output_path``:

    ``<output_path>.ovr``
        GDAL's external overviews of the VRT, the stretched gray + alpha at
        ``overview_factors`` (default x4, x8, x16), ``overview_compression``
        (default DEFLATE). Coarser than the first factor, a read is a lookup.
    ``<output_path>.alpha.tif``
        The full-resolution alpha, so that only the gray band is warped from
        the SAFE on a fine-zoom read.

    Finer than the first overview, every read warps the SAFE with
    ``serve_num_threads`` threads (default 1: on a request-sized window one
    thread measured faster than six). Nothing else is written; the VRT names
    its sidecar relatively, so the three files move together.

    The reading process needs a GDAL block cache of about 1 GB
    (``GDAL_CACHEMAX``): a SAFE measurement is stored in one-line strips the
    full swath wide, and a fine-zoom read of a rotated swath touches thousands
    of them. With 256-512 MB a 2000 px window was measured at over 40 s, with
    1 GB at 2-3 s.

    Returns ``path``, ``sidecar_paths``, ``overview_factors``, ``variable``,
    ``target_epsg``, ``resolution``, ``width``, ``height``, ``sources``,
    ``stretch`` and ``provenance``. Raises
    :class:`pysent.errors.EmptySceneError` if the scene has no valid pixel.
    """
    from .s1 import (
        _resolve_safe_band_index,
        _resolve_safe_manifest_path,
        stretch_sentinel_s1_grayscale,
    )

    final_path = _output_path(output_path)
    polarisation = str(polarisation or "").strip().upper().removeprefix("AMPLITUDE_")
    if polarisation not in {"VV", "VH", "HH", "HV"}:
        raise ValueError(f"polarisation must be VV, VH, HH or HV, not {polarisation!r}")
    variable = f"Amplitude_{polarisation}"
    settings = _s1_resolve_options(processing_options)
    manifest_path = _resolve_safe_manifest_path(input_dataset)
    band_index = _resolve_safe_band_index(manifest_path, variable)

    warped = _s1_warped_vrt(manifest_path, band_index, settings, settings["gdal_num_threads"])
    width, height = warped.RasterXSize, warped.RasterYSize
    geotransform, srs = warped.GetGeoTransform(), warped.GetProjectionRef()
    with gdal_errors():
        amplitude = warped.GetRasterBand(1).ReadAsArray()
    warped = None
    served = _s1_warped_vrt(manifest_path, band_index, settings, settings["serve_num_threads"])
    if (served.RasterXSize, served.RasterYSize, served.GetGeoTransform()) != (width, height, geotransform):
        raise RuntimeError("the served warp is not on the grid the statistics were computed on")
    warped_xml = served.GetMetadata("xml:VRT")[0]
    served = None

    gray, alpha, stretch = stretch_sentinel_s1_grayscale(
        amplitude, nodata=0.0, percentiles=settings["percentiles"], method=settings["method"]
    )
    del amplitude
    if not alpha.any():
        raise EmptySceneError(f"Sentinel-1 {variable} has no valid pixels")

    stamp = provenance(
        warp_transform="thin-plate-spline" if settings["use_tps"] else "polynomial-gcp",
        stretch_method=settings["method"],
        stretch_percentiles=tuple(settings["percentiles"]),
        speckle_filter="none",
        target_epsg=settings["target_epsg"],
        target_resolution=settings["target_resolution"],
        preset=settings["preset"],
        output=VRT_OUTPUT,
        overview_factors=settings["overview_factors"],
    )
    stamp.pop("TIFFTAG_SOFTWARE", None)

    overview_path = final_path.with_name(final_path.name + ".ovr")
    alpha_path = final_path.with_name(final_path.name + ".alpha.tif")

    root = _dataset_element(width, height, srs, geotransform)
    _add_metadata(root, stamp)
    gray_band = _sub(root, "VRTRasterBand", dataType="Byte", band=1)
    _sub(gray_band, "ColorInterp", "Gray")
    source = _sub(gray_band, "ComplexSource")
    # The warped VRT is kept inline: one XML file instead of two.
    _sub(source, "SourceFilename", warped_xml, relativeToVRT="0")
    _sub(source, "SourceBand", 1)
    _sub(source, "SourceProperties", RasterXSize=width, RasterYSize=height, DataType="Float32",
         BlockXSize=min(512, width), BlockYSize=min(128, height))
    _sub(source, "SrcRect", xOff=0, yOff=0, xSize=width, ySize=height)
    _sub(source, "DstRect", xOff=0, yOff=0, xSize=width, ySize=height)
    _sub(source, "NODATA", 0)
    _sub(source, "LUT", _s1_lut(stretch["p_low"], stretch["p_high"], settings["method"]))
    alpha_band = _sub(root, "VRTRasterBand", dataType="Byte", band=2)
    _sub(alpha_band, "ColorInterp", "Alpha")
    source = _sub(alpha_band, "SimpleSource")
    _sub(source, "SourceFilename", alpha_path.name, relativeToVRT="1")
    _sub(source, "SourceBand", 1)
    _sub(source, "SourceProperties", RasterXSize=width, RasterYSize=height, DataType="Byte",
         BlockXSize=min(512, width), BlockYSize=min(512, height))
    _sub(source, "SrcRect", xOff=0, yOff=0, xSize=width, ySize=height)
    _sub(source, "DstRect", xOff=0, yOff=0, xSize=width, ySize=height)

    final_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _s1_alpha_file(alpha, alpha_path, geotransform, srs)
        _s1_overview_file(gray, alpha, overview_path, settings)
        _write_vrt(root, final_path)
    except BaseException:
        overview_path.unlink(missing_ok=True)
        alpha_path.unlink(missing_ok=True)
        raise

    return {
        "variable": variable,
        "path": str(final_path),
        "output_file": final_path.name,
        "sidecar_paths": [str(overview_path), str(alpha_path)],
        "overview_factors": list(settings["overview_factors"]),
        "target_epsg": settings["target_epsg"],
        "resolution": settings["target_resolution"],
        "width": width,
        "height": height,
        "sources": [manifest_path],
        "stretch": stretch,
        "provenance": stamp,
    }


__all__ = [
    "S1_VRT_OVERVIEW_FACTORS",
    "VRT_OUTPUT",
    "build_sentinel_s1_vrt",
    "build_sentinel_s2_vrt",
]
