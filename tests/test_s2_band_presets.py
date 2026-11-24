"""Sentinel-2 band combinations: one all-bands VRT per scene, presets as band selections.

The scene is built from the committed window of a real scene, as in
``test_vrt.py``: B4/B3/B2 as they are, the other bands derived from them at
their own resolutions (10, 20 and 60 m) with different brightness, so that each
band gets its own stretch. The SAFE lookups return those files. Every preset
read out of the all-bands VRT is compared, pixel for pixel, with the VRT
:func:`build_sentinel_s2_vrt` writes for that preset alone.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("rasterio")
gdal = pytest.importorskip("osgeo.gdal")

from pysent import s2 as s2_module  # noqa: E402
from pysent import vrt as vrt_module  # noqa: E402
from pysent.errors import EmptySceneError  # noqa: E402
from pysent.s2 import S2_BAND_PRESETS, S2_SUPPORTED_BANDS, resolve_sentinel_s2_preset  # noqa: E402
from pysent.vrt import (  # noqa: E402
    build_sentinel_s2_all_bands_vrt,
    build_sentinel_s2_vrt,
    sentinel_s2_vrt_band_indices,
)

gdal.UseExceptions()

L1C_BANDS = [band for band in S2_SUPPORTED_BANDS]
L2A_BANDS = [band for band in S2_SUPPORTED_BANDS if band != "B10"]
_RESOLUTION = {"B2": 10, "B3": 10, "B4": 10, "B8": 10, "B5": 20, "B6": 20, "B7": 20, "B8A": 20, "B11": 20,
               "B12": 20, "B1": 60, "B9": 60, "B10": 60}


def _write(path: Path, data: np.ndarray, geotransform, wkt: str, data_type=gdal.GDT_UInt16) -> Path:
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), data.shape[1], data.shape[0], 1, data_type)
    dataset.SetGeoTransform(geotransform)
    dataset.SetProjection(wkt)
    dataset.GetRasterBand(1).WriteArray(data)
    dataset = None
    return path


def _coarse(array: np.ndarray, factor: int) -> np.ndarray:
    height, width = array.shape[0] // factor, array.shape[1] // factor
    return array[: height * factor, : width * factor].reshape(height, factor, width, factor).mean(axis=(1, 3))


def _scene(tmp_path: Path, s2_rgb: Path, band_names, *, scl: bool = False) -> dict[str, dict[str, object]]:
    source = gdal.Open(str(s2_rgb))
    geotransform, wkt = source.GetGeoTransform(), source.GetProjectionRef()
    rgb = source.ReadAsArray().astype(np.float64)
    rgb[:, :24, :] = 0  # a NoData strip, on the 60 m grid too (512 = 60 m x 85.3: keep it a multiple of 6)
    rgb = rgb[:, :510, :510]  # a whole number of 60 m pixels
    base = {"B4": rgb[0], "B3": rgb[1], "B2": rgb[2]}
    bands_dir = tmp_path / "safe"
    bands_dir.mkdir()
    sources: dict[str, dict[str, object]] = {}
    for offset, name in enumerate(band_names):
        data = base.get(name)
        if data is None:  # a derived band: a mix of the visible ones, brighter by band
            data = np.where(rgb[0] > 0, 0.6 * rgb[0] + 0.3 * rgb[1] + 0.1 * rgb[2] + 150 * offset, 0)
        factor = _RESOLUTION[name] // 10
        if factor > 1:
            data = _coarse(data, factor)
        res = 10.0 * factor
        path = _write(bands_dir / f"T35_{name}.tif", np.rint(data).astype(np.uint16),
                      (geotransform[0], res, 0.0, geotransform[3], 0.0, -res), wkt)
        sources[name] = {"subdataset_name": str(path), "band_index": 1, "resolution": res, "epsg": "EPSG:32635"}
    if scl:
        classes = np.full((255, 255), 4, dtype=np.uint8)
        classes[:12, :] = 0
        classes[60:120, 60:160] = 9
        path = _write(bands_dir / "T35_SCL.tif", classes, (geotransform[0], 20.0, 0.0, geotransform[3], 0.0, -20.0),
                      wkt, gdal.GDT_Byte)
        sources["SCL"] = {"subdataset_name": str(path), "band_index": 1, "resolution": 20.0, "epsg": "EPSG:32635"}
    return sources


@pytest.fixture
def scene(tmp_path, s2_rgb, monkeypatch):
    """A Level-2A-like scene: the twelve bands without B10, and an SCL."""
    state = {"sources": _scene(tmp_path, s2_rgb, L2A_BANDS, scl=True)}
    monkeypatch.setattr(s2_module, "_collect_sentinel_s2_band_sources", lambda _input: dict(state["sources"]))
    monkeypatch.setattr(vrt_module, "_s2_band_file", lambda subdataset, _band: subdataset)
    return {"input": "scene.zip", "state": state}


# --------------------------------------------------------------------------- #
# The all-bands VRT
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cloud_mask", [False, True])
def test_every_preset_is_its_own_vrt_pixel_for_pixel(tmp_path, scene, cloud_mask):
    options = {"cloud_mask": cloud_mask}
    result = build_sentinel_s2_all_bands_vrt(
        input_dataset=scene["input"], output_path=tmp_path / "all.vrt", processing_options=options,
    )
    everything = gdal.Open(result["path"])
    assert everything.RasterCount == len(L2A_BANDS) and result["bands"] == L2A_BANDS
    assert everything.GetGeoTransform()[1] == 10.0 and result["resolution"] == 10.0
    assert set(result["presets"]) == set(S2_BAND_PRESETS)
    for preset, triplet in S2_BAND_PRESETS.items():
        own = build_sentinel_s2_vrt(
            input_dataset=scene["input"], product=preset, output_path=tmp_path / f"{preset}.vrt",
            processing_options=options,
        )
        indices = result["presets"][preset]
        assert indices == [result["band_index"][band] for band in triplet]
        assert indices == sentinel_s2_vrt_band_indices(result["path"], preset)
        selected = np.stack([everything.GetRasterBand(index).ReadAsArray() for index in indices])
        np.testing.assert_array_equal(selected, gdal.Open(own["path"]).ReadAsArray(), err_msg=preset)
        assert [result["stretch"]["p_low"][index - 1] for index in indices] == own["stretch"]["p_low"]
        assert [result["stretch"]["p_high"][index - 1] for index in indices] == own["stretch"]["p_high"]
        # Zoomed out too: a reader's decimated read of the selection is the preset's own.
        small = np.stack([everything.GetRasterBand(i).ReadAsArray(buf_xsize=64, buf_ysize=64) for i in indices])
        np.testing.assert_array_equal(small, gdal.Open(own["path"]).ReadAsArray(buf_xsize=64, buf_ysize=64))
    if cloud_mask:
        assert result["cloud_mask"]["classes"] == [3, 8, 9, 10]
        assert everything.GetMetadataItem("PYSENT_CLOUD_MASK")


def test_the_all_bands_vrt_describes_its_bands(tmp_path, scene):
    result = build_sentinel_s2_all_bands_vrt(input_dataset=scene["input"], output_path=tmp_path / "all.vrt")
    dataset = gdal.Open(result["path"])
    assert [dataset.GetRasterBand(i).GetDescription() for i in range(1, dataset.RasterCount + 1)] == L2A_BANDS
    assert all(dataset.GetRasterBand(i).DataType == gdal.GDT_Byte for i in range(1, dataset.RasterCount + 1))
    assert all(dataset.GetRasterBand(i).GetNoDataValue() == 0 for i in range(1, dataset.RasterCount + 1))
    assert result["band_index"] == {band: i for i, band in enumerate(L2A_BANDS, start=1)}
    assert result["sources"] == [scene["state"]["sources"][band]["subdataset_name"] for band in L2A_BANDS]
    assert dataset.GetMetadataItem("PYSENT_OUTPUT") == "vrt"
    # The 60 m band is nearest-upsampled: each 60 m pixel is a 6 x 6 block of the same value.
    b1 = dataset.GetRasterBand(result["band_index"]["B1"]).ReadAsArray()
    assert (b1 == np.kron(b1[::6, ::6], np.ones((6, 6), dtype=b1.dtype))).all()
    # Only the VRT is written.
    assert sorted(path.name for path in tmp_path.iterdir() if path.suffix == ".vrt") == ["all.vrt"]


def test_level_1c_holds_b10_and_level_2a_does_not(tmp_path, s2_rgb, monkeypatch):
    sources = _scene(tmp_path, s2_rgb, L1C_BANDS)
    monkeypatch.setattr(s2_module, "_collect_sentinel_s2_band_sources", lambda _input: dict(sources))
    monkeypatch.setattr(vrt_module, "_s2_band_file", lambda subdataset, _band: subdataset)
    result = build_sentinel_s2_all_bands_vrt(input_dataset="l1c.zip", output_path=tmp_path / "l1c.vrt")
    assert result["bands"] == L1C_BANDS
    assert sentinel_s2_vrt_band_indices(result["bands"], ["B10", "B11", "B12"]) == [11, 12, 13]


def test_a_preset_whose_bands_are_missing_is_left_out(tmp_path, scene):
    scene["state"]["sources"].pop("B11")
    result = build_sentinel_s2_all_bands_vrt(input_dataset=scene["input"], output_path=tmp_path / "all.vrt")
    assert "agriculture" not in result["presets"] and "true_color" in result["presets"]
    with pytest.raises(ValueError, match="no B11"):
        sentinel_s2_vrt_band_indices(result["path"], "agriculture")


def test_threads_change_nothing(tmp_path, scene):
    one = build_sentinel_s2_all_bands_vrt(
        input_dataset=scene["input"], output_path=tmp_path / "one.vrt", processing_options={"workers": 1})
    many = build_sentinel_s2_all_bands_vrt(
        input_dataset=scene["input"], output_path=tmp_path / "many.vrt", processing_options={"workers": 6})
    assert one["stretch"] == many["stretch"]
    np.testing.assert_array_equal(gdal.Open(one["path"]).ReadAsArray(), gdal.Open(many["path"]).ReadAsArray())


def test_the_stretch_options_apply_to_every_band(tmp_path, scene):
    default = build_sentinel_s2_all_bands_vrt(input_dataset=scene["input"], output_path=tmp_path / "a.vrt")
    minmax = build_sentinel_s2_all_bands_vrt(
        input_dataset=scene["input"], output_path=tmp_path / "b.vrt", processing_options={"stretch_method": "minmax"})
    assert minmax["stretch"]["method"] == "minmax" and "percentiles" not in minmax["stretch"]
    assert all(m <= d for m, d in zip(minmax["stretch"]["p_low"], default["stretch"]["p_low"]))
    assert all(m >= d for m, d in zip(minmax["stretch"]["p_high"], default["stretch"]["p_high"]))


@pytest.mark.parametrize(
    ("options", "message"),
    [({"histogram_stretch": False}, "always stretched"), ({"preset": "quicklook"}, "own grid")],
)
def test_the_all_bands_vrt_refuses_what_it_cannot_express(tmp_path, scene, options, message):
    with pytest.raises(ValueError, match=message):
        build_sentinel_s2_all_bands_vrt(input_dataset=scene["input"], output_path=tmp_path / "a.vrt",
                                        processing_options=options)
    with pytest.raises(ValueError, match="must end in .vrt"):
        build_sentinel_s2_all_bands_vrt(input_dataset=scene["input"], output_path=tmp_path / "a.tif")


def test_an_empty_band_raises_and_writes_nothing(tmp_path, scene):
    dataset = gdal.Open(scene["state"]["sources"]["B5"]["subdataset_name"], gdal.GA_Update)
    dataset.GetRasterBand(1).Fill(0)
    dataset = None
    with pytest.raises(EmptySceneError, match="B5"):
        build_sentinel_s2_all_bands_vrt(input_dataset=scene["input"], output_path=tmp_path / "a.vrt")
    assert not (tmp_path / "a.vrt").exists()


def test_the_all_bands_vrt_from_the_netcdf_is_the_safe_one(tmp_path, scene):
    """An NBS netCDF holds every band at 10 m; its bands are found by name."""
    if gdal.GetDriverByName("netCDF") is None:
        pytest.skip("GDAL built without netCDF")
    from test_vrt import _write_nbs_netcdf

    from_safe = build_sentinel_s2_all_bands_vrt(input_dataset=scene["input"], output_path=tmp_path / "safe.vrt")
    safe = gdal.Open(from_safe["path"])
    data = {}
    for band in L2A_BANDS:
        array = gdal.Open(scene["state"]["sources"][band]["subdataset_name"]).ReadAsArray()
        factor = _RESOLUTION[band] // 10
        data[band] = np.kron(array, np.ones((factor, factor), dtype=array.dtype))
    x0, res, _, y0, _, _ = safe.GetGeoTransform()
    nc = _write_nbs_netcdf(tmp_path / "scene.nc", data, x0, y0, res, safe.GetProjectionRef(), label_corners=True)
    from_nc = build_sentinel_s2_all_bands_vrt(input_dataset=str(nc), output_path=tmp_path / "nc.vrt")
    assert from_nc["bands"] == L2A_BANDS
    netcdf = gdal.Open(from_nc["path"])
    assert netcdf.GetGeoTransform() == safe.GetGeoTransform()
    np.testing.assert_array_equal(netcdf.ReadAsArray(), safe.ReadAsArray())
    assert from_nc["stretch"] == from_safe["stretch"]


# --------------------------------------------------------------------------- #
# The presets table and the triplet check
# --------------------------------------------------------------------------- #
def test_the_presets_are_the_owner_s_list():
    assert S2_BAND_PRESETS == {
        "true_color": ("B4", "B3", "B2"),
        "false_color": ("B8", "B4", "B3"),
        "false_color_vegetation": ("B8A", "B4", "B3"),
        "swir": ("B12", "B8A", "B4"),
        "agriculture": ("B11", "B8", "B2"),
        "glacier": ("B12", "B8A", "B3"),
    }
    assert all(band in L2A_BANDS for triplet in S2_BAND_PRESETS.values() for band in triplet)


@pytest.mark.parametrize(
    ("preset", "expected"),
    [
        ("swir", ("B12", "B8A", "B4")),
        (" True_Color ", ("B4", "B3", "B2")),
        ("false_color_glacier", ("B12", "B8A", "B3")),  # the GeoTIFF path's product names work too
        (["8a", "b04", "B3"], ("B8A", "B4", "B3")),
        (("B08", "11", "b12"), ("B8", "B11", "B12")),
    ],
)
def test_resolve_preset(preset, expected):
    assert resolve_sentinel_s2_preset(preset) == expected
    assert resolve_sentinel_s2_preset(preset, L2A_BANDS) == expected


@pytest.mark.parametrize(
    ("preset", "available", "message"),
    [
        ("ndvi", None, "unknown Sentinel-2 preset"),
        (["B4", "B3"], None, "three bands"),
        (["B4", "B3", "B13"], None, "not Sentinel-2 bands: B13"),
        (["B4", "B3", "SCL"], None, "not Sentinel-2 bands: SCL"),
        (["B10", "B4", "B3"], L2A_BANDS, "no B10"),
    ],
)
def test_resolve_preset_refuses(preset, available, message):
    with pytest.raises(ValueError, match=message):
        resolve_sentinel_s2_preset(preset, available)


def test_the_single_product_vrt_takes_the_new_preset_names(tmp_path, scene):
    result = build_sentinel_s2_vrt(input_dataset=scene["input"], product="agriculture", output_path=tmp_path / "a.vrt")
    assert result["bands"] == ["B11", "B8", "B2"] and result["product_name"] == "agriculture"
