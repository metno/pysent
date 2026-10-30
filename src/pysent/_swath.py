"""Put a satellite swath on a map grid.

A swath product does not come on a grid. Each pixel carries its own latitude
and longitude, the rows follow the ground track and the columns fan out across
it, so the array is not a picture of anywhere until it has been resampled. This
module does that resampling, and the handful of decisions that come with it:
which projection and resolution to use, where the output's edges are, which
pixels are valid, and what the covered area actually is.

It is written for Sentinel-5P first, but nothing here knows about Sentinel-5P;
Sentinel-3's OLCI and SLSTR swaths take the same path with a different reader
in front of it.

Three things in here exist because of measurements rather than taste:

* **The warp goes through a geolocation array**, not through corner polygons.
  Rasterising every pixel's four corners is the obvious exact method and it is
  what the tests compare against, but on a full orbit it cost 35 s and 2.9 GB
  against 3 s and 324 MB, and it draws garbage across the map for any pixel
  that straddles ±180°. The geolocation warp agrees with it to 0.15-0.35 %
  away from the poles.
* **``GEOREFERENCING_CONVENTION=PIXEL_CENTER`` is set.** GDAL otherwise reads
  each geolocation sample as a pixel's top-left corner and shifts the result by
  half a source pixel - about 3.5 km for Sentinel-5P.
* **The projection is chosen per granule.** Plain lat/lon leaves 5.5 % holes on
  a swath that crosses the antimeridian and smears pixels across the map at the
  pole; a polar stereographic grid cannot hold a whole orbit. Neither is right
  for everything, so :func:`choose_grid` picks.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from osgeo import gdal, osr

# A swath that stays inside the mid-latitudes goes on a plain lat/lon grid.
# Outside them, or across the seam, it goes on a polar stereographic one.
POLAR_LATITUDE = 75.0
MID_LATITUDE_FLOOR = 30.0
# Beyond this latitude span a swath is a whole orbit: no polar grid can hold it,
# so it stays in lat/lon and accepts the fringe artefacts at the polar cap.
ORBIT_LATITUDE_SPAN = 120.0

EPSG_LATLON = 4326
EPSG_POLAR_NORTH = 32661  # UPS North, the grid Sentinel-1 products already use
EPSG_POLAR_SOUTH = 32761
DEFAULT_LATLON_RESOLUTION = 0.05  # degrees
DEFAULT_POLAR_RESOLUTION = 5000.0  # metres

# WGS 84, written out because the GEOLOCATION metadata domain wants WKT.
_GEOLOCATION_SRS = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
)

RESAMPLING_METHODS = ("near", "average", "bilinear")


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def open_root(path: str):
    """Open a NetCDF/HDF5 file through GDAL's multidimensional API."""
    dataset = gdal.OpenEx(path, gdal.OF_MULTIDIM_RASTER)
    if dataset is None:
        raise ValueError(f"{path} could not be opened as a multidimensional dataset")
    return dataset.GetRootGroup()


def open_array(root, path: str):
    """Open an array by its group path, e.g. ``/PRODUCT/qa_value``."""
    group = root
    parts = [part for part in path.split("/") if part]
    for part in parts[:-1]:
        child = group.OpenGroup(part)
        if child is None:
            raise ValueError(f"group /{part} is missing; looked for {path}")
        group = child
    try:
        array = group.OpenMDArray(parts[-1])
    except RuntimeError:
        # With GDAL exceptions enabled a missing array raises "Array x does not
        # exist", which says nothing about what the product does have.
        array = None
    if array is None:
        available = ", ".join(sorted(group.GetMDArrayNames() or [])) or "none"
        raise ValueError(f"{path} is not in this product; it has: {available}")
    return array


def read_array(root, path: str, *, masked: bool = True) -> np.ndarray:
    """Read an array as float32, fill value as NaN, scale and offset applied.

    The multidimensional reader hands back *raw* packed values and exposes the
    packing as properties rather than attributes, so a caller that forgets this
    gets a quality band of 0-100 where it expected 0-1. Applying it here is the
    whole reason this function exists.

    The leading length-1 time axis that Sentinel-5P products carry is dropped.
    """
    array = open_array(root, path)
    raw = array.ReadAsArray()
    values = raw.astype(np.float32 if raw.dtype.kind != "f" else raw.dtype)

    nodata = array.GetNoDataValueAsDouble()
    if masked and nodata is not None:
        if raw.dtype.kind == "f":
            # The fill value is 9.96921e36 and does not always round-trip exactly.
            fill = np.isclose(raw, nodata, rtol=1e-6, atol=0) | ~np.isfinite(raw)
        else:
            fill = raw == nodata
        values = np.where(fill, np.nan, values)

    scale, offset = array.GetScale(), array.GetOffset()
    if scale not in (None, 1.0):
        values = values * np.float32(scale)
    if offset not in (None, 0.0):
        values = values + np.float32(offset)

    if values.ndim >= 3 and values.shape[0] == 1:
        values = np.squeeze(values, axis=0)
    return values


def read_raw_array(root, path: str) -> np.ndarray:
    """Read an array exactly as stored, with no masking, scaling or offset.

    Used for the quality band: thresholds have to be compared against the
    stored byte, because a scaled float never equals the threshold it came from.
    """
    raw = open_array(root, path).ReadAsArray()
    if raw.ndim >= 3 and raw.shape[0] == 1:
        raw = np.squeeze(raw, axis=0)
    return raw


# --------------------------------------------------------------------------- #
# Geometry of the swath
# --------------------------------------------------------------------------- #
def crosses_antimeridian(lon: np.ndarray) -> bool:
    """True when neighbouring pixel centres jump more than 180° in longitude.

    That jump is what a swath crossing ±180° looks like in the array: the
    longitudes themselves are continuous on the ground but wrap in the numbers.
    """
    with np.errstate(invalid="ignore"):
        across = np.nanmax(np.abs(np.diff(lon, axis=1))) if lon.shape[1] > 1 else 0.0
        along = np.nanmax(np.abs(np.diff(lon, axis=0))) if lon.shape[0] > 1 else 0.0
    return bool(np.nan_to_num(across) > 180.0 or np.nan_to_num(along) > 180.0)


def contains_pole(lat: np.ndarray) -> bool:
    """True when the swath reaches into either polar cap."""
    with np.errstate(invalid="ignore"):
        return bool(np.nanmax(np.abs(lat)) >= POLAR_LATITUDE)


# One degree of latitude, for turning a ground sampling in metres into the
# degrees a geographic grid wants. Good to a few per cent, which is far finer
# than the choice of grid step needs.
METRES_PER_DEGREE = 111_320.0


def choose_grid(
    lat: np.ndarray,
    lon: np.ndarray,
    *,
    epsg: int | str = "auto",
    resolution: float | None = None,
    ground_sampling_m: float | None = None,
) -> tuple[int, float]:
    """Pick the output projection and resolution for this swath.

    With an explicit ``epsg`` only the resolution is defaulted. With ``"auto"``:

    * a swath spanning more than 120° of latitude is a whole orbit and stays in
      **EPSG:4326**, because no polar stereographic grid can hold it. The known
      cost is fringe artefacts along the polar cap;
    * otherwise a swath that crosses the antimeridian or reaches a pole goes on
      **EPSG:32661** (UPS North) or **32761** in the south, at 5 km. In lat/lon
      those leave 5.5 % holes and smear pixels across the map;
    * anything else - the ordinary mid-latitude case - goes on **EPSG:4326** at
      0.05°.

    ``resolution`` is in the *chosen* CRS's own units, so it can only be given
    by a caller who already knows which CRS that will be.
    ``ground_sampling_m`` is the alternative for a caller who does not: a
    distance on the ground, converted to degrees or left in metres once the CRS
    is known. A product whose native sampling differs from the family's passes
    this, since it cannot know whether the granule will land on a geographic or
    a projected grid.

    Returns ``(epsg, resolution)``, the resolution in degrees for EPSG:4326 and
    in metres otherwise.
    """
    if epsg not in ("auto", None, ""):
        chosen = int(epsg)
        if resolution is not None:
            return chosen, float(resolution)
        if ground_sampling_m is not None:
            return chosen, _ground_sampling_in(chosen, ground_sampling_m)
        return chosen, DEFAULT_LATLON_RESOLUTION if chosen == EPSG_LATLON else DEFAULT_POLAR_RESOLUTION

    with np.errstate(invalid="ignore"):
        lat_min, lat_max = float(np.nanmin(lat)), float(np.nanmax(lat))
    spans_orbit = (lat_max - lat_min) > ORBIT_LATITUDE_SPAN

    if not spans_orbit and (crosses_antimeridian(lon) or contains_pole(lat)):
        northern = (lat_min + lat_max) / 2.0 >= 0.0
        chosen = EPSG_POLAR_NORTH if northern else EPSG_POLAR_SOUTH
        return chosen, _resolution_for(chosen, resolution, ground_sampling_m)

    # Mid-latitudes, and whole orbits for want of anything better.
    return EPSG_LATLON, _resolution_for(EPSG_LATLON, resolution, ground_sampling_m)


def _ground_sampling_in(epsg: int, ground_sampling_m: float) -> float:
    """``ground_sampling_m`` expressed in the units of ``epsg``."""
    if int(epsg) == EPSG_LATLON:
        return float(ground_sampling_m) / METRES_PER_DEGREE
    return float(ground_sampling_m)


def _resolution_for(epsg: int, resolution: float | None, ground_sampling_m: float | None) -> float:
    if resolution is not None:
        return float(resolution)
    if ground_sampling_m is not None:
        return _ground_sampling_in(epsg, ground_sampling_m)
    return DEFAULT_LATLON_RESOLUTION if int(epsg) == EPSG_LATLON else DEFAULT_POLAR_RESOLUTION


def _spatial_reference(epsg: int) -> osr.SpatialReference:
    reference = osr.SpatialReference()
    reference.ImportFromEPSG(int(epsg))
    # x is always longitude or easting, whatever the authority says about axis order.
    reference.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return reference


def grid_bounds(lat: np.ndarray, lon: np.ndarray, epsg: int, resolution: float) -> tuple[float, float, float, float]:
    """Output bounds ``(min_x, min_y, max_x, max_y)`` covering the swath.

    Snapped outwards to whole multiples of the resolution, so the same product
    on the same grid always lands on the same cell edges and two granules can
    be compared cell by cell.
    """
    with np.errstate(invalid="ignore"):
        if int(epsg) == EPSG_LATLON:
            lat_min, lat_max = float(np.nanmin(lat)), float(np.nanmax(lat))
            if crosses_antimeridian(lon):
                # The swath wraps, so the only honest x extent is the whole world.
                x_min, x_max = -180.0, 180.0
            else:
                x_min, x_max = float(np.nanmin(lon)), float(np.nanmax(lon))
            y_min, y_max = lat_min, lat_max
        else:
            transform = osr.CoordinateTransformation(_spatial_reference(EPSG_LATLON), _spatial_reference(epsg))
            # Every fourth pixel is plenty to find the extent and keeps this cheap
            # on an orbit: 1.9 M pixels become 120 k points.
            sample = np.column_stack([lon[::4, ::4].ravel(), lat[::4, ::4].ravel()])
            sample = sample[np.isfinite(sample).all(axis=1)]
            if not sample.size:
                raise ValueError("the swath has no finite coordinates, so it cannot be gridded")
            points = np.asarray(transform.TransformPoints(sample.tolist()))
            x, y = points[:, 0], points[:, 1]
            x_min, x_max, y_min, y_max = float(x.min()), float(x.max()), float(y.min()), float(y.max())

    step = float(resolution)
    return (
        float(np.floor(x_min / step) * step),
        float(np.floor(y_min / step) * step),
        float(np.ceil(x_max / step) * step),
        float(np.ceil(y_max / step) * step),
    )


def footprint_polygon(lat: np.ndarray, lon: np.ndarray, *, step: int = 8) -> dict[str, Any]:
    """The swath outline as GeoJSON, in lat/lon, split where it crosses ±180°.

    Walked around the edge of the array - down one side, along the far end, back
    up the other - rather than taken from the bounding box, which for a swath is
    much larger than the swath. Returns a ``Polygon`` or, for a swath that
    crosses the seam, a ``MultiPolygon``, so the geometry stays valid in any
    consumer that assumes longitudes are between -180 and 180.
    """
    rows, columns = lat.shape
    step = max(1, int(step))
    edge_rows = list(range(0, rows, step))
    if edge_rows[-1] != rows - 1:
        edge_rows.append(rows - 1)
    edge_columns = list(range(0, columns, step))
    if edge_columns[-1] != columns - 1:
        edge_columns.append(columns - 1)

    ring_indices = (
        [(row, 0) for row in edge_rows]
        + [(rows - 1, column) for column in edge_columns]
        + [(row, columns - 1) for row in reversed(edge_rows)]
        + [(0, column) for column in reversed(edge_columns)]
    )
    ring = [(float(lon[row, column]), float(lat[row, column])) for row, column in ring_indices]
    ring = [point for point in ring if np.isfinite(point[0]) and np.isfinite(point[1])]
    if len(ring) < 4:
        raise ValueError("the swath edge has too few finite coordinates to make a footprint")
    if ring[0] != ring[-1]:
        ring.append(ring[0])

    if not crosses_antimeridian(lon):
        return {"type": "Polygon", "coordinates": [ring]}

    # Shift everything to a continuous 0..360 frame, then cut at the seam, so
    # neither half is drawn across the map.
    shifted = [((x + 360.0) % 360.0, y) for x, y in ring]
    west = [(x - 360.0 if x > 180.0 else x, y) for x, y in shifted if x >= 180.0]
    east = [(x, y) for x, y in shifted if x < 180.0]
    parts = []
    for part in (west, east):
        if len(part) >= 3:
            closed = part + [part[0]]
            parts.append([closed])
    if not parts:
        return {"type": "Polygon", "coordinates": [ring]}
    return {"type": "MultiPolygon", "coordinates": parts}


# --------------------------------------------------------------------------- #
# The warp
# --------------------------------------------------------------------------- #
def _write_geolocation_tiff(path: str, data: np.ndarray) -> None:
    """Write one coordinate array as a Float64 single-band GeoTIFF.

    Float64 rather than Float32: a degree in Float32 resolves to about 2 m at
    180°, which is small but free to avoid, and GDAL refuses to use an integer
    geolocation array at all.
    """
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(path, data.shape[1], data.shape[0], 1, gdal.GDT_Float64)
    dataset.GetRasterBand(1).WriteArray(np.asarray(data, dtype=np.float64))
    dataset.FlushCache()
    dataset = None


def _write_value_tiff(path: str, data: np.ndarray) -> None:
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(path, data.shape[1], data.shape[0], 1, gdal.GDT_Float32)
    band = dataset.GetRasterBand(1)
    band.WriteArray(np.asarray(data, dtype=np.float32))
    band.SetNoDataValue(float("nan"))
    dataset.FlushCache()
    dataset = None


# A decimated geolocation grid: GDAL reads every n-th sample and treats it as
# the centre of an n x n block, so the array has to be shifted back by half a
# block or everything lands (n-1)/2 source pixels away. Measured on a real OLCI
# frame, as the share of source pixels landing in the output cell that holds
# their own coordinates:
#
#   step  with -(n-1)/2 offset   with no offset
#      1              88.1 %            88.1 %
#      2              87.9 %            47.5 %   (1.0 source pixels out)
#      4              87.6 %             0.0 %   (2.2 out)
#      8              87.3 %             0.0 %   (5.0 out)
#     16              86.7 %             0.0 %   (10.6 out, about 3 km)
#
# The useful half of that table is the left column: with the offset right, a
# decimated grid costs almost nothing in accuracy, so the step can be chosen on
# cost alone.
GEOLOCATION_STEPS = (1, 2, 8, 16, 32, 64)
# 4 is absent deliberately. It measured correct on a 296-row frame, but the
# audit saw it return a truncated warp on a full 4091-row frame, and 2 and 8 are
# indistinguishable from it in accuracy - so there is nothing to gain by
# allowing a value that has been seen to fail.
REFUSED_GEOLOCATION_STEP = 4


def warp_swath(
    *,
    values: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    output_path: str,
    epsg: int,
    resolution: float,
    bounds: tuple[float, float, float, float] | None = None,
    resampling: str = "average",
    scratch: Path,
    creation_options: list[str] | None = None,
    num_threads: int | None = None,
    geoloc_step: int = 1,
    minimum_valid: int | None = None,
) -> str:
    """Resample a swath onto a map grid and write it as a Float32 GeoTIFF.

    ``values`` carries NaN wherever a pixel is missing or was masked, and the
    output carries NaN as its nodata value, so a masked pixel stays masked
    through the warp instead of being averaged into its neighbours.

    The geolocation arrays are handed to GDAL as side-car rasters referenced
    from a VRT's ``GEOLOCATION`` metadata domain, with
    ``GEOREFERENCING_CONVENTION=PIXEL_CENTER`` - without it each sample is read
    as a pixel's top-left corner and the whole result shifts by half a source
    pixel.
    """
    if values.shape != lat.shape or values.shape != lon.shape:
        raise ValueError(
            f"values {values.shape}, latitude {lat.shape} and longitude {lon.shape} must have the same shape"
        )
    if resampling not in RESAMPLING_METHODS:
        raise ValueError(f"resampling must be one of {', '.join(RESAMPLING_METHODS)}, not {resampling!r}")
    geoloc_step = int(geoloc_step)
    if geoloc_step == REFUSED_GEOLOCATION_STEP:
        raise ValueError(
            f"geoloc_step {REFUSED_GEOLOCATION_STEP} is refused: it has been observed to return a "
            f"truncated warp on a full-size frame. Use {GEOLOCATION_STEPS[1]} or {GEOLOCATION_STEPS[2]}, "
            "which measure the same in accuracy."
        )
    if geoloc_step not in GEOLOCATION_STEPS:
        raise ValueError(
            f"geoloc_step must be one of {', '.join(str(value) for value in GEOLOCATION_STEPS)}, "
            f"not {geoloc_step}"
        )

    scratch = Path(scratch)
    value_path = str(scratch / "swath_values.tif")
    lat_path = str(scratch / "swath_latitude.tif")
    lon_path = str(scratch / "swath_longitude.tif")
    vrt_path = str(scratch / "swath_values.vrt")

    _write_value_tiff(value_path, values)
    _write_geolocation_tiff(lat_path, lat[::geoloc_step, ::geoloc_step])
    _write_geolocation_tiff(lon_path, lon[::geoloc_step, ::geoloc_step])

    # See GEOLOCATION_STEPS: a sample of an n x n block is read as its centre.
    sample_offset = -(geoloc_step - 1) / 2.0

    vrt = gdal.Translate(vrt_path, value_path, format="VRT")
    vrt.SetMetadata(
        {
            "SRS": _GEOLOCATION_SRS,
            "X_DATASET": lon_path,
            "X_BAND": "1",
            "Y_DATASET": lat_path,
            "Y_BAND": "1",
            "PIXEL_OFFSET": str(sample_offset),
            "LINE_OFFSET": str(sample_offset),
            "PIXEL_STEP": str(geoloc_step),
            "LINE_STEP": str(geoloc_step),
            "GEOREFERENCING_CONVENTION": "PIXEL_CENTER",
        },
        "GEOLOCATION",
    )
    vrt.FlushCache()
    vrt = None

    options: dict[str, Any] = {
        "format": "GTiff",
        "dstSRS": f"EPSG:{int(epsg)}",
        "geoloc": True,
        "xRes": float(resolution),
        "yRes": float(resolution),
        "resampleAlg": resampling,
        "dstNodata": float("nan"),
        "outputType": gdal.GDT_Float32,
        "creationOptions": list(creation_options or ["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=3"]),
    }
    if bounds is not None:
        options["outputBounds"] = tuple(float(value) for value in bounds)
    if num_threads:
        # Either a count or GDAL's own "ALL_CPUS"; both are valid here.
        options["warpOptions"] = [f"NUM_THREADS={num_threads}"]

    gdal.Warp(output_path, vrt_path, **options)

    # Never trust the warp: check it produced something. An unscaled or
    # otherwise unusable geolocation array does not raise - GDAL logs "too many
    # points failed to transform", writes a raster of exactly the right size
    # with **no valid pixels at all**, and returns successfully. A truncated
    # warp from a bad step looks the same, only partly. Measured: an unscaled
    # Int32 geolocation gave 0 valid pixels where a scaled one gave 2,171,012.
    if minimum_valid is not None:
        written = gdal.Open(output_path)
        band = written.GetRasterBand(1)
        histogram_valid = _count_valid(band)
        written = None
        if histogram_valid < minimum_valid:
            raise ValueError(
                f"the warp produced {histogram_valid:,} valid pixels but at least {minimum_valid:,} "
                "were expected, so it is truncated or empty. A geolocation array that GDAL cannot "
                "use gives exactly this: a correctly sized raster with nothing in it, and no error."
            )

    for temporary in (value_path, lat_path, lon_path, vrt_path):
        gdal.Unlink(temporary) if temporary.startswith("/vsi") else Path(temporary).unlink(missing_ok=True)
    return output_path


# --------------------------------------------------------------------------- #
# What came out
# --------------------------------------------------------------------------- #
def _count_valid(band: Any) -> int:
    """Valid (non-nodata) pixels of a band, read block-wise.

    Counted without holding the whole raster, so validating a 100-megapixel
    output costs a few megabytes rather than hundreds.
    """
    total = 0
    block_x, block_y = band.GetBlockSize()
    block_y = max(1, block_y)
    for offset in range(0, band.YSize, block_y):
        rows = min(block_y, band.YSize - offset)
        chunk = band.ReadAsArray(0, offset, band.XSize, rows)
        total += int(np.count_nonzero(np.isfinite(chunk)))
    return total


def value_statistics(values: np.ndarray) -> dict[str, float]:
    """Min, 1st, 50th and 99th percentile and max of the valid values.

    The percentiles are what a colour range is chosen from; the extremes alone
    are single-pixel noise, which for these products can be negative.
    """
    finite = values[np.isfinite(values)]
    if not finite.size:
        return {"min": float("nan"), "p01": float("nan"), "p50": float("nan"), "p99": float("nan"), "max": float("nan")}
    p01, p50, p99 = (float(value) for value in np.percentile(finite, [1.0, 50.0, 99.0]))
    return {"min": float(finite.min()), "p01": p01, "p50": p50, "p99": p99, "max": float(finite.max())}


def valid_fraction(values: np.ndarray) -> float:
    """Share of the array that is a real measurement, between 0 and 1."""
    if not values.size:
        return 0.0
    return float(np.count_nonzero(np.isfinite(values)) / values.size)
