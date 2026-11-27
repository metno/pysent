"""Sentinel-1 NetCDF: the geolocation warp through a decimated lat/lon grid.

The NetCDF product carries a latitude and a longitude for every pixel. Handed to
GDAL whole, the geolocation warp of a full scene grew past 4 GB on GDAL 3.13 and
was killed; the warp now reads every n-th sample from a staged copy. These tests
build a small CF NetCDF with full-resolution geolocation, as the product has,
and warp it through the real GDAL.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from osgeo import gdal

from pysent import s1

gdal.UseExceptions()

HEIGHT, WIDTH = 500, 600
PIXEL_M = 100.0
RESOLUTION = 200.0


def _swath_geolocation() -> tuple[np.ndarray, np.ndarray]:
    """A swath tilted about 11 degrees near 72 N, 100 m pixels, smooth as a real one."""
    rows, columns = np.mgrid[0:HEIGHT, 0:WIDTH].astype(np.float64)
    angle = 0.2
    lat = 72.0 + (rows * np.cos(angle) - columns * np.sin(angle)) * PIXEL_M / 111_320.0
    lon = 20.0 + (rows * np.sin(angle) + columns * np.cos(angle)) * PIXEL_M / (
        111_320.0 * np.cos(np.radians(lat))
    )
    return lat, lon


def _amplitude() -> np.ndarray:
    rows, columns = np.mgrid[0:HEIGHT, 0:WIDTH].astype(np.float64)
    amplitude = (1000 + 500 * np.sin(rows / 17.0) * np.cos(columns / 23.0)).astype(np.uint16)
    amplitude[:, :40] = 0  # a no-data margin, as the product's swath edges
    return amplitude


def _write_netcdf(path: Path, *, with_coordinates: bool = True) -> Path:
    """A CF NetCDF shaped like the product: Amplitude_HH with full-size lat and lon."""
    lat, lon = _swath_geolocation()
    dataset = gdal.GetDriverByName("netCDF").CreateMultiDimensional(str(path))
    group = dataset.GetRootGroup()
    y = group.CreateDimension("y", None, None, HEIGHT)
    x = group.CreateDimension("x", None, None, WIDTH)
    text = gdal.ExtendedDataType.CreateString()
    for name, data, data_type in (
        ("lat", lat.astype(np.float32), gdal.GDT_Float32),
        ("lon", lon.astype(np.float32), gdal.GDT_Float32),
        ("Amplitude_HH", _amplitude(), gdal.GDT_UInt16),
    ):
        array = group.CreateMDArray(name, [y, x], gdal.ExtendedDataType.Create(data_type))
        if name == "Amplitude_HH":
            if with_coordinates:
                array.CreateAttribute("coordinates", [], text).Write("lat lon")
            array.SetNoDataValueDouble(0)
        else:
            array.CreateAttribute("standard_name", [], text).Write("latitude" if name == "lat" else "longitude")
            array.CreateAttribute("units", [], text).Write("degrees_north" if name == "lat" else "degrees_east")
        array.Write(data)
    dataset = None
    return path


@pytest.fixture
def netcdf(tmp_path) -> Path:
    return _write_netcdf(tmp_path / "s1.nc")


def _read(path: Path) -> np.ndarray:
    dataset = gdal.Open(str(path))
    data = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    return data


def _reference_warp(netcdf: Path, output: Path, bounds=None) -> gdal.Dataset:
    """The warp as it was, from the full-size geolocation, but exact: the reference."""
    return gdal.Warp(
        str(output),
        f"NETCDF:{netcdf}:Amplitude_HH",
        format="GTiff",
        dstSRS=s1.S1_TARGET_EPSG,
        outputBounds=bounds,
        xRes=RESOLUTION,
        yRes=RESOLUTION,
        srcNodata=0,
        dstNodata=0,
        geoloc=True,
        errorThreshold=0.0,
        resampleAlg="average",
        outputType=gdal.GDT_Float32,
    )


def _warp(netcdf: Path, output: Path) -> gdal.Dataset:
    s1._warp_sentinel_s1_amplitude(
        str(netcdf), "Amplitude_HH", output, target_resolution=RESOLUTION, gdal_num_threads="2"
    )
    return gdal.Open(str(output))


def _bounds(dataset: gdal.Dataset) -> tuple[float, float, float, float]:
    x0, dx, _, y0, _, dy = dataset.GetGeoTransform()
    return (x0, y0 + dy * dataset.RasterYSize, x0 + dx * dataset.RasterXSize, y0)


def test_the_grid_is_the_one_the_full_geolocation_gives(netcdf, tmp_path):
    """Same size, and the origin within a few metres of GDAL's own from every sample.

    Left to suggest the extent from a decimated grid, GDAL adds half a sample
    of margin (here 8 pixels, 800 m): 305 x 341 cells instead of 304 x 340.
    """
    reference = _reference_warp(netcdf, tmp_path / "reference.tif")
    warped = _warp(netcdf, tmp_path / "warped.tif")
    assert (warped.RasterXSize, warped.RasterYSize) == (reference.RasterXSize, reference.RasterYSize)
    assert warped.GetGeoTransform()[1] == reference.GetGeoTransform()[1] == RESOLUTION
    # GDAL's margin at full resolution is half a pixel: 50 m here, 5 m on the
    # product's 10 m pixels.
    for mine, theirs in zip(_bounds(warped), _bounds(reference)):
        assert abs(mine - theirs) <= PIXEL_M / 2


def test_the_decimated_warp_matches_the_full_geolocation(netcdf, tmp_path):
    """On the same grid, every 16th sample gives the picture every sample gives."""
    warped = _warp(netcdf, tmp_path / "warped.tif")
    values = warped.GetRasterBand(1).ReadAsArray()
    reference = _reference_warp(netcdf, tmp_path / "reference.tif", bounds=_bounds(warped))
    expected = reference.GetRasterBand(1).ReadAsArray()

    valid_reference = expected != 0
    valid = values != 0
    assert valid_reference.sum() > 0.5 * expected.size
    # Valid masks: no cell is lost. The decimated inverse fills a partial fringe
    # one cell wide along the swath's edge (measured 0.55 % here, where a cell
    # holds only 2 x 2 source pixels; 59 cells of 29.9 million on a real scene).
    assert not (valid_reference & ~valid).any()
    gained = valid & ~valid_reference
    assert gained.sum() <= 0.01 * valid_reference.sum()
    padded = np.pad(valid_reference, 1)
    beside = np.zeros_like(valid_reference)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            beside |= padded[1 + dy : 1 + dy + valid.shape[0], 1 + dx : 1 + dx + valid.shape[1]]
    assert not (gained & ~beside).any(), "a gained cell is not beside the reference footprint"
    both = valid & valid_reference
    difference = np.abs(values[both].astype(np.float64) - expected[both])
    assert difference.mean() < 0.1  # amplitude counts, against a signal of 500-1500 (measured 0.018)
    assert np.percentile(difference, 99) < 0.5  # (measured 0.084)


def test_the_staged_grid_is_every_nth_sample_across_strip_boundaries(netcdf, tmp_path, monkeypatch):
    """Read in strips, the staged grid is still exactly lat[::n, ::n] and lon[::n, ::n]."""
    # As GDAL presents them (its netCDF driver may turn the rows bottom-up, the
    # data and its geolocation alike), which is what the warp reads.
    lat = gdal.Open(f'NETCDF:"{netcdf}":lat').ReadAsArray()
    lon = gdal.Open(f'NETCDF:"{netcdf}":lon').ReadAsArray()
    # Strips of 3 steps (48 rows): 500 rows end in a short strip.
    monkeypatch.setattr(s1, "S1_GEOLOCATION_STRIP_CELLS", WIDTH * 50)
    step = 16
    metadata, extremes = s1._stage_sentinel_s1_geolocation(f"NETCDF:{netcdf}:Amplitude_HH", tmp_path / "w", step)

    np.testing.assert_array_equal(_read(Path(metadata["Y_DATASET"])), lat[::step, ::step])
    np.testing.assert_array_equal(_read(Path(metadata["X_DATASET"])), lon[::step, ::step])
    assert metadata["PIXEL_STEP"] == metadata["LINE_STEP"] == "16"
    # A sample is read as the centre of its 16 x 16 block: shifted back by 7.5.
    assert float(metadata["PIXEL_OFFSET"]) == float(metadata["LINE_OFFSET"]) == -7.5
    assert metadata["GEOREFERENCING_CONVENTION"] == "PIXEL_CENTER"
    # The extremes are of the full arrays, not of the staged samples.
    west, east, south, north = extremes
    assert west == (float(lon.min()), float(lat.flat[np.argmin(lon)]))
    assert east == (float(lon.max()), float(lat.flat[np.argmax(lon)]))
    assert south == (float(lon.flat[np.argmin(lat)]), float(lat.min()))
    assert north == (float(lon.flat[np.argmax(lat)]), float(lat.max()))


def test_the_warp_is_exact_single_pooled_and_leaves_nothing_behind(netcdf, tmp_path, monkeypatch):
    """errorThreshold 0, GDAL_NUM_THREADS 1 and CPL_TMPDIR at scratch during the warp; all restored after."""
    seen: dict[str, object] = {}
    real_warp = gdal.Warp

    def spy(destination, source, **kwargs):
        seen["threads"] = gdal.GetConfigOption("GDAL_NUM_THREADS")
        seen["tmpdir"] = gdal.GetConfigOption("CPL_TMPDIR")
        seen["source"] = gdal.Open(str(source)).GetMetadata("GEOLOCATION")
        return real_warp(destination, source, **kwargs)

    monkeypatch.setattr(gdal, "Warp", spy)
    real_options = gdal.WarpOptions

    def options_spy(*args, **kwargs):
        seen["errorThreshold"] = kwargs.get("errorThreshold")
        return real_options(*args, **kwargs)

    monkeypatch.setattr(gdal, "WarpOptions", options_spy)
    gdal.SetConfigOption("GDAL_NUM_THREADS", "ALL_CPUS")
    try:
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        s1._warp_sentinel_s1_amplitude(str(netcdf), "Amplitude_HH", scratch / "w.tif", target_resolution=RESOLUTION)
        assert gdal.GetConfigOption("GDAL_NUM_THREADS") == "ALL_CPUS"
    finally:
        gdal.SetConfigOption("GDAL_NUM_THREADS", None)

    assert seen["threads"] == "1"
    assert seen["tmpdir"] == str(scratch)
    assert seen["errorThreshold"] == 0.0
    assert seen["source"]["PIXEL_STEP"] == str(s1.S1_NETCDF_GEOLOCATION_STEP)
    assert gdal.GetConfigOption("CPL_TMPDIR") is None
    # Only the warped raster is left: the staged grid and its VRT are gone.
    assert sorted(path.name for path in scratch.iterdir()) == ["w.tif"]


def test_a_netcdf_without_geolocation_is_refused_with_its_name(tmp_path):
    path = _write_netcdf(tmp_path / "bare.nc", with_coordinates=False)
    with pytest.raises(RuntimeError, match="carries no geolocation arrays"):
        s1._warp_sentinel_s1_amplitude(str(path), "Amplitude_HH", tmp_path / "w.tif")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bare.nc"]


def test_process_sentinel_s1_netcdf_writes_the_quicklook(netcdf, tmp_path):
    """The public call, end to end, through the decimated warp."""
    results = s1.process_sentinel_s1_netcdf(
        input_dataset=str(netcdf),
        output_dir=tmp_path / "out",
        output_names={"Amplitude_HH": "hh.tif"},
        processing_options={"target_resolution": RESOLUTION, "work_dir": str(tmp_path / "work")},
    )
    assert len(results) == 1
    written = gdal.Open(results[0]["path"])
    assert written.RasterCount >= 1
    assert written.GetRasterBand(1).ReadAsArray().any()
    written = None
    assert not any((tmp_path / "work").iterdir())
