"""The styled copy and the materialised thumbnail are built in row strips.

Both used to read the whole grid and build full-size float64 and int64
temporaries from it; on a full-resolution OLCI frame (45.8 M cells) that took a
run from 1.2 GB to 2.8 GB after the warp had finished. Working a strip at a
time keeps the arithmetic - float64, clip, rint - exactly as it was, so every
byte of every output must stay the same. The reference below is the whole-grid
version these functions replaced, kept here only to prove that.
"""
from pathlib import Path
import tracemalloc

import numpy as np
import pytest

gdal = pytest.importorskip("osgeo.gdal")

from pysent import _output  # noqa: E402
from pysent._output import (  # noqa: E402
    _matches_codes,
    colour_table,
    creation_options,
    styled_bytes,
    write_styled_copy,
    write_thumbnail,
)

gdal.UseExceptions()

SETTINGS = {
    "compression": "DEFLATE",
    "predictor": 3,
    "gdal_num_threads": "2",
    "overview_factors": [2, 4, 8],
}
HIDDEN = (3, 7)
VALUE_RANGE = (0.05, 30.0)


def _float_product(path: Path, rows: int = 1100, columns: int = 700) -> Path:
    """A two-band product like a swath one: values and reject reasons, NaN off the swath."""
    rng = np.random.default_rng(14)
    values = rng.lognormal(mean=0.0, sigma=2.0, size=(rows, columns)).astype(np.float32)
    values[rng.random((rows, columns)) < 0.05] = -0.3  # below the range: clipped, not dropped
    values[rng.random((rows, columns)) < 0.05] = 500.0  # above it
    codes = rng.integers(0, 9, size=(rows, columns)).astype(np.float32)
    # Off the swath both bands carry the warp's NaN.
    yy, xx = np.mgrid[:rows, :columns]
    outside = xx > columns * 0.8 + (yy - rows / 2) * 0.3
    values[outside] = np.nan
    codes[outside] = np.nan
    values[(codes == 0) & ~outside] = np.nan  # code 0: a pixel the mask removed

    dataset = gdal.GetDriverByName("GTiff").Create(
        str(path), columns, rows, 2, gdal.GDT_Float32,
        options=creation_options(SETTINGS, float_data=True),
    )
    dataset.SetGeoTransform((500000.0, 300.0, 0.0, 7000000.0, 0.0, -300.0))
    srs = gdal.osr.SpatialReference()
    srs.ImportFromEPSG(32633)
    dataset.SetProjection(srs.ExportToWkt())
    for index, array in enumerate((values, codes)):
        band = dataset.GetRasterBand(index + 1)
        band.SetNoDataValue(float("nan"))
        band.WriteArray(array)
    dataset.FlushCache()
    dataset = None
    return path


def _whole_grid_styled_copy(*, source_path, output_path, value_range, colormap, settings, metadata,
                            log_scale=False, hide_codes=(), reason_band=None):
    """``write_styled_copy`` as it was: one read of the whole grid."""
    source = gdal.Open(source_path)
    values = source.GetRasterBand(1).ReadAsArray()
    if hide_codes and reason_band and source.RasterCount >= reason_band:
        codes = source.GetRasterBand(reason_band).ReadAsArray()
        values = np.where(_matches_codes(codes, hide_codes), np.nan, values)
    styled = styled_bytes(values, value_range, log_scale=log_scale)
    dataset = gdal.GetDriverByName("GTiff").Create(
        str(output_path), styled.shape[1], styled.shape[0], 1, gdal.GDT_Byte,
        options=creation_options(settings, float_data=False),
    )
    dataset.SetGeoTransform(source.GetGeoTransform())
    dataset.SetProjection(source.GetProjection())
    band = dataset.GetRasterBand(1)
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


def _whole_grid_thumbnail(*, source_path, output_path, value_range, colormap, log_scale=False,
                          hide_codes=(), reason_band=None):
    """``write_thumbnail(materialise=True)`` as it was: the MEM copy written in one go."""
    source = gdal.Open(source_path)
    width, height = source.RasterXSize, source.RasterYSize
    scale = min(1.0, _output.THUMBNAIL_PIXELS / max(width, height))
    target_width, target_height = max(1, round(width * scale)), max(1, round(height * scale))
    transform = source.GetGeoTransform()
    bounds = (transform[0], transform[3] + transform[5] * height,
              transform[0] + transform[1] * width, transform[3])
    full = source.GetRasterBand(1).ReadAsArray()
    if hide_codes and reason_band and source.RasterCount >= reason_band:
        codes = source.GetRasterBand(reason_band).ReadAsArray()
        full = np.where(_matches_codes(codes, hide_codes), np.nan, full)
    materialised = gdal.GetDriverByName("MEM").Create("", width, height, 1, gdal.GDT_Float32)
    materialised.SetGeoTransform(source.GetGeoTransform())
    materialised.SetProjection(source.GetProjection())
    band = materialised.GetRasterBand(1)
    band.SetNoDataValue(float("nan"))
    band.WriteArray(full)
    resampled = gdal.Warp("/vsimem/reference_thumbnail.tif", materialised, format="GTiff",
                          width=target_width, height=target_height, outputBounds=bounds,
                          dstSRS=materialised.GetProjection(), resampleAlg="average",
                          dstNodata=float("nan"), outputType=gdal.GDT_Float32)
    values = resampled.GetRasterBand(1).ReadAsArray()
    resampled = None
    gdal.Unlink("/vsimem/reference_thumbnail.tif")
    styled = styled_bytes(values, value_range, log_scale=log_scale)
    memory = gdal.GetDriverByName("MEM").Create("", styled.shape[1], styled.shape[0], 1, gdal.GDT_Byte)
    band = memory.GetRasterBand(1)
    band.WriteArray(styled)
    band.SetNoDataValue(0)
    band.SetRasterColorTable(colour_table(colormap))
    gdal.GetDriverByName("PNG").CreateCopy(str(output_path), memory)


@pytest.fixture(scope="module")
def product(tmp_path_factory):
    return _float_product(tmp_path_factory.mktemp("strips") / "product.tif")


@pytest.fixture
def small_strips(monkeypatch):
    # Far smaller than the product, so it takes several strips and a short last one.
    monkeypatch.setattr(_output, "STRIP_CELLS", 700 * 300)


@pytest.mark.parametrize("log_scale", [False, True])
@pytest.mark.parametrize("hide", [True, False])
def test_the_styled_copy_is_byte_identical_to_the_whole_grid_one(product, tmp_path, small_strips,
                                                                 log_scale, hide):
    kwargs = dict(value_range=VALUE_RANGE, colormap="viridis", settings=SETTINGS,
                  metadata={"PYSENT_PRODUCT": "chl"}, log_scale=log_scale,
                  hide_codes=HIDDEN if hide else (), reason_band=2 if hide else None)
    _whole_grid_styled_copy(source_path=str(product), output_path=tmp_path / "whole.tif", **kwargs)
    write_styled_copy(source_path=str(product), output_path=tmp_path / "strips.tif", **kwargs)

    assert (tmp_path / "strips.tif").read_bytes() == (tmp_path / "whole.tif").read_bytes()


def test_the_strips_cover_every_row_once(product, tmp_path, small_strips):
    write_styled_copy(source_path=str(product), output_path=tmp_path / "strips.tif",
                      value_range=VALUE_RANGE, colormap="viridis", settings=SETTINGS, metadata={},
                      hide_codes=HIDDEN, reason_band=2)
    source = gdal.Open(str(product))
    values = source.GetRasterBand(1).ReadAsArray()
    codes = source.GetRasterBand(2).ReadAsArray()
    expected = styled_bytes(np.where(_matches_codes(codes, HIDDEN), np.nan, values), VALUE_RANGE)

    assert np.array_equal(gdal.Open(str(tmp_path / "strips.tif")).ReadAsArray(), expected)


def test_a_strip_is_whole_tile_rows(product, tmp_path, monkeypatch):
    # A strip that ended inside a row of tiles would leave those tiles half
    # written, and a tile flushed half written is rewritten at a new offset: the
    # pixels survive, the file's bytes do not. So a strip is rounded to the
    # output's block height, whatever STRIP_CELLS asks for.
    monkeypatch.setattr(_output, "STRIP_CELLS", 1)
    seen = []
    original = _output._row_strips

    def recording(height, width, block_rows):
        for offset, rows in original(height, width, block_rows):
            seen.append((offset, rows))
            yield offset, rows

    monkeypatch.setattr(_output, "_row_strips", recording)
    write_styled_copy(source_path=str(product), output_path=tmp_path / "strips.tif",
                      value_range=VALUE_RANGE, colormap="viridis", settings=SETTINGS, metadata={})
    height = gdal.Open(str(product)).RasterYSize

    assert all(offset % 256 == 0 for offset, _ in seen)
    assert sum(rows for _, rows in seen) == height
    assert seen[-1][0] + seen[-1][1] == height


def test_the_styled_copy_never_holds_the_whole_grid(product, tmp_path, small_strips):
    source = gdal.Open(str(product))
    cells = source.RasterXSize * source.RasterYSize
    source = None

    tracemalloc.start()
    try:
        write_styled_copy(source_path=str(product), output_path=tmp_path / "strips.tif",
                          value_range=VALUE_RANGE, colormap="viridis", settings=SETTINGS, metadata={},
                          hide_codes=HIDDEN, reason_band=2)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # The whole-grid version held a float64 copy (8 B a cell) and more; one
    # strip of 768 rows here is 70 % of the grid at most, and the temporaries of
    # a strip come to about 25 B a cell.
    assert peak < cells * 8


@pytest.mark.parametrize("hide", [True, False])
def test_the_materialised_thumbnail_is_byte_identical(product, tmp_path, small_strips, hide):
    kwargs = dict(value_range=VALUE_RANGE, colormap="magma", log_scale=True,
                  hide_codes=HIDDEN if hide else (), reason_band=2 if hide else None)
    _whole_grid_thumbnail(source_path=str(product), output_path=tmp_path / "whole.png", **kwargs)
    write_thumbnail(source_path=str(product), output_path=tmp_path / "strips.png", materialise=True, **kwargs)

    assert (tmp_path / "strips.png").read_bytes() == (tmp_path / "whole.png").read_bytes()


def test_a_product_of_one_row_still_gets_its_styled_pair(tmp_path):
    product = _float_product(tmp_path / "thin.tif", rows=1, columns=50)
    write_styled_copy(source_path=str(product), output_path=tmp_path / "thin_styled.tif",
                      value_range=VALUE_RANGE, colormap="viridis",
                      settings={**SETTINGS, "overview_factors": []}, metadata={},
                      hide_codes=HIDDEN, reason_band=2)
    write_thumbnail(source_path=str(product), output_path=tmp_path / "thin_thumb.png",
                    value_range=VALUE_RANGE, colormap="viridis", materialise=True)

    assert gdal.Open(str(tmp_path / "thin_styled.tif")).RasterYSize == 1
    assert gdal.Open(str(tmp_path / "thin_thumb.png")).RasterXSize == 50
