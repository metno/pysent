"""Sentinel-2 Level-2A: the optional SCL cloud mask and the radiometric offsets.

No SAFE product is needed. The committed window of a real scene stands in for
the band files, with a synthetic scene classification (SCL) at 20 m beside it;
the lookups that find those files inside a SAFE are replaced by ones that
return them, for both the GeoTIFF and the VRT path, so the two see the same
input and their pictures can be compared pixel for pixel.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("rasterio")
gdal = pytest.importorskip("osgeo.gdal")

from pysent import s2 as s2_module  # noqa: E402
from pysent import vrt as vrt_module  # noqa: E402
from pysent.profiles import resolve_sentinel_pipeline  # noqa: E402
from pysent.s2 import (  # noqa: E402
    S2_CLOUD_MASK_CLASSES,
    S2_DEFAULT_PRODUCTS,
    process_sentinel_s2_safe,
    read_sentinel_s2_radiometry,
    resolve_sentinel_s2_cloud_mask,
)
from pysent.vrt import build_sentinel_s2_vrt  # noqa: E402

gdal.UseExceptions()

PRODUCT = "true_color_vegetation"


def _write(path: Path, data: np.ndarray, geotransform, wkt: str, data_type=gdal.GDT_UInt16) -> Path:
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), data.shape[1], data.shape[0], 1, data_type)
    dataset.SetGeoTransform(geotransform)
    dataset.SetProjection(wkt)
    dataset.GetRasterBand(1).WriteArray(data)
    dataset = None
    return path


@pytest.fixture
def l2a_scene(tmp_path, s2_rgb, monkeypatch):
    """B4/B3/B2 at 10 m with a bright cloud and a dark shadow, and their SCL at 20 m."""
    source = gdal.Open(str(s2_rgb))
    geotransform, wkt = source.GetGeoTransform(), source.GetProjectionRef()
    rgb = source.ReadAsArray().astype(np.uint16)
    rgb[:, :16, :] = 0  # a NoData strip
    rgb[:, 100:200, 100:300] = 12000  # a cloud, far brighter than the land
    rgb[:, 300:340, 100:300] = 300  # its shadow
    scl = np.full((256, 256), 4, dtype=np.uint8)  # vegetation
    scl[:8, :] = 0  # no data, under the NoData strip
    scl[50:100, 50:150] = 9  # cloud, high probability
    scl[150:170, 50:150] = 3  # cloud shadow
    scl[200:210, 200:220] = 6  # water: never masked by default
    bands_dir = tmp_path / "safe"
    bands_dir.mkdir()
    sources: dict[str, dict[str, object]] = {}
    for name, data in zip(("B4", "B3", "B2"), rgb):
        path = _write(bands_dir / f"T35_{name}.tif", data, geotransform, wkt)
        sources[name] = {"subdataset_name": str(path), "band_index": 1, "resolution": 10.0, "epsg": "EPSG:32635"}
    coarse_gt = (geotransform[0], 20.0, 0.0, geotransform[3], 0.0, -20.0)
    path = _write(bands_dir / "T35_SCL.tif", scl, coarse_gt, wkt, gdal.GDT_Byte)
    sources["SCL"] = {"subdataset_name": str(path), "band_index": 1, "resolution": 20.0, "epsg": "EPSG:32635"}

    state = {"sources": sources}
    monkeypatch.setattr(s2_module, "_collect_sentinel_s2_band_sources", lambda _input: dict(state["sources"]))
    monkeypatch.setattr(vrt_module, "_s2_band_file", lambda subdataset, _band: subdataset)
    # The SCL repeated to 10 m: what the mask is expected to follow.
    return {"input": "scene.zip", "state": state, "scl10": np.kron(scl, np.ones((2, 2), dtype=np.uint8))}


def _gtiff(tmp_path: Path, options: dict | None = None, name: str = "gtiff"):
    result = process_sentinel_s2_safe(
        input_dataset="scene.zip",
        output_dir=tmp_path / name,
        product_bands={PRODUCT: S2_DEFAULT_PRODUCTS[PRODUCT]},
        output_names={PRODUCT: f"{PRODUCT}.tif"},
        processing_options={"histogram_stretch": True, **(options or {})},
    )[0]
    return result, gdal.Open(result["path"]).ReadAsArray()


# --------------------------------------------------------------------------- #
# The mask in the GeoTIFF path
# --------------------------------------------------------------------------- #
def test_the_mask_is_off_by_default_and_changes_nothing(tmp_path, l2a_scene):
    default, expected = _gtiff(tmp_path, name="default")
    off, picture = _gtiff(tmp_path, {"cloud_mask": False}, name="off")
    np.testing.assert_array_equal(picture, expected)
    assert default["cloud_mask"] is None and off["cloud_mask"] is None
    assert "PYSENT_CLOUD_MASK" not in default["provenance"]
    # The cloud stays in the picture, and in the stretch.
    assert default["stretch"]["p_high"][0] == 12000.0


def test_the_mask_turns_cloud_and_shadow_into_nodata(tmp_path, l2a_scene):
    result, picture = _gtiff(tmp_path, {"cloud_mask": True})
    scl = l2a_scene["scl10"]
    masked = np.isin(scl, S2_CLOUD_MASK_CLASSES)
    assert not picture[:, masked].any()  # transparent in every band
    kept = ~masked
    kept[:16, :] = False  # the NoData strip
    assert (picture[:, kept] > 0).all()  # water and land stay
    assert result["cloud_mask"] == {
        "classes": list(S2_CLOUD_MASK_CLASSES),
        "masked_fraction": pytest.approx(masked.mean()),
    }
    assert result["provenance"]["PYSENT_CLOUD_MASK"] == "3,8,9,10"
    # The cloud pixels the SCL marks leave the percentiles: the land gets the range.
    off, _ = _gtiff(tmp_path, name="off")
    assert result["stretch"]["p_high"][0] < off["stretch"]["p_high"][0]


def test_a_list_of_classes_masks_only_those(tmp_path, l2a_scene):
    result, picture = _gtiff(tmp_path, {"cloud_mask": [6]})
    scl = l2a_scene["scl10"]
    assert not picture[:, scl == 6].any()
    assert picture[:, scl == 9].all()
    assert result["provenance"]["PYSENT_CLOUD_MASK"] == "6"


def test_the_mask_refuses_a_product_without_an_scl(tmp_path, l2a_scene):
    l2a_scene["state"]["sources"].pop("SCL")  # a Level-1C product
    with pytest.raises(ValueError, match="SCL"):
        _gtiff(tmp_path, {"cloud_mask": True})
    _gtiff(tmp_path, name="unmasked")  # and without the mask it is processed as before


# --------------------------------------------------------------------------- #
# The mask in the VRT
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cloud_mask", [True, [3], False])
def test_the_vrt_mask_is_the_gtiff_picture(tmp_path, l2a_scene, cloud_mask):
    options = {"cloud_mask": cloud_mask}
    reference, expected = _gtiff(tmp_path, options)
    result = build_sentinel_s2_vrt(
        input_dataset=l2a_scene["input"], product=PRODUCT, output_path=tmp_path / "p.vrt", processing_options=options,
    )
    dataset = gdal.Open(result["path"])
    np.testing.assert_array_equal(dataset.ReadAsArray(), expected)
    assert result["stretch"]["p_low"] == reference["stretch"]["p_low"]
    assert result["stretch"]["p_high"] == reference["stretch"]["p_high"]
    assert dataset.GetRasterBand(1).GetNoDataValue() == 0
    assert dataset.GetRasterBand(1).GetColorInterpretation() == gdal.GCI_RedBand
    if cloud_mask:
        assert result["cloud_mask"]["source"] == l2a_scene["state"]["sources"]["SCL"]["subdataset_name"]
        assert dataset.GetMetadataItem("PYSENT_CLOUD_MASK") == reference["provenance"]["PYSENT_CLOUD_MASK"]
        # A built-in pixel function: the VRT needs no Python to be read.
        text = Path(result["path"]).read_text()
        assert "<PixelFunctionType>mul</PixelFunctionType>" in text and "PixelFunctionLanguage" not in text
    else:
        assert result["cloud_mask"] is None and "VRTDerivedRasterBand" not in Path(result["path"]).read_text()


def test_the_vrt_mask_refuses_a_product_without_an_scl(tmp_path, l2a_scene):
    l2a_scene["state"]["sources"].pop("SCL")
    with pytest.raises(ValueError, match="SCL"):
        build_sentinel_s2_vrt(
            input_dataset=l2a_scene["input"], product=PRODUCT, output_path=tmp_path / "p.vrt",
            processing_options={"cloud_mask": True},
        )
    assert not (tmp_path / "p.vrt").exists()


# --------------------------------------------------------------------------- #
# Options, routing, radiometry
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ()), (False, ()), ("", ()), ("off", ()), ("false", ()),
        (True, S2_CLOUD_MASK_CLASSES), ("true", S2_CLOUD_MASK_CLASSES),
        ([10, 9, 9], (9, 10)), ((0,), (0,)),
    ],
)
def test_cloud_mask_values(value, expected):
    assert resolve_sentinel_s2_cloud_mask(value) == expected


@pytest.mark.parametrize("value", [[12], [], [-1], "clouds", 3.5])
def test_cloud_mask_refuses_what_is_not_an_scl_class(value):
    with pytest.raises(ValueError, match="cloud_mask"):
        resolve_sentinel_s2_cloud_mask(value)


def test_level_2a_routes_to_the_s2_pipeline():
    assert resolve_sentinel_pipeline("S2C_MSIL2A_20261122T104341_N0513_R008_T32UMD_20261122T121307.zip") == "s2"


_BANDS = ("B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B9", "B10", "B11", "B12")


def _safe(tmp_path: Path, level: str, baseline: str, offset_tag: str | None) -> Path:
    """A SAFE directory holding only the product metadata, shaped like the real one."""
    safe = tmp_path / f"S2B_MSIL{level}_20261117T104229_N{baseline.replace('.', '')}_R008_T33VUG_X.SAFE"
    safe.mkdir()
    spectral = "".join(
        f'<Spectral_Information bandId="{index}" physicalBand="{name}"><RESOLUTION>10</RESOLUTION></Spectral_Information>'
        for index, name in enumerate(_BANDS)
    )
    quantification = (
        '<QUANTIFICATION_VALUES_LIST><BOA_QUANTIFICATION_VALUE unit="none">10000</BOA_QUANTIFICATION_VALUE>'
        '<AOT_QUANTIFICATION_VALUE unit="none">1000.0</AOT_QUANTIFICATION_VALUE></QUANTIFICATION_VALUES_LIST>'
        if level == "2A" else '<QUANTIFICATION_VALUE unit="none">10000</QUANTIFICATION_VALUE>'
    )
    offsets = ""
    if offset_tag:
        offsets = "".join(f'<{offset_tag} band_id="{index}">-1000</{offset_tag}>' for index in range(13))
    (safe / f"MTD_MSIL{level}.xml").write_text(
        f"<n1:Level-{level}_User_Product xmlns:n1='x'><General_Info><Product_Info>"
        f"<PROCESSING_LEVEL>Level-{level}</PROCESSING_LEVEL><PROCESSING_BASELINE>{baseline}</PROCESSING_BASELINE>"
        f"</Product_Info><Product_Image_Characteristics>{quantification}{offsets}"
        f"<Spectral_Information_List>{spectral}</Spectral_Information_List>"
        f"</Product_Image_Characteristics></General_Info></n1:Level-{level}_User_Product>"
    )
    return safe


@pytest.mark.parametrize(
    ("level", "tag"), [("2A", "BOA_ADD_OFFSET"), ("1C", "RADIO_ADD_OFFSET")],
)
def test_radiometry_reads_the_offset_of_baseline_04_and_later(tmp_path, level, tag):
    radiometry = read_sentinel_s2_radiometry(str(_safe(tmp_path, level, "05.13", tag)))
    assert radiometry["level"] == f"L{level}"
    assert radiometry["processing_baseline"] == "05.13"
    assert radiometry["quantification"] == 10000.0
    assert radiometry["offsets"] == dict.fromkeys(_BANDS, -1000.0)


def test_radiometry_before_baseline_04_has_no_offset(tmp_path):
    radiometry = read_sentinel_s2_radiometry(str(_safe(tmp_path, "2A", "03.01", None)))
    assert radiometry["offsets"] == dict.fromkeys(_BANDS, 0.0)


def test_the_offset_leaves_the_picture_alone(tmp_path, l2a_scene):
    """Adding the offset to every value moves the percentiles with it: the bytes are the same."""
    _, expected = _gtiff(tmp_path, name="dn")
    for name in ("B4", "B3", "B2"):
        path = l2a_scene["state"]["sources"][name]["subdataset_name"]
        dataset = gdal.Open(path, gdal.GA_Update)
        band = dataset.GetRasterBand(1)
        data = band.ReadAsArray()
        band.WriteArray(np.where(data > 0, data + 1000, 0).astype(np.uint16))  # the -1000 undone, NoData kept
        dataset = None
    _, shifted = _gtiff(tmp_path, name="shifted")
    np.testing.assert_array_equal(shifted, expected)
