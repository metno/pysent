"""Write a derived product: the float raster, a styled copy and a thumbnail.

Shared by every pipeline that produces a continuous geophysical quantity
rather than a stretched picture, so Sentinel-5P and Sentinel-3 cannot drift
apart in what they emit. The shape of the output is the owner's Q2 decision:
a Float32 product in physical units for the coverage service, a
full-resolution 8-bit paletted copy for the map service, and a 256 px
thumbnail for a catalogue page.

The styled copy is paletted rather than RGB: one band with a colour table is
1.7 times smaller than RGBA, keeps one value per pixel so a reader can recover
the class a pixel fell in, and carries transparency for no data.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from ._runtime import atomic_output

__all__ = [
    "COLOUR_ANCHORS",
    "STYLED_CEILING",
    "STYLED_FLOOR",
    "THUMBNAIL_PIXELS",
    "colour_table",
    "creation_options",
    "finish_float_product",
    "styled_bytes",
    "write_styled_copy",
    "write_thumbnail",
]


THUMBNAIL_PIXELS = 256
# Valid data is written into 1..255 so that 0 can mean "no data" and nothing
# else, the same convention the Sentinel-2 output uses.
STYLED_FLOOR = 1
STYLED_CEILING = 255


def creation_options(settings: dict[str, Any], *, float_data: bool) -> list[str]:
    options = ["TILED=YES", f"COMPRESS={settings['compression']}"]
    if settings.get("gdal_num_threads"):
        options.append(f"NUM_THREADS={settings['gdal_num_threads']}")
    if float_data and settings["compression"] in ("DEFLATE", "ZSTD", "LZW"):
        options.append(f"PREDICTOR={settings['predictor']}")
    return options


def styled_bytes(
    values: np.ndarray,
    value_range: tuple[float, float],
    *,
    log_scale: bool = False,
) -> np.ndarray:
    """Scale physical values into 1..255, with 0 reserved for no data.

    Values outside the range are clipped rather than dropped: a negative NO2
    column is instrument noise, and the plan's decision is to keep it in the
    data and clip it in the style.

    ``log_scale`` spaces the ramp by powers of ten instead of linearly, for a
    quantity that spans orders of magnitude. Chlorophyll runs from about 0.01
    to 100 mg m-3, so a linear ramp renders one bright estuary and an
    otherwise uniformly dark ocean. Values at or below zero have no logarithm
    and are clipped to the bottom of the ramp.
    """
    low, high = value_range
    with np.errstate(invalid="ignore", divide="ignore"):
        if log_scale:
            if low <= 0.0:
                raise ValueError(f"a log colour scale needs a positive lower bound, not {low}")
            scaled = (np.log10(np.maximum(values.astype(np.float64), low * 1e-3)) - np.log10(low)) / (
                np.log10(high) - np.log10(low)
            )
        else:
            scaled = (values.astype(np.float64) - low) / (high - low)
        scaled = np.clip(scaled, 0.0, 1.0)
        out = np.rint(scaled * (STYLED_CEILING - STYLED_FLOOR) + STYLED_FLOOR)
    return np.where(np.isfinite(values), out, 0).astype(np.uint8)


def colour_table(colormap: str):
    """A 256-entry GDAL colour table, 0 transparent and 1..255 the ramp.

    Written out rather than taken from a plotting library so the output needs no
    dependency beyond GDAL, and so the ramp is stable across versions.
    """
    from osgeo import gdal

    anchors = COLOUR_ANCHORS.get(colormap.strip().lower())
    if anchors is None:
        known = ", ".join(sorted(COLOUR_ANCHORS))
        raise ValueError(f"{colormap!r} is not a colormap pysent ships; it has: {known}")

    table = gdal.ColorTable()
    table.SetColorEntry(0, (0, 0, 0, 0))
    positions = np.linspace(0.0, 1.0, len(anchors))
    for index in range(STYLED_FLOOR, STYLED_CEILING + 1):
        fraction = (index - STYLED_FLOOR) / (STYLED_CEILING - STYLED_FLOOR)
        red = float(np.interp(fraction, positions, [anchor[0] for anchor in anchors]))
        green = float(np.interp(fraction, positions, [anchor[1] for anchor in anchors]))
        blue = float(np.interp(fraction, positions, [anchor[2] for anchor in anchors]))
        table.SetColorEntry(index, (int(round(red)), int(round(green)), int(round(blue)), 255))
    return table


# Anchor colours sampled from the named ramps. Perceptually ordered, so a
# greyscale print still reads low-to-high.
COLOUR_ANCHORS: dict[str, list[tuple[int, int, int]]] = {
    "viridis": [(68, 1, 84), (59, 82, 139), (33, 145, 140), (94, 201, 98), (253, 231, 37)],
    "magma": [(0, 0, 4), (81, 18, 124), (183, 55, 121), (252, 137, 97), (252, 253, 191)],
    "grey": [(0, 0, 0), (255, 255, 255)],
    # Diverging, for the aerosol index in phase 2.
    "rdbu": [(5, 48, 97), (146, 197, 222), (247, 247, 247), (244, 165, 130), (103, 0, 31)],
    # Vegetation: bare ground through sparse cover to dense canopy. Not
    # perceptually uniform, but it is the convention for NDVI and a reader
    # expects brown to mean bare and green to mean growing.
    "brown_green": [(120, 86, 56), (191, 158, 110), (223, 216, 150), (134, 181, 95), (26, 102, 46)],
}


def write_styled_copy(
    *,
    source_path: str,
    output_path: Path,
    value_range: tuple[float, float],
    colormap: str,
    settings: dict[str, Any],
    metadata: dict[str, str],
    log_scale: bool = False,
) -> None:
    """The 8-bit copy a map service can serve without applying a colour ramp.

    Paletted rather than RGB: one band with a colour table is a third of the
    size, keeps one value per pixel so a reader can still recover the class,
    and carries transparency for no data.
    """
    from osgeo import gdal

    source = gdal.Open(source_path)
    values = source.GetRasterBand(1).ReadAsArray()
    styled = styled_bytes(values, value_range, log_scale=log_scale)

    with atomic_output(output_path) as temporary:
        driver = gdal.GetDriverByName("GTiff")
        dataset = driver.Create(
            str(temporary),
            styled.shape[1],
            styled.shape[0],
            1,
            gdal.GDT_Byte,
            options=creation_options(settings, float_data=False),
        )
        dataset.SetGeoTransform(source.GetGeoTransform())
        dataset.SetProjection(source.GetProjection())
        band = dataset.GetRasterBand(1)
        # The palette must be set before the first pixel is written: libtiff
        # cannot change PhotometricInterpretation once a strip has gone out.
        band.SetRasterColorTable(colour_table(colormap))
        band.SetRasterColorInterpretation(gdal.GCI_PaletteIndex)
        band.SetNoDataValue(0)
        band.WriteArray(styled)
        dataset.SetMetadata({
            **metadata,
            "PYSENT_STYLE_RANGE": f"{value_range[0]},{value_range[1]}",
            "PYSENT_STYLE_SCALE": "log10" if log_scale else "linear",
        })
        if settings["overview_factors"]:
            dataset.BuildOverviews("NEAREST", settings["overview_factors"])
        dataset.FlushCache()
        dataset = None
    source = None


def write_thumbnail(*, source_path: str, output_path: Path, value_range: tuple[float, float],
                    colormap: str, log_scale: bool = False) -> None:
    """A small paletted PNG for a catalogue page, decimated on read."""
    from osgeo import gdal

    source = gdal.Open(source_path)
    width, height = source.RasterXSize, source.RasterYSize
    longest = max(width, height)
    scale = min(1.0, THUMBNAIL_PIXELS / longest)
    target_width, target_height = max(1, round(width * scale)), max(1, round(height * scale))

    # Averaging keeps a sparse map visible; nearest would drop most valid pixels
    # of a granule where only a quarter of them survived the quality mask.
    transform = source.GetGeoTransform()
    bounds = (
        transform[0],
        transform[3] + transform[5] * height,
        transform[0] + transform[1] * width,
        transform[3],
    )
    resampled = gdal.Warp(
        "/vsimem/s5p_thumbnail.tif",
        source,
        format="GTiff",
        width=target_width,
        height=target_height,
        # Bounds and projection are given explicitly: left to work them out,
        # GDAL reports "unable to compute output bounds" on a whole-orbit
        # raster and carries on, which is exactly the kind of quiet result the
        # plan says not to trust.
        outputBounds=bounds,
        dstSRS=source.GetProjection(),
        resampleAlg="average",
        dstNodata=float("nan"),
        outputType=gdal.GDT_Float32,
    )
    values = resampled.GetRasterBand(1).ReadAsArray()
    resampled = None
    gdal.Unlink("/vsimem/s5p_thumbnail.tif")
    source = None

    styled = styled_bytes(values, value_range, log_scale=log_scale)
    with atomic_output(output_path) as temporary:
        memory = gdal.GetDriverByName("MEM").Create("", styled.shape[1], styled.shape[0], 1, gdal.GDT_Byte)
        band = memory.GetRasterBand(1)
        band.WriteArray(styled)
        band.SetNoDataValue(0)
        band.SetRasterColorTable(colour_table(colormap))
        gdal.GetDriverByName("PNG").CreateCopy(str(temporary), memory)
        memory = None


def finish_float_product(*, path: str, settings: dict[str, Any], metadata: dict[str, str]) -> None:
    from osgeo import gdal

    dataset = gdal.Open(path, gdal.GA_Update)
    dataset.SetMetadata(metadata)
    if settings["overview_factors"]:
        # Nodata-aware averaging, so an overview of a sparse map does not pull
        # the no-data value into its neighbours.
        gdal.SetConfigOption("GDAL_OVR_PROPAGATE_NODATA", "YES")
        try:
            dataset.BuildOverviews("AVERAGE", settings["overview_factors"])
        finally:
            gdal.SetConfigOption("GDAL_OVR_PROPAGATE_NODATA", None)
    dataset.FlushCache()
    dataset = None
