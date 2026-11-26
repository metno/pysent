"""A ``bbox`` read only inside its window.

With a bbox the geolocation is read whole, because it is what finds the window;
the science variable, the flag word, the extra-mask variables and, for the
Level-1 composites, the radiance, the detector index and the tie grids are then
read only inside that window instead of read whole and cut. On a 44-minute RR
segment that is most of the read.

The rule is the same as for every memory change here: **the same bytes**. So
the end-to-end tests run each product twice, once as it runs now and once with
the window withheld from the readers (``_SwathCrop.read_window`` forced to
``None``, which is exactly how the crop worked before: read whole, cut after),
and compare the written files byte for byte.
"""
import filecmp
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("osgeo")

from osgeo import gdal  # noqa: E402

from pysent import _olci_l1, _sen3  # noqa: E402
from pysent._sen3 import read_packed, read_window  # noqa: E402
from pysent.s3 import _SwathCrop, process_sentinel_s3_safe  # noqa: E402

# Inside the WFR test window (lat 54.59..55.52, lon 17.84..19.37), off every edge.
OLCI_BOX = (18.2, 54.8, 19.0, 55.3)
QUIET = {"styled": False, "thumbnail": False}


# --------------------------------------------------------------------------- #
# The readers
# --------------------------------------------------------------------------- #
def _member(tmp_path, values, *, nodata=None, scale=None, offset=None, name="v"):
    path = tmp_path / f"{name}.tif"
    dtype = {np.dtype("int16"): gdal.GDT_Int16, np.dtype("uint64"): gdal.GDT_UInt64,
             np.dtype("int32"): gdal.GDT_Int32}[values.dtype]
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), values.shape[1], values.shape[0], 1, dtype)
    band = dataset.GetRasterBand(1)
    band.WriteArray(values)
    if nodata is not None:
        band.SetNoDataValue(nodata)
    if scale is not None:
        band.SetScale(scale)
    if offset is not None:
        band.SetOffset(offset)
    dataset = None
    return gdal.Open(str(path))


WINDOW = (slice(7, 41), slice(3, 29))


def test_a_window_is_the_whole_reads_pixels_there(tmp_path):
    rng = np.random.default_rng(1)
    raw = rng.integers(-500, 500, size=(53, 37), dtype=np.int16)
    dataset = _member(tmp_path, raw)
    band = dataset.GetRasterBand(1)
    assert np.array_equal(read_window(band, window=WINDOW), raw[WINDOW])
    assert np.array_equal(read_window(band), raw)


def test_a_window_of_another_shape_cuts_the_resampled_read(tmp_path):
    """A flag word on another grid is resampled to the swath's shape, then cut."""
    rng = np.random.default_rng(2)
    raw = rng.integers(0, 2**40, size=(106, 74), dtype=np.uint64)
    dataset = _member(tmp_path, raw)
    band = dataset.GetRasterBand(1)
    whole = read_window(band, (53, 37))
    assert whole.shape == (53, 37)
    assert np.array_equal(read_window(band, (53, 37), WINDOW), whole[WINDOW])


def test_only_the_window_is_read_at_the_bands_own_size(tmp_path, monkeypatch):
    raw = np.arange(53 * 37, dtype=np.int32).reshape(53, 37)
    dataset = _member(tmp_path, raw)
    band = dataset.GetRasterBand(1)
    calls = []
    original = type(band).ReadAsArray

    def spy(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(band), "ReadAsArray", spy)
    read_window(band, (53, 37), WINDOW)
    assert calls == [((3, 7, 26, 34), {})]


@pytest.mark.parametrize("decimate", [1, 3])
def test_read_packed_in_a_window_is_bitwise_the_whole_read_cut(tmp_path, decimate):
    rng = np.random.default_rng(3)
    raw = rng.integers(-200, 200, size=(160, 111), dtype=np.int16)
    raw[::7, ::5] = -999
    dataset = _member(tmp_path, raw, nodata=-999, scale=0.013, offset=1.5)
    band = dataset.GetRasterBand(1)
    whole = read_packed(band, decimate=decimate, log10=True)
    window = (slice(5, 40), slice(2, 30))
    part = read_packed(band, decimate=decimate, log10=True, window=window)
    assert part.dtype == np.float32
    assert part.tobytes() == whole[window].tobytes()


def test_a_tie_grid_in_a_window_is_the_whole_expansion_cut(tmp_path, monkeypatch):
    rng = np.random.default_rng(4)
    tie = (rng.random((60, 9)) * 80).astype(np.float32)
    path = tmp_path / "tie.tif"
    created = gdal.GetDriverByName("GTiff").Create(str(path), 9, 60, 1, gdal.GDT_Float32)
    created.GetRasterBand(1).WriteArray(tie)
    created = None
    monkeypatch.setattr(_olci_l1, "extract_member", lambda root, member, scratch: str(path))
    monkeypatch.setattr(_olci_l1, "open_sen3_variable", lambda local, variable: gdal.Open(local))

    whole = _olci_l1.tie_point_field("root", "m", "v", tmp_path, (60, 500))
    window = (slice(11, 47), slice(123, 391))
    part = _olci_l1.tie_point_field("root", "m", "v", tmp_path, (60, 500), window)
    assert part.tobytes() == np.ascontiguousarray(whole[window]).tobytes()


def test_a_band_of_another_size_than_the_geolocation_is_refused_in_a_window(tmp_path, monkeypatch):
    raw = np.zeros((40, 30), dtype=np.int16)
    _member(tmp_path, raw, name="radiance")
    monkeypatch.setattr(_olci_l1, "extract_member", lambda root, member, scratch: str(tmp_path / "radiance.tif"))
    monkeypatch.setattr(_olci_l1, "open_sen3_variable", lambda local, variable: gdal.Open(local))
    with pytest.raises(ValueError, match="geolocation"):
        _olci_l1.toa_reflectance("root", 8, tmp_path, window=(slice(0, 5), slice(0, 5)),
                                 image_shape=(41, 30))


# --------------------------------------------------------------------------- #
# End to end: the same bytes as reading whole and cutting after
# --------------------------------------------------------------------------- #
def _files(directory: Path) -> dict[str, Path]:
    return {path.name: path for path in sorted(directory.rglob("*")) if path.is_file()}


def _run_both(source, products, options, tmp_path, monkeypatch):
    windowed = process_sentinel_s3_safe(
        input_dataset=str(source), output_dir=tmp_path / "windowed", products=products,
        processing_options=options,
    )
    with monkeypatch.context() as patch:
        patch.setattr(_SwathCrop, "read_window", property(lambda self: None))
        whole = process_sentinel_s3_safe(
            input_dataset=str(source), output_dir=tmp_path / "whole", products=products,
            processing_options=options,
        )
    first, second = _files(tmp_path / "windowed"), _files(tmp_path / "whole")
    assert first.keys() == second.keys() and first
    for name in first:
        assert filecmp.cmp(first[name], second[name], shallow=False), name
    return windowed, whole


def _spy_reads(monkeypatch):
    """The (shape, window) of every raw read the readers make."""
    seen = []
    original = _sen3.read_window

    def spy(band, shape=None, window=None):
        seen.append(((band.YSize, band.XSize), window))
        return original(band, shape, window)

    for module in ("pysent._sen3", "pysent.s3", "pysent._olci_l1"):
        monkeypatch.setattr(f"{module}.read_window", spy)
    return seen


@pytest.mark.parametrize("styled", [False, True])
def test_an_olci_swath_with_a_bbox_writes_the_same_bytes(s3_olci_window, tmp_path, monkeypatch, styled):
    options = {"styled": styled, "thumbnail": styled, "bbox": list(OLCI_BOX)}
    windowed, _ = _run_both(s3_olci_window, {"chl": {}}, options, tmp_path, monkeypatch)
    rows0, rows1, columns0, columns1 = windowed[0]["source_window"]
    assert (rows1 - rows0) * (columns1 - columns0) < 320 * 320 // 2


def test_with_a_bbox_only_the_geolocation_is_read_whole(s3_olci_window, tmp_path, monkeypatch):
    seen = _spy_reads(monkeypatch)
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_olci_window), output_dir=tmp_path, products={"chl": {}},
        processing_options={**QUIET, "bbox": list(OLCI_BOX)},
    )[0]
    rows0, rows1, columns0, columns1 = result["source_window"]
    # read_packed (values and the coordinates) and the flag word all come here.
    windows = [window for _shape, window in seen]
    assert windows.count(None) == 2, "latitude and longitude, whole"
    expected = (slice(rows0, rows1), slice(columns0, columns1))
    assert [window for window in windows if window is not None] == [expected, expected]


def test_without_a_bbox_nothing_is_read_in_a_window(s3_olci_window, tmp_path, monkeypatch):
    seen = _spy_reads(monkeypatch)
    process_sentinel_s3_safe(
        input_dataset=str(s3_olci_window), output_dir=tmp_path, products={"chl": {}},
        processing_options=QUIET,
    )
    assert seen and all(window is None for _shape, window in seen)


def test_a_bbox_with_nothing_inside_still_reads_whole(s3_olci_window, tmp_path, monkeypatch):
    seen = _spy_reads(monkeypatch)
    process_sentinel_s3_safe(
        input_dataset=str(s3_olci_window), output_dir=tmp_path, products={"chl": {}},
        processing_options={**QUIET, "bbox": [-30, -10, -20, 0], "on_empty": "write"},
    )
    assert all(window is None for _shape, window in seen)


def test_a_decimated_swath_with_a_bbox_writes_the_same_bytes(s3_olci_window, tmp_path, monkeypatch):
    _run_both(s3_olci_window, {"chl": {"decimate": 2}}, {**QUIET, "bbox": list(OLCI_BOX)},
              tmp_path, monkeypatch)


def test_an_slstr_product_with_extra_masks_writes_the_same_bytes(s3_slstr_lst_window, tmp_path, monkeypatch):
    whole = process_sentinel_s3_safe(
        input_dataset=str(s3_slstr_lst_window), output_dir=tmp_path / "probe", products={"lst": {}},
        processing_options=QUIET,
    )[0]
    west, south, east, north = whole["bounds"]
    box = [west + (east - west) / 4, south + (north - south) / 4,
           east - (east - west) / 4, north - (north - south) / 4]
    seen = _spy_reads(monkeypatch)
    _run_both(s3_slstr_lst_window, {"lst": {}}, {**QUIET, "bbox": box}, tmp_path, monkeypatch)
    windowed = [window for _shape, window in seen if window is not None]
    # science, flag word and the exception word of the extra mask
    assert len(windowed) >= 3


def test_an_sst_product_with_a_bbox_writes_the_same_bytes(s3_slstr_sst_window, tmp_path, monkeypatch):
    probe = process_sentinel_s3_safe(
        input_dataset=str(s3_slstr_sst_window), output_dir=tmp_path / "probe", products={"sst": {}},
        processing_options=QUIET,
    )[0]
    west, south, east, north = probe["bounds"]
    box = [west + (east - west) / 3, south + (north - south) / 3, east, north]
    _run_both(s3_slstr_sst_window, {"sst": {}}, {**QUIET, "bbox": box}, tmp_path, monkeypatch)


def test_true_colour_with_a_bbox_writes_the_same_bytes(olci_l1_archive, tmp_path, monkeypatch):
    """Needs a real Level-1 frame (``PYSENT_OLCI_ARCHIVE``): no window of one is committed."""
    source = sorted(olci_l1_archive.glob("*OL_1_E*R*.zip"))[0]
    probe = process_sentinel_s3_safe(
        input_dataset=str(source), output_dir=tmp_path / "probe", products={"truecolor": {}},
        processing_options={"preset": "quicklook", **QUIET},
    )[0]
    west, south, east, north = probe["bounds"]
    box = [west + (east - west) / 3, south + (north - south) / 3,
           east - (east - west) / 3, north - (north - south) / 3]
    _run_both(source, {"truecolor": {}}, {"bbox": box, "gdal_cachemax_mb": 256}, tmp_path, monkeypatch)
