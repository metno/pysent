"""The true-colour product and its picture are written in row strips.

Both used to hold every band of the grid at once: the warped bands were read
into memory to build the Float32 product, and the picture read the three
bands back whole and styled them together, which on a full-resolution OLCI
frame took a run to 2.7 GB. Working a strip of whole tile rows at a time keeps
the arithmetic exactly as it was, so every byte of every output must stay the
same. The references below are the whole-grid versions these functions
replaced, kept here only to prove that.
"""
from pathlib import Path
import tracemalloc

import numpy as np
import pytest

gdal = pytest.importorskip("osgeo.gdal")

from pysent import _output  # noqa: E402
from pysent._output import (  # noqa: E402
    creation_options,
    finish_float_product,
    rgb_bytes,
    write_rgb_product,
    write_rgb_styled,
)

gdal.UseExceptions()

SETTINGS = {
    "compression": "DEFLATE",
    "predictor": 3,
    "gdal_num_threads": "2",
    "overview_factors": [2, 4, 8],
}
VALUE_RANGE = (0.0, 0.6)
GAMMA = 0.6
TRANSFORM = (500000.0, 300.0, 0.0, 7000000.0, 0.0, -300.0)
ROWS, COLUMNS = 1100, 700


def _projection() -> str:
    srs = gdal.osr.SpatialReference()
    srs.ImportFromEPSG(32633)
    return srs.ExportToWkt()


def _bands(count: int = 3) -> list[np.ndarray]:
    """Reflectance-like bands, NaN off the swath and where the mask removed a pixel."""
    rng = np.random.default_rng(4)
    yy, xx = np.mgrid[:ROWS, :COLUMNS]
    outside = xx > COLUMNS * 0.8 + (yy - ROWS / 2) * 0.3
    removed = rng.random((ROWS, COLUMNS)) < 0.03
    bands = []
    for _ in range(count):
        band = rng.gamma(2.0, 0.08, size=(ROWS, COLUMNS)).astype(np.float32)
        band[rng.random((ROWS, COLUMNS)) < 0.02] = 1.3  # bright cloud: clips to white
        band[rng.random((ROWS, COLUMNS)) < 0.02] = -0.01  # below the range
        band[outside | removed] = np.nan
        bands.append(band)
    return bands


def _single_band_files(directory: Path, bands: list[np.ndarray]) -> list[str]:
    """The warp's per-band outputs: tiled, uncompressed, one band each."""
    paths = []
    for index, band in enumerate(bands):
        path = directory / f"warped_{index}.tif"
        dataset = gdal.GetDriverByName("GTiff").Create(
            str(path), COLUMNS, ROWS, 1, gdal.GDT_Float32, options=["TILED=YES", "COMPRESS=NONE"]
        )
        dataset.SetGeoTransform(TRANSFORM)
        dataset.SetProjection(_projection())
        dataset.GetRasterBand(1).SetNoDataValue(float("nan"))
        dataset.GetRasterBand(1).WriteArray(band)
        dataset = None
        paths.append(str(path))
    return paths


def _product(path: Path, bands, descriptions=None) -> Path:
    metadata = {"PYSENT_PRODUCT": "truecolor"}
    write_rgb_product(
        path=str(path), bands=bands, transform=TRANSFORM, projection=_projection(),
        settings=SETTINGS, metadata=metadata,
        descriptions=descriptions or [f"band_{index}" for index in range(len(bands))],
    )
    finish_float_product(path=str(path), settings=SETTINGS, metadata=metadata)
    return path


def _whole_grid_rgb_styled(*, source_path, output_path, value_range, gamma, settings, metadata):
    """``write_rgb_styled`` as it was: the three bands read and styled whole."""
    source = gdal.Open(source_path)
    bands = [source.GetRasterBand(index + 1).ReadAsArray() for index in range(3)]
    transform, projection = source.GetGeoTransform(), source.GetProjection()
    source = None
    rgb = rgb_bytes(bands, value_range, gamma=gamma)
    alpha = np.where(np.isfinite(bands[0]), 255, 0).astype(np.uint8)
    rows, columns = alpha.shape
    dataset = gdal.GetDriverByName("GTiff").Create(
        str(output_path), columns, rows, 4, gdal.GDT_Byte,
        options=creation_options(settings, float_data=False) + ["PHOTOMETRIC=RGB", "ALPHA=YES"],
    )
    dataset.SetGeoTransform(transform)
    dataset.SetProjection(projection)
    for index in range(3):
        dataset.GetRasterBand(index + 1).WriteArray(rgb[:, :, index])
    dataset.GetRasterBand(4).WriteArray(alpha)
    dataset.SetMetadata({
        **metadata,
        "PYSENT_STYLE_RANGE": f"{value_range[0]:g},{value_range[1]:g}",
        "PYSENT_STYLE_GAMMA": f"{gamma:g}",
        "PYSENT_STYLE_SCALE": "linear",
    })
    if settings["overview_factors"]:
        dataset.BuildOverviews("AVERAGE", settings["overview_factors"])
    dataset.FlushCache()
    dataset = None


@pytest.fixture
def small_strips(monkeypatch):
    # Far smaller than the grid, so it takes several strips and a short last one.
    monkeypatch.setattr(_output, "STRIP_CELLS", COLUMNS * 300)


@pytest.fixture(scope="module")
def product(tmp_path_factory):
    return _product(tmp_path_factory.mktemp("rgb") / "product.tif", _bands())


@pytest.mark.parametrize("count", [3, 7])
def test_the_product_from_files_is_byte_identical_to_the_one_from_arrays(tmp_path, small_strips, count):
    # Seven layers is a corrected surface_reflectance: three values, three
    # uncertainties and a quality band.
    bands = _bands(count)
    _product(tmp_path / "arrays.tif", bands)
    _product(tmp_path / "files.tif", _single_band_files(tmp_path, bands))

    assert (tmp_path / "files.tif").read_bytes() == (tmp_path / "arrays.tif").read_bytes()


@pytest.fixture
def small_cache():
    # Smaller than the product, as GDAL_CACHEMAX=512 is beside a full-resolution
    # frame's 550 MB: tiles are flushed before all their bands are written, and
    # where that happens depends on what else is in the cache.
    before = gdal.GetCacheMax()
    gdal.SetCacheMax(5 * 1024 * 1024)
    yield
    gdal.SetCacheMax(before)


def test_reading_the_band_files_leaves_the_cache_to_the_product(tmp_path, small_strips, small_cache):
    bands = _bands()
    _product(tmp_path / "arrays.tif", bands)
    _product(tmp_path / "files.tif", _single_band_files(tmp_path, bands))

    assert (tmp_path / "files.tif").read_bytes() == (tmp_path / "arrays.tif").read_bytes()


def test_a_band_file_on_another_grid_is_refused(tmp_path):
    paths = _single_band_files(tmp_path, _bands())
    small = tmp_path / "small.tif"
    gdal.GetDriverByName("GTiff").Create(str(small), 10, 10, 1, gdal.GDT_Float32)
    with pytest.raises(ValueError, match="band 3 is 10 x 10"):
        _product(tmp_path / "out.tif", paths[:2] + [str(small)])


def test_the_picture_is_byte_identical_to_the_whole_grid_one(product, tmp_path, small_strips):
    kwargs = dict(value_range=VALUE_RANGE, gamma=GAMMA, settings=SETTINGS,
                  metadata={"PYSENT_PRODUCT": "truecolor"})
    _whole_grid_rgb_styled(source_path=str(product), output_path=tmp_path / "whole.tif", **kwargs)
    write_rgb_styled(source_path=str(product), output_path=tmp_path / "strips.tif", **kwargs)

    assert (tmp_path / "strips.tif").read_bytes() == (tmp_path / "whole.tif").read_bytes()
    # The styled scratch files go with the call.
    assert sorted(path.name for path in tmp_path.iterdir()) == ["strips.tif", "whole.tif"]


@pytest.mark.parametrize("cache_mb", [2, 5, 12])
def test_reading_the_product_leaves_the_cache_to_the_picture(product, tmp_path, small_strips, cache_mb):
    # The product is compressed, so it is read through the cache. Styled straight
    # into the picture, its decompressed tiles pushed the picture's tiles out
    # early and moved them in the file; 2 MB is below the picture's own size,
    # where even the order of the writes shows in the bytes.
    kwargs = dict(value_range=VALUE_RANGE, gamma=GAMMA, settings=SETTINGS, metadata={})
    before = gdal.GetCacheMax()
    gdal.SetCacheMax(cache_mb * 1024 * 1024)
    try:
        _whole_grid_rgb_styled(source_path=str(product), output_path=tmp_path / "whole.tif", **kwargs)
        write_rgb_styled(source_path=str(product), output_path=tmp_path / "strips.tif", **kwargs)
    finally:
        gdal.SetCacheMax(before)

    assert (tmp_path / "strips.tif").read_bytes() == (tmp_path / "whole.tif").read_bytes()


def test_a_corrected_product_styles_its_first_three_bands(tmp_path, small_strips):
    product = _product(tmp_path / "surface.tif", _bands(7))
    kwargs = dict(value_range=VALUE_RANGE, gamma=1.0, settings=SETTINGS, metadata={})
    _whole_grid_rgb_styled(source_path=str(product), output_path=tmp_path / "whole.tif", **kwargs)
    write_rgb_styled(source_path=str(product), output_path=tmp_path / "strips.tif", **kwargs)

    assert (tmp_path / "strips.tif").read_bytes() == (tmp_path / "whole.tif").read_bytes()


def test_the_picture_never_holds_the_whole_grid(product, tmp_path, small_strips):
    tracemalloc.start()
    try:
        write_rgb_styled(source_path=str(product), output_path=tmp_path / "strips.tif",
                         value_range=VALUE_RANGE, gamma=GAMMA, settings=SETTINGS, metadata={})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # The whole-grid version held three float32 bands (12 B a cell) and the
    # RGB bytes beside them; one strip of 512 rows is under half the grid.
    assert peak < ROWS * COLUMNS * 6


@pytest.mark.parametrize(("value_range", "gamma", "message"), [
    ((0.6, 0.0), 0.6, "low < high"),
    ((0.0, 0.6), 0.0, "gamma must be positive"),
])
def test_a_bad_style_is_refused_before_anything_is_written(product, tmp_path, value_range, gamma, message):
    with pytest.raises(ValueError, match=message):
        write_rgb_styled(source_path=str(product), output_path=tmp_path / "bad.tif",
                         value_range=value_range, gamma=gamma, settings=SETTINGS, metadata={})
    assert not (tmp_path / "bad.tif").exists()


def test_a_product_of_one_row_still_gets_its_picture(tmp_path):
    bands = [band[:1, :50].copy() for band in _bands()]
    path = tmp_path / "thin.tif"
    write_rgb_product(path=str(path), bands=bands, transform=TRANSFORM, projection=_projection(),
                      settings={**SETTINGS, "overview_factors": []}, metadata={},
                      descriptions=["r", "g", "b"])
    write_rgb_styled(source_path=str(path), output_path=tmp_path / "thin_styled.tif",
                     value_range=VALUE_RANGE, gamma=GAMMA,
                     settings={**SETTINGS, "overview_factors": []}, metadata={})

    picture = gdal.Open(str(tmp_path / "thin_styled.tif")).ReadAsArray()
    assert picture.shape == (4, 1, 50)
    assert np.array_equal(np.moveaxis(picture[:3], 0, -1), rgb_bytes(bands, VALUE_RANGE, gamma=GAMMA))
