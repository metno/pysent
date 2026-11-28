"""Sentinel-2 normalised-difference indices as one-band VRTs: GDAL's ``norm_diff`` over the raw bands, coloured by a ramp.

The bands are the committed window of a real scene (B4, B3), a near-infrared
band derived from them and four 20 m bands (B5, B8A, B11, B12) derived from
those at half the size, written as files the SAFE lookups return; the product
metadata is a real-shaped ``MTD_MSIL2A.xml`` in a SAFE directory, so the
offsets come through :func:`pysent.s2.read_sentinel_s2_radiometry`. Every
value GDAL reads out of the VRT is compared with numpy's index of the same
pixels.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

gdal = pytest.importorskip("osgeo.gdal")

from pysent import s2 as s2_module  # noqa: E402
from pysent import vrt as vrt_module  # noqa: E402
from pysent.vrt import (  # noqa: E402
    S2_INDEX_REFLECTANCE_FLOOR,
    S2_INDEX_RAMPS,
    S2_INDICES,
    build_sentinel_s2_index_vrt,
    sentinel_s2_index_palette,
)

gdal.UseExceptions()

_BANDS = ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B9", "B10", "B11", "B12"]


def _write(path: Path, data: np.ndarray, geotransform, wkt: str) -> Path:
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), data.shape[1], data.shape[0], 1, gdal.GDT_UInt16)
    dataset.SetGeoTransform(geotransform)
    dataset.SetProjection(wkt)
    dataset.GetRasterBand(1).WriteArray(data)
    dataset = None
    return path


def _metadata(safe: Path, baseline: str) -> None:
    spectral = "".join(
        f'<Spectral_Information bandId="{index}" physicalBand="{name}"><RESOLUTION>10</RESOLUTION></Spectral_Information>'
        for index, name in enumerate(_BANDS)
    )
    offsets = ""
    if baseline >= "04.00":
        offsets = "".join(f'<BOA_ADD_OFFSET band_id="{index}">-1000</BOA_ADD_OFFSET>' for index in range(13))
    (safe / "MTD_MSIL2A.xml").write_text(
        "<n1:Level-2A_User_Product xmlns:n1='x'><General_Info><Product_Info>"
        f"<PROCESSING_LEVEL>Level-2A</PROCESSING_LEVEL><PROCESSING_BASELINE>{baseline}</PROCESSING_BASELINE>"
        "</Product_Info><Product_Image_Characteristics><QUANTIFICATION_VALUES_LIST>"
        '<BOA_QUANTIFICATION_VALUE unit="none">10000</BOA_QUANTIFICATION_VALUE></QUANTIFICATION_VALUES_LIST>'
        f"{offsets}<Spectral_Information_List>{spectral}</Spectral_Information_List>"
        "</Product_Image_Characteristics></General_Info></n1:Level-2A_User_Product>"
    )


def _make_scene(tmp_path: Path, s2_rgb: Path, monkeypatch, baseline: str = "05.13") -> dict[str, object]:
    source = gdal.Open(str(s2_rgb))
    geotransform, wkt = source.GetGeoTransform(), source.GetProjectionRef()
    rgb = source.ReadAsArray().astype(np.int64)
    red, green = rgb[0].copy(), rgb[1].copy()
    nir = np.where(red > 0, 0.4 * red + 0.9 * green + 300, 0)
    red[:16, :] = 0  # NoData in one band only: the index is NoData there
    nir[:, :8] = 0
    red[100:140, 100:180] = 600  # below the -1000 offset: reflectance held at 0
    nir[120:160, 150:220] = 900
    nir[300:310, 300:310] = red[300:310, 300:310] = green[300:310, 300:310] = 1000  # both exactly 0 reflectance: index 0, not infinity
    # The 20 m bands: every other pixel of the 10 m ones, each with NoData and dark patches of its own.
    coarse = {
        "B5": 0.8 * red[::2, ::2] + 0.3 * green[::2, ::2] + 150,
        "B8A": 0.5 * red[::2, ::2] + 0.8 * green[::2, ::2] + 400,
        "B11": 1.1 * red[::2, ::2] + 0.2 * green[::2, ::2] + 50,
        "B12": 0.9 * red[::2, ::2] + 100,
    }
    for data in coarse.values():
        data[(red[::2, ::2] == 0) | (green[::2, ::2] == 0)] = 0
    coarse["B11"][:, -6:] = 0
    coarse["B5"][-5:, :] = 0
    coarse["B11"][20:40, 30:60] = 700
    coarse["B12"][40:60, 50:90] = 800
    for data in coarse.values():
        data[150:155, 150:155] = 1000  # with the 10 m bands' patch: every band exactly 0 reflectance
    coarse_geotransform = (geotransform[0], 2 * geotransform[1], geotransform[2],
                           geotransform[3], geotransform[4], 2 * geotransform[5])
    safe = tmp_path / "S2X_MSIL2A_TEST.SAFE"
    safe.mkdir()
    _metadata(safe, baseline)
    sources, arrays = {}, {"B4": red, "B3": green, "B8": nir, **coarse}
    for name, data in arrays.items():
        resolution = 20.0 if name in coarse else 10.0
        path = _write(tmp_path / f"T35_{name}.tif", np.rint(data).astype(np.uint16),
                      coarse_geotransform if name in coarse else geotransform, wkt)
        sources[name] = {"subdataset_name": str(path), "band_index": 1, "resolution": resolution, "epsg": "EPSG:32635"}
    monkeypatch.setattr(s2_module, "_collect_sentinel_s2_band_sources", lambda _input: dict(sources))
    monkeypatch.setattr(vrt_module, "_s2_band_file", lambda subdataset, _band: subdataset)
    return {"input": str(safe), "arrays": {k: np.rint(v).astype(np.float64) for k, v in arrays.items()}}


@pytest.fixture
def scene(tmp_path, s2_rgb, monkeypatch):
    return _make_scene(tmp_path, s2_rgb, monkeypatch)


def _pair(scene, name: str) -> tuple[np.ndarray, np.ndarray]:
    """The index's two bands on its grid, the finer band's: a 20 m band beside a 10 m one is read at 10 m, nearest."""
    a, b = (scene["arrays"][band] for band in S2_INDICES[name])
    size = max(a.shape[0], b.shape[0])
    return tuple(np.repeat(np.repeat(x, size // x.shape[0], axis=0), size // x.shape[1], axis=1) for x in (a, b))


def _reference(a: np.ndarray, b: np.ndarray, offset: float) -> np.ndarray:
    floor = S2_INDEX_REFLECTANCE_FLOOR
    ra, rb = np.maximum(a + offset, floor), np.maximum(b + offset, floor)
    index = (ra - rb) / (ra + rb)
    index[(a == 0) | (b == 0)] = np.nan
    return index


def _encode(index: np.ndarray) -> np.ndarray:
    """The byte for each index value; GDAL rounds halves up."""
    return np.where(np.isnan(index), 0, np.floor(1 + (index + 1) / 2 * 254 + 0.5)).astype(np.uint8)


@pytest.mark.parametrize("name", sorted(S2_INDICES))
def test_the_index_values_are_numpys_on_reflectance(tmp_path, scene, name):
    result = build_sentinel_s2_index_vrt(
        input_dataset=scene["input"], index=name, output_path=tmp_path / f"{name}.vrt", processing_options={"values": True},
    )
    first, second = S2_INDICES[name]
    assert result["bands"] == [first, second] and result["offsets"] == {first: -1000.0, second: -1000.0}
    expected = _reference(*_pair(scene, name), -1000.0)
    dataset = gdal.Open(result["path"])
    band = dataset.GetRasterBand(1)
    assert band.DataType == gdal.GDT_Float32 and np.isnan(band.GetNoDataValue()) and band.GetDescription() == name
    values = band.ReadAsArray().astype(np.float64)
    np.testing.assert_array_equal(np.isnan(values), np.isnan(expected))
    np.testing.assert_allclose(values, expected, atol=1e-6, equal_nan=True)
    assert np.nanmin(values) >= -1.0 and np.nanmax(values) <= 1.0
    assert np.isfinite(values[~np.isnan(values)]).all()  # never infinity
    if "B8" in (first, second):
        assert np.isnan(values[:, :8]).all()  # B8 NoData there
    patch = slice(300, 310) if values.shape[0] == 512 else slice(150, 155)
    np.testing.assert_array_equal(values[patch, patch], 0.0)
    assert dataset.GetMetadataItem("PYSENT_INDEX") == name
    assert dataset.GetGeoTransform() == gdal.Open(result["sources"][0]).GetGeoTransform()


@pytest.mark.parametrize("name", sorted(S2_INDICES))
def test_the_coloured_index_is_the_encoded_value_with_the_ramps_colours(tmp_path, scene, name):
    result = build_sentinel_s2_index_vrt(input_dataset=scene["input"], index=name, output_path=tmp_path / f"{name}.vrt")
    expected = _reference(*_pair(scene, name), -1000.0)
    dataset = gdal.Open(result["path"])
    band = dataset.GetRasterBand(1)
    assert band.DataType == gdal.GDT_Byte and band.GetNoDataValue() == 0
    assert band.GetColorInterpretation() == gdal.GCI_PaletteIndex
    coloured = band.ReadAsArray().astype(int)
    # The byte is the Float32 index encoded. Where the encoding lands on a
    # rounding edge (x.5 within float precision) GDAL's arithmetic and numpy's
    # may pick the two neighbouring bytes.
    values = build_sentinel_s2_index_vrt(
        input_dataset=scene["input"], index=name, output_path=tmp_path / "v.vrt", processing_options={"values": True},
    )
    index = gdal.Open(values["path"]).ReadAsArray().astype(np.float64)
    gap = np.abs(coloured - _encode(index).astype(int))
    edge = np.abs((1 + (index + 1) / 2 * 254) % 1 - 0.5) < 1e-4
    assert gap.max() <= 1 and not (gap[~edge] > 0).any()
    assert (np.abs(coloured - _encode(expected).astype(int)) <= 1).all()
    table = band.GetColorTable()
    palette = sentinel_s2_index_palette(S2_INDEX_RAMPS[name])
    assert table.GetCount() == 256 and table.GetColorEntry(0)[3] == 0
    assert [tuple(table.GetColorEntry(i)) for i in range(256)] == [tuple(int(c) for c in row) for row in palette]
    assert result["ramp"] == [list(stop) for stop in S2_INDEX_RAMPS[name]]
    assert result["encoding"] == {"nodata": 0, "min_value": 1, "max_value": 255, "min_index": -1.0, "max_index": 1.0}


def test_the_table_keeps_ndvi_and_ndwi_first_then_the_five_in_order():
    """A map server lists one layer per index in this order (mapping-api's ``INDEX_LAYERS``)."""
    assert list(S2_INDICES) == ["ndvi", "ndwi", "ndsi", "mndwi", "ndmi", "ndre", "nbr"]
    assert S2_INDICES["ndvi"] == ("B8", "B4") and S2_INDICES["ndwi"] == ("B3", "B8")
    assert list(S2_INDEX_RAMPS) == list(S2_INDICES)


@pytest.mark.parametrize(
    ("name", "resolution", "size"),
    [("ndvi", 10.0, 512), ("ndwi", 10.0, 512), ("ndsi", 10.0, 512), ("mndwi", 10.0, 512),
     ("ndmi", 10.0, 512), ("ndre", 20.0, 256), ("nbr", 10.0, 512)],
)
@pytest.mark.parametrize("values", [False, True])
def test_each_index_lands_on_its_finer_bands_grid(tmp_path, scene, name, resolution, size, values):
    result = build_sentinel_s2_index_vrt(
        input_dataset=scene["input"], index=name, output_path=tmp_path / f"{name}.vrt",
        processing_options={"values": values},
    )
    assert result["resolution"] == resolution and (result["width"], result["height"]) == (size, size)
    dataset = gdal.Open(result["path"])
    assert (dataset.RasterXSize, dataset.RasterYSize) == (size, size)
    assert dataset.GetGeoTransform()[1] == resolution
    assert float(dataset.GetMetadataItem("PYSENT_TARGET_RESOLUTION")) == resolution
    first, second = S2_INDICES[name]
    assert dataset.GetMetadataItem("PYSENT_INDEX_FORMULA") == f"({first} - {second}) / ({first} + {second})"


@pytest.mark.parametrize("values", [False, True])
def test_ndsi_and_mndwi_read_the_same_values(tmp_path, scene, values):
    """The same formula under two names (snow, water): identical pixels, only the colours differ."""
    read = {}
    for name in ("ndsi", "mndwi"):
        result = build_sentinel_s2_index_vrt(
            input_dataset=scene["input"], index=name, output_path=tmp_path / f"{name}.vrt",
            processing_options={"values": values},
        )
        read[name] = gdal.Open(result["path"]).ReadAsArray()
    np.testing.assert_array_equal(read["ndsi"], read["mndwi"])
    assert S2_INDEX_RAMPS["ndsi"] != S2_INDEX_RAMPS["mndwi"]


def test_a_zoomed_out_read_is_the_index_of_the_pixels_it_picks(tmp_path, scene):
    """A map server reads the VRT decimated: each output pixel is the index of the source pixels picked."""
    values = build_sentinel_s2_index_vrt(
        input_dataset=scene["input"], index="ndvi", output_path=tmp_path / "v.vrt", processing_options={"values": True},
    )
    small = gdal.Open(values["path"]).ReadAsArray(buf_xsize=64, buf_ysize=64).astype(np.float64)
    a, b = (gdal.Open(path).ReadAsArray(buf_xsize=64, buf_ysize=64).astype(np.float64) for path in values["sources"])
    np.testing.assert_allclose(small, _reference(a, b, -1000.0), atol=1e-6, equal_nan=True)
    coloured = build_sentinel_s2_index_vrt(input_dataset=scene["input"], index="ndvi", output_path=tmp_path / "c.vrt")
    np.testing.assert_array_equal(gdal.Open(coloured["path"]).ReadAsArray(buf_xsize=64, buf_ysize=64), _encode(small))


def test_a_product_before_baseline_04_has_no_offset(tmp_path, s2_rgb, monkeypatch):
    scene = _make_scene(tmp_path, s2_rgb, monkeypatch, baseline="03.01")
    result = build_sentinel_s2_index_vrt(
        input_dataset=scene["input"], index="ndvi", output_path=tmp_path / "v.vrt", processing_options={"values": True},
    )
    assert result["offsets"] == {"B8": 0.0, "B4": 0.0}
    expected = _reference(scene["arrays"]["B8"], scene["arrays"]["B4"], 0.0)
    np.testing.assert_allclose(gdal.Open(result["path"]).ReadAsArray(), expected, atol=1e-6, equal_nan=True)


def test_the_offset_changes_the_index(tmp_path, scene):
    """On raw DN the index is wrong: the offset is applied (the reason the stretched bands cannot be used)."""
    result = build_sentinel_s2_index_vrt(
        input_dataset=scene["input"], index="ndvi", output_path=tmp_path / "v.vrt", processing_options={"values": True},
    )
    values = gdal.Open(result["path"]).ReadAsArray().astype(np.float64)
    raw = _reference(scene["arrays"]["B8"], scene["arrays"]["B4"], 0.0)
    assert np.nanmax(np.abs(values - raw)) > 0.05


def test_a_ramp_given_replaces_the_default(tmp_path, scene):
    ramp = [(-1.0, "#000000"), (1.0, "#ffffff")]
    result = build_sentinel_s2_index_vrt(
        input_dataset=scene["input"], index="ndwi", output_path=tmp_path / "c.vrt", processing_options={"ramp": ramp},
    )
    dataset = gdal.Open(result["path"])
    table = dataset.GetRasterBand(1).GetColorTable()
    assert table.GetColorEntry(1) == (0, 0, 0, 255) and table.GetColorEntry(255) == (255, 255, 255, 255)
    assert table.GetColorEntry(128) == (128, 128, 128, 255)
    assert result["ramp"] == [[-1.0, "#000000"], [1.0, "#ffffff"]]


def test_the_palette_hits_each_stop_and_holds_beyond_the_ends():
    palette = sentinel_s2_index_palette([(0.0, "#ff0000"), (1.0, "#0000ff")])
    assert tuple(palette[0]) == (0, 0, 0, 0)
    assert tuple(palette[1]) == (255, 0, 0, 255)  # -1: below the first stop
    assert tuple(palette[128]) == (255, 0, 0, 255)  # index 0 exactly
    assert tuple(palette[255]) == (0, 0, 255, 255)
    for ramp in S2_INDEX_RAMPS.values():
        values = [value for value, _ in ramp]
        assert values == sorted(values) and values[0] == -1.0 and values[-1] == 1.0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"index": "evi"}, "unknown Sentinel-2 index"),
        ({"processing_options": {"cloud_mask": True}}, "cloud_mask"),
        ({"processing_options": {"stretch_gamma": 1.0}}, "unknown processing options"),
        ({"processing_options": {"ramp": [(0, "green")]}}, "#rrggbb"),
        ({"processing_options": {"ramp": [(0, "#00ff00")]}}, "two stops"),
        ({"input_dataset": "S2_tile.nc"}, "netCDF"),
        ({"output_path": "index.tif"}, ".vrt"),
    ],
)
def test_the_index_vrt_refuses_what_it_cannot_express(tmp_path, scene, kwargs, message):
    arguments = {"input_dataset": scene["input"], "index": "ndvi", "output_path": tmp_path / "index.vrt", **kwargs}
    with pytest.raises(ValueError, match=message):
        build_sentinel_s2_index_vrt(**arguments)
    assert not (tmp_path / "index.vrt").exists()


def test_the_index_name_is_case_blind(tmp_path, scene):
    result = build_sentinel_s2_index_vrt(input_dataset=scene["input"], index="NDVI", output_path=tmp_path / "c.vrt")
    assert result["index"] == "ndvi"
