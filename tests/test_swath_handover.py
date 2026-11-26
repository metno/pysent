"""The swath warp lets go of its arrays before GDAL's warp, and writes the same file.

``warp_swath`` writes every array to scratch before the first ``gdal.Warp``.
A caller that hands them over in a ``SwathArrays`` gives it the only
references, so they are freed before GDAL runs: about 0.3 GB on a
full-resolution OLCI frame. ``_combine_bands`` copies the two warped bands in
strips of whole tile rows instead of two whole-grid reads. Neither may change
a byte of the output.
"""
import gc
import weakref
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("osgeo")

from osgeo import gdal  # noqa: E402

from pysent import _swath  # noqa: E402
from pysent._swath import EPSG_LATLON, SwathArrays, _combine_bands, warp_swath  # noqa: E402


def _frame(rows=80, columns=60):
    lat = np.repeat(np.linspace(60.0, 61.6, rows)[:, None], columns, axis=1)
    lon = np.repeat(np.linspace(10.0, 11.2, columns)[None, :], rows, axis=0)
    lon = lon + np.linspace(0.0, 0.3, rows)[:, None]  # sheared, like a real swath
    values = (np.arange(rows)[:, None] * columns + np.arange(columns)[None, :]).astype(np.float32)
    values[::7, ::5] = np.nan
    reasons = np.where(np.isnan(values), 3.0, 0.0).astype(np.float32)
    return values, lat, lon, reasons


WARP = dict(epsg=EPSG_LATLON, resolution=0.02, resampling="average")


def _bytes(path):
    return Path(path).read_bytes()


@pytest.mark.parametrize("with_reasons", [False, True])
def test_a_handed_over_swath_writes_the_same_file(tmp_path, with_reasons):
    values, lat, lon, reasons = _frame()
    lent = tmp_path / "lent"
    handed = tmp_path / "handed"
    lent.mkdir()
    handed.mkdir()

    warp_swath(
        values=values, lat=lat, lon=lon, categorical=reasons if with_reasons else None,
        output_path=str(lent / "out.tif"), scratch=lent, minimum_valid=1, **WARP,
    )
    swath = SwathArrays(
        values=values.copy(), lat=lat.copy(), lon=lon.copy(),
        categorical=reasons.copy() if with_reasons else None,
    )
    warp_swath(swath=swath, output_path=str(handed / "out.tif"), scratch=handed, minimum_valid=1, **WARP)

    assert _bytes(lent / "out.tif") == _bytes(handed / "out.tif")
    # The holder is emptied, and the scratch is cleaned either way.
    assert swath.values is swath.lat is swath.lon is swath.categorical is None
    assert sorted(p.name for p in handed.iterdir()) == ["out.tif"]
    assert sorted(p.name for p in lent.iterdir()) == ["out.tif"]


def test_handed_over_arrays_are_freed_before_gdal_warps(tmp_path, monkeypatch):
    values, lat, lon, reasons = _frame()
    alive = [weakref.ref(array) for array in (values, lat, lon, reasons)]
    swath = SwathArrays(values=values, lat=lat, lon=lon, categorical=reasons)
    del values, lat, lon, reasons

    seen = []
    real_warp = gdal.Warp

    def spy(destination, source, **options):
        gc.collect()
        seen.append([ref() is None for ref in alive])
        return real_warp(destination, source, **options)

    monkeypatch.setattr(gdal, "Warp", spy)
    warp_swath(swath=swath, output_path=str(tmp_path / "out.tif"), scratch=tmp_path, **WARP)

    assert seen == [[True] * 4, [True] * 4]  # both warps: the values and the reasons


def test_the_arrays_are_given_one_way_or_the_other(tmp_path):
    values, lat, lon, _ = _frame()
    swath = SwathArrays(values=values, lat=lat, lon=lon)
    with pytest.raises(ValueError, match="not both"):
        warp_swath(swath=swath, values=values, output_path=str(tmp_path / "x.tif"), scratch=tmp_path, **WARP)
    with pytest.raises(ValueError, match="needs values, lat and lon"):
        warp_swath(values=values, output_path=str(tmp_path / "x.tif"), scratch=tmp_path, **WARP)


def _two_warped_bands(tmp_path, width, height, tile):
    rng = np.random.default_rng(7)
    options = ["TILED=YES", f"BLOCKXSIZE={tile}", f"BLOCKYSIZE={tile}"]
    paths = []
    for name, data in (
        ("values", rng.random((height, width), dtype=np.float32)),
        ("reasons", rng.integers(0, 4, (height, width)).astype(np.float32)),
    ):
        path = str(tmp_path / f"{name}.tif")
        dataset = gdal.GetDriverByName("GTiff").Create(path, width, height, 1, gdal.GDT_Float32, options=options)
        dataset.SetGeoTransform((10.0, 0.01, 0.0, 61.0, 0.0, -0.01))
        dataset.GetRasterBand(1).WriteArray(data)
        dataset = None
        paths.append(path)
    return paths


def _combined_whole(values_path, reason_path, output_path, creation_options):
    """``_combine_bands`` as it was: each band read and written whole."""
    values, reasons = gdal.Open(values_path), gdal.Open(reason_path)
    dataset = gdal.GetDriverByName("GTiff").Create(
        output_path, values.RasterXSize, values.RasterYSize, 2, gdal.GDT_Float32, options=creation_options,
    )
    dataset.SetGeoTransform(values.GetGeoTransform())
    dataset.SetProjection(values.GetProjection())
    first = dataset.GetRasterBand(1)
    first.SetNoDataValue(float("nan"))
    first.WriteArray(values.GetRasterBand(1).ReadAsArray())
    dataset.GetRasterBand(2).WriteArray(reasons.GetRasterBand(1).ReadAsArray())
    dataset.FlushCache()
    dataset = values = reasons = None


@pytest.mark.parametrize("cache_mb", [2, 4, 64])
def test_combining_in_strips_writes_the_same_file_as_whole_reads(tmp_path, monkeypatch, cache_mb):
    """The flush-order trap: with the product bigger than GDAL's block cache,
    GDAL flushes tiles half written and rewrites them at new offsets, so a
    different write order gives the same pixels in a different file. Strips of
    whole tile rows keep the file; the product here is ~9 MB, so caches of 2
    and 4 MB put it past the cache."""
    width, height, tile = 1100, 1030, 256
    values_path, reason_path = _two_warped_bands(tmp_path, width, height, tile)
    creation = ["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=3"]
    previous = gdal.GetCacheMax()
    gdal.SetCacheMax(cache_mb * 1024 * 1024)
    try:
        _combined_whole(values_path, reason_path, str(tmp_path / "whole.tif"), creation)
        # Strips far smaller than the grid: a few tile rows each, and a last one short.
        monkeypatch.setattr(_swath, "_COMBINE_STRIP_CELLS", width * tile * 2 + 5)
        _combine_bands(
            values_path=values_path, reason_path=reason_path,
            output_path=str(tmp_path / "strips.tif"), creation_options=creation,
        )
    finally:
        gdal.SetCacheMax(previous)

    assert _bytes(tmp_path / "whole.tif") == _bytes(tmp_path / "strips.tif")


def test_a_strip_smaller_than_one_tile_row_is_still_a_whole_tile_row(tmp_path, monkeypatch):
    values_path, reason_path = _two_warped_bands(tmp_path, 300, 200, 64)
    reads = []
    real = gdal.Band.ReadAsArray

    def spy(self, *args, **kwargs):
        reads.append(args)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(_swath, "_COMBINE_STRIP_CELLS", 1)
    monkeypatch.setattr(gdal.Band, "ReadAsArray", spy)
    _combine_bands(
        values_path=values_path, reason_path=reason_path,
        output_path=str(tmp_path / "out.tif"), creation_options=["TILED=YES", "BLOCKYSIZE=64", "BLOCKXSIZE=64"],
    )
    offsets = [args[1] for args in reads]
    assert offsets == [0, 64, 128, 192] * 2
