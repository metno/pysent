"""The VRT builders against the GeoTIFF path they reproduce.

No SAFE product is needed: the committed windows of real scenes stand in for
the files inside one. For Sentinel-2 each band becomes its own file (plus a
20 m band, to exercise the nearest-neighbour upsampling); for Sentinel-1 the VV
window gets ground control points, like a SAFE measurement. The lookups that
find those files inside a SAFE are replaced by ones that return them, for both
the VRT and the GeoTIFF path, so the two see the same input and their pictures
can be compared pixel for pixel.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("rasterio")
gdal = pytest.importorskip("osgeo.gdal")
osr = pytest.importorskip("osgeo.osr")

import pysent  # noqa: E402
from pysent import s1 as s1_module  # noqa: E402
from pysent import s2 as s2_module  # noqa: E402
from pysent import vrt as vrt_module  # noqa: E402
from pysent.errors import EmptySceneError  # noqa: E402
from pysent.s1 import process_sentinel_s1_safe  # noqa: E402
from pysent.s2 import S2_DEFAULT_PRODUCTS, process_sentinel_s2_safe  # noqa: E402
from pysent.vrt import build_sentinel_s1_vrt, build_sentinel_s2_vrt  # noqa: E402

gdal.UseExceptions()


# --------------------------------------------------------------------------- #
# Sentinel-2
# --------------------------------------------------------------------------- #
def _write_band(path: Path, data: np.ndarray, geotransform, wkt: str) -> Path:
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), data.shape[1], data.shape[0], 1, gdal.GDT_UInt16)
    dataset.SetGeoTransform(geotransform)
    dataset.SetProjection(wkt)
    dataset.GetRasterBand(1).WriteArray(data)
    dataset = None
    return path


@pytest.fixture
def s2_scene(tmp_path, s2_rgb, monkeypatch):
    """B4/B3/B2 at 10 m from the real window, B8A at 20 m, a NoData strip in all of them."""
    source = gdal.Open(str(s2_rgb))
    geotransform, wkt = source.GetGeoTransform(), source.GetProjectionRef()
    rgb = source.ReadAsArray().astype(np.uint16)
    rgb[:, :16, :] = 0
    bands_dir = tmp_path / "safe"
    bands_dir.mkdir()
    sources: dict[str, dict[str, object]] = {}
    for name, data in zip(("B4", "B3", "B2"), rgb):
        path = _write_band(bands_dir / f"T35_{name}.tif", data, geotransform, wkt)
        sources[name] = {"subdataset_name": str(path), "band_index": 1, "resolution": 10.0, "epsg": "EPSG:32635"}
    # A 20 m band on the same tile: the 2x2 mean of B4, brighter so its stretch differs.
    coarse = rgb[0].reshape(256, 2, 256, 2).mean(axis=(1, 3)).astype(np.uint16)
    coarse[coarse > 0] += 500
    coarse_gt = (geotransform[0], 20.0, 0.0, geotransform[3], 0.0, -20.0)
    path = _write_band(bands_dir / "T35_B8A.tif", coarse, coarse_gt, wkt)
    sources["B8A"] = {"subdataset_name": str(path), "band_index": 1, "resolution": 20.0, "epsg": "EPSG:32635"}

    monkeypatch.setattr(s2_module, "_collect_sentinel_s2_band_sources", lambda _input: dict(sources))
    monkeypatch.setattr(vrt_module, "_s2_band_file", lambda subdataset, _band: subdataset)
    return {"input": "scene.zip", "sources": sources}


def _s2_gtiff(tmp_path: Path, product: str, options: dict | None = None):
    options = {"histogram_stretch": True, "compression": "DEFLATE", **(options or {})}
    result = process_sentinel_s2_safe(
        input_dataset="scene.zip",
        output_dir=tmp_path / "gtiff",
        product_bands={product: S2_DEFAULT_PRODUCTS[product]},
        output_names={product: f"{product}.tif"},
        processing_options=options,
    )[0]
    return result, gdal.Open(result["path"]).ReadAsArray()


@pytest.mark.parametrize("product", ["true_color_vegetation", "false_color_vegetation"])
def test_s2_vrt_is_the_gtiff_picture(tmp_path, s2_scene, product):
    reference, expected = _s2_gtiff(tmp_path, product)
    result = build_sentinel_s2_vrt(input_dataset=s2_scene["input"], product=product, output_path=tmp_path / "out" / "p.vrt")

    dataset = gdal.Open(result["path"])
    assert dataset.RasterCount == 3 and dataset.GetRasterBand(1).DataType == gdal.GDT_Byte
    np.testing.assert_array_equal(dataset.ReadAsArray(), expected)
    assert dataset.GetGeoTransform() == gdal.Open(reference["path"]).GetGeoTransform()
    assert result["stretch"]["p_low"] == reference["stretch"]["p_low"]
    assert result["stretch"]["p_high"] == reference["stretch"]["p_high"]
    assert [dataset.GetRasterBand(i).GetNoDataValue() for i in (1, 2, 3)] == [0, 0, 0]
    assert [dataset.GetRasterBand(i).GetColorInterpretation() for i in (1, 2, 3)] == [
        gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand,
    ]


def test_s2_vrt_honours_the_stretch_options(tmp_path, s2_scene):
    options = {"stretch_percentiles": (2.0, 98.0), "stretch_gamma": 1.0}
    _, expected = _s2_gtiff(tmp_path, "true_color_vegetation", options)
    result = build_sentinel_s2_vrt(
        input_dataset=s2_scene["input"], product="true_color_vegetation",
        output_path=tmp_path / "p.vrt", processing_options=options,
    )
    np.testing.assert_array_equal(gdal.Open(result["path"]).ReadAsArray(), expected)
    assert result["stretch"]["percentiles"] == [2.0, 98.0] and result["stretch"]["gamma"] == 1.0


def test_s2_vrt_minmax_stretch_matches(tmp_path, s2_scene):
    _, expected = _s2_gtiff(tmp_path, "true_color_vegetation", {"stretch_method": "minmax"})
    result = build_sentinel_s2_vrt(
        input_dataset=s2_scene["input"], product="true_color_vegetation",
        output_path=tmp_path / "p.vrt", processing_options={"stretch_method": "minmax"},
    )
    np.testing.assert_array_equal(gdal.Open(result["path"]).ReadAsArray(), expected)
    assert "percentiles" not in result["stretch"]


def test_s2_vrt_writes_only_the_vrt_and_stamps_it(tmp_path, s2_scene):
    out = tmp_path / "out"
    result = build_sentinel_s2_vrt(input_dataset=s2_scene["input"], product=("B4", "B3", "B2"), output_path=out / "x.vrt")
    assert sorted(path.name for path in out.iterdir()) == ["x.vrt"]
    assert result["product_name"] == "b4_b3_b2"
    assert result["sources"] == [s2_scene["sources"][band]["subdataset_name"] for band in ("B4", "B3", "B2")]
    stamp = result["provenance"]
    assert stamp["PYSENT_OUTPUT"] == "vrt" and stamp["PYSENT_VERSION"] == pysent.__version__
    assert stamp["PYSENT_STRETCH_PERCENTILES"] == "0.5,99.5" and stamp["PYSENT_STRETCH_GAMMA"] == "0.7"
    metadata = gdal.Open(result["path"]).GetMetadata()
    assert {key: metadata[key] for key in stamp} == stamp


def test_s2_vrt_reads_its_sources_by_reference(tmp_path, s2_scene):
    """The VRT holds no pixels: change a source, and the picture changes."""
    result = build_sentinel_s2_vrt(input_dataset=s2_scene["input"], product="true_color_vegetation", output_path=tmp_path / "p.vrt")
    before = gdal.Open(result["path"]).ReadAsArray()
    source = gdal.Open(s2_scene["sources"]["B4"]["subdataset_name"], gdal.GA_Update)
    source.GetRasterBand(1).Fill(0)
    source = None
    after = gdal.Open(result["path"]).ReadAsArray()
    assert before[0].any() and not after[0].any()
    np.testing.assert_array_equal(before[1:], after[1:])


def test_s2_vrt_empty_band_raises(tmp_path, s2_scene):
    path = s2_scene["sources"]["B2"]["subdataset_name"]
    source = gdal.Open(path, gdal.GA_Update)
    source.GetRasterBand(1).Fill(0)
    source = None
    with pytest.raises(EmptySceneError):
        build_sentinel_s2_vrt(input_dataset=s2_scene["input"], product="true_color_vegetation", output_path=tmp_path / "p.vrt")
    assert not (tmp_path / "p.vrt").exists()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"processing_options": {"histogram_stretch": False}}, "always stretched"),
        ({"processing_options": {"preset": "quicklook"}}, "own grid"),
        ({"output_path": "x.tif"}, "must end in .vrt"),
        ({"product": "no_such_product"}, "unknown Sentinel-2 product"),
    ],
)
def test_s2_vrt_refuses_what_it_cannot_express(tmp_path, s2_scene, kwargs, message):
    arguments = {"input_dataset": s2_scene["input"], "product": "true_color_vegetation", "output_path": tmp_path / "p.vrt"}
    arguments.update(kwargs)
    with pytest.raises(ValueError, match=message):
        build_sentinel_s2_vrt(**arguments)


# --------------------------------------------------------------------------- #
# Sentinel-1
# --------------------------------------------------------------------------- #
@pytest.fixture
def s1_scene(tmp_path, s1_vv, monkeypatch):
    """The real VV window with a grid of ground control points near 70 N, ~10 m pixels."""
    data = gdal.Open(str(s1_vv)).ReadAsArray().astype(np.uint16)
    data[:, :24] = 0  # the swath edge
    path = tmp_path / "measurement-vv.tiff"
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), data.shape[1], data.shape[0], 1, gdal.GDT_UInt16)
    dataset.GetRasterBand(1).WriteArray(data)
    gcps = []
    for row in np.linspace(0, data.shape[0], 5):
        for col in np.linspace(0, data.shape[1], 5):
            # a slightly rotated, slightly sheared grid, as a radar swath is
            lon = 20.0 + col * 2.6e-4 + row * 3.0e-5
            lat = 70.0 - row * 9.0e-5 + col * 1.0e-5
            gcps.append(gdal.GCP(lon, lat, 0.0, float(col), float(row)))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    dataset.SetGCPs(gcps, srs.ExportToWkt())
    dataset = None

    monkeypatch.setattr(s1_module, "_resolve_safe_manifest_path", lambda _input: str(path))
    monkeypatch.setattr(s1_module, "_resolve_safe_band_index", lambda _manifest, _variable: 1)
    monkeypatch.setattr(s1_module, "detect_sentinel_s1_polarizations", lambda _input: ["Amplitude_VV"])
    return {"input": "scene.zip", "path": path}


S1_OPTIONS = {"use_tps": False, "target_resolution": 20.0, "gdal_num_threads": 1}


def _s1_gtiff(tmp_path: Path, options: dict | None = None):
    result = process_sentinel_s1_safe(
        input_dataset="scene.zip",
        output_dir=tmp_path / "gtiff",
        output_names={"Amplitude_VV": "vv.tif"},
        processing_options={**S1_OPTIONS, "compression": "DEFLATE", **(options or {})},
    )[0]
    return result, gdal.Open(result["path"])


def _warped_amplitude(vrt_path: str) -> np.ndarray:
    """The amplitude the VRT's gray band is computed from: its own inline warp, read whole."""
    root = vrt_module.ET.parse(vrt_path).getroot()
    return gdal.Open(root.find(".//ComplexSource/SourceFilename").text).ReadAsArray()


@pytest.mark.parametrize("method", ["db", "linear"])
def test_s1_vrt_stretch_is_pysents_stretch(tmp_path, s1_scene, method):
    """On the amplitudes it reads, the VRT gives what stretch_sentinel_s1_grayscale gives."""
    result = build_sentinel_s1_vrt(
        input_dataset=s1_scene["input"], polarisation="VV", output_path=tmp_path / "vv.vrt",
        processing_options={**S1_OPTIONS, "stretch_method": method},
    )
    gray, alpha = gdal.Open(result["path"]).ReadAsArray()
    want_gray, want_alpha, stats = s1_module.stretch_sentinel_s1_grayscale(
        _warped_amplitude(result["path"]), nodata=0.0, method=method
    )
    np.testing.assert_array_equal(alpha, want_alpha)
    difference = np.abs(gray.astype(int) - want_gray.astype(int))
    # Only a value sitting exactly on a step may land on the neighbouring level.
    assert difference.max() <= 1 and (difference > 0).mean() < 1e-3
    assert result["stretch"] == stats


@pytest.mark.parametrize("method", ["db", "linear"])
def test_s1_vrt_is_the_gtiff_picture(tmp_path, s1_scene, method):
    reference, expected = _s1_gtiff(tmp_path, {"stretch_method": method})
    result = build_sentinel_s1_vrt(
        input_dataset=s1_scene["input"], polarisation="VV", output_path=tmp_path / "out" / "vv.vrt",
        processing_options={**S1_OPTIONS, "stretch_method": method},
    )
    dataset = gdal.Open(result["path"])
    assert (dataset.RasterXSize, dataset.RasterYSize) == (expected.RasterXSize, expected.RasterYSize)
    assert dataset.GetGeoTransform() == pytest.approx(expected.GetGeoTransform())
    assert dataset.GetRasterBand(2).GetColorInterpretation() == gdal.GCI_AlphaBand

    gray, alpha = dataset.ReadAsArray()
    want_gray, want_alpha = expected.ReadAsArray()
    assert alpha.any() and not alpha.all()
    # Not bit for bit: GDAL warps with an approximate transform (an error of up
    # to 1/8 pixel) fitted per chunk, and a warped VRT is read in other chunks
    # than the GeoTIFF was written in. A pixel's average then moves by a level or
    # so, and a pixel on the swath edge may fall in or out. A wrong grid, curve
    # or mask would move every pixel.
    assert (alpha != want_alpha).mean() < 1e-3
    both = (alpha > 0) & (want_alpha > 0)
    difference = np.abs(gray.astype(int) - want_gray.astype(int))[both]
    assert difference.mean() < 0.5
    assert (difference <= 3).mean() > 0.99
    assert result["stretch"]["p_low"] == pytest.approx(reference["stretch"]["p_low"], rel=2e-3)
    assert result["stretch"]["p_high"] == pytest.approx(reference["stretch"]["p_high"], rel=2e-3)
    assert result["stretch"]["unit"] == reference["stretch"]["unit"]


def test_s1_vrt_overviews_are_the_gtiff_writers_overviews(tmp_path, s1_scene):
    """Fed the VRT's own warp, pysent's GeoTIFF writer builds the same x4, x8, x16 levels."""
    result = build_sentinel_s1_vrt(
        input_dataset=s1_scene["input"], polarisation="VV", output_path=tmp_path / "vv.vrt", processing_options=S1_OPTIONS,
    )
    dataset = gdal.Open(result["path"])
    assert dataset.GetRasterBand(1).GetOverviewCount() == 3  # from the .ovr

    root = vrt_module.ET.parse(result["path"]).getroot()
    warped = tmp_path / "warped.tif"
    gdal.Translate(str(warped), gdal.Open(root.find(".//ComplexSource/SourceFilename").text))
    written = tmp_path / "written.tif"
    s1_module._write_quicklook_from_warped(warped, written, compression="DEFLATE", overview_factors=(2, 4, 8, 16))
    expected = gdal.Open(str(written))
    for level, factor in enumerate(result["overview_factors"]):
        reference_level = (2, 4, 8, 16).index(factor)
        for band in (1, 2):
            got = dataset.GetRasterBand(band).GetOverview(level).ReadAsArray()
            want = expected.GetRasterBand(band).GetOverview(reference_level).ReadAsArray()
            np.testing.assert_array_equal(got, want, err_msg=f"x{factor} band {band}")


def test_s1_vrt_writes_the_vrt_and_its_sidecar_only(tmp_path, s1_scene):
    out = tmp_path / "out"
    result = build_sentinel_s1_vrt(
        input_dataset=s1_scene["input"], polarisation="Amplitude_VV", output_path=out / "vv.vrt", processing_options=S1_OPTIONS,
    )
    assert sorted(path.name for path in out.iterdir()) == ["vv.vrt", "vv.vrt.alpha.tif", "vv.vrt.ovr"]
    assert result["sidecar_paths"] == [str(out / "vv.vrt.ovr"), str(out / "vv.vrt.alpha.tif")]
    assert result["variable"] == "Amplitude_VV" and result["sources"] == [str(s1_scene["path"])]
    stamp = result["provenance"]
    assert stamp["PYSENT_OUTPUT"] == "vrt"
    assert stamp["PYSENT_WARP_TRANSFORM"] == "polynomial-gcp"
    assert stamp["PYSENT_STRETCH_METHOD"] == "db" and stamp["PYSENT_OVERVIEW_FACTORS"] == "4,8,16"
    metadata = gdal.Open(result["path"]).GetMetadata()
    assert {key: metadata[key] for key in stamp} == stamp


def test_s1_vrt_directory_can_move(tmp_path, s1_scene):
    """The sidecar is named relatively: the three files move together."""
    result = build_sentinel_s1_vrt(
        input_dataset=s1_scene["input"], polarisation="VV", output_path=tmp_path / "a" / "vv.vrt", processing_options=S1_OPTIONS,
    )
    before = gdal.Open(result["path"]).ReadAsArray()
    shutil.move(str(tmp_path / "a"), str(tmp_path / "b"))
    moved = gdal.Open(str(tmp_path / "b" / "vv.vrt"))
    np.testing.assert_array_equal(moved.ReadAsArray(), before)
    assert moved.GetRasterBand(1).GetOverviewCount() == 3


def test_s1_vrt_full_resolution_gray_is_warped_from_the_source(tmp_path, s1_scene):
    """Only the levels at x4 and coarser are stored; the full resolution reads the SAFE."""
    result = build_sentinel_s1_vrt(
        input_dataset=s1_scene["input"], polarisation="VV", output_path=tmp_path / "vv.vrt", processing_options=S1_OPTIONS,
    )
    before = gdal.Open(result["path"]).ReadAsArray()
    source = gdal.Open(str(s1_scene["path"]), gdal.GA_Update)
    source.GetRasterBand(1).WriteArray(np.full((512, 512), 50000, dtype=np.uint16))
    source = None
    gray, alpha = gdal.Open(result["path"]).ReadAsArray()
    assert (gray[before[1] > 0] == 255).all()  # every valid pixel now above the baked-in p_high
    np.testing.assert_array_equal(alpha, before[1])  # the mask is the stored one


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"processing_options": {"speckle_filter": "lee"}}, ValueError, "speckle"),
        ({"polarisation": "XX"}, ValueError, "polarisation"),
        ({"output_path": "vv.tif"}, ValueError, "must end in .vrt"),
        ({"processing_options": {"overview_factors": [1]}}, ValueError, "overview_factors"),
    ],
)
def test_s1_vrt_refuses_what_it_cannot_express(tmp_path, s1_scene, kwargs, error, message):
    arguments = {"input_dataset": s1_scene["input"], "polarisation": "VV", "output_path": tmp_path / "vv.vrt"}
    arguments.update(kwargs)
    with pytest.raises(error, match=message):
        build_sentinel_s1_vrt(**arguments)


def test_s1_vrt_empty_scene_raises_and_leaves_nothing(tmp_path, s1_scene):
    source = gdal.Open(str(s1_scene["path"]), gdal.GA_Update)
    source.GetRasterBand(1).Fill(0)
    source = None
    with pytest.raises(EmptySceneError):
        build_sentinel_s1_vrt(
            input_dataset=s1_scene["input"], polarisation="VV", output_path=tmp_path / "out" / "vv.vrt",
            processing_options=S1_OPTIONS,
        )
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


# --------------------------------------------------------------------------- #
# The lookup tables
# --------------------------------------------------------------------------- #
def test_step_lut_reproduces_a_step_function_at_integers():
    values = np.arange(1, 20)
    outputs = np.array([1, 1, 1, 5, 5, 9, 9, 9, 9, 12, 12, 12, 12, 12, 12, 12, 200, 255, 255])
    lut = vrt_module._step_lut(values, outputs)
    pairs = [tuple(float(x) for x in pair.split(":")) for pair in lut.split(",")]
    xs, ys = zip(*pairs)
    np.testing.assert_array_equal(np.interp(values, xs, ys), outputs)
    assert len(pairs) < len(values)


@pytest.mark.parametrize("method", ["db", "linear"])
def test_s1_lut_matches_the_numpy_stretch(method):
    rng = np.random.default_rng(1)
    amplitude = rng.gamma(2.0, 120.0, size=200_000).astype(np.float32) + 1.0
    gray, _, stats = s1_module.stretch_sentinel_s1_grayscale(amplitude, nodata=0.0, method=method)
    pairs = [tuple(float(x) for x in pair.split(":")) for pair in vrt_module._s1_lut(stats["p_low"], stats["p_high"], method).split(",")]
    xs, ys = zip(*pairs)
    via_lut = np.rint(np.interp(amplitude.astype(np.float64), xs, ys)).astype(np.uint8)
    difference = np.abs(via_lut.astype(int) - gray.astype(int))
    # numpy rounds halves to even and float32 log10 differs in the last bit:
    # only a value sitting on a step can land on the neighbouring level.
    assert difference.max() <= 1 and (difference > 0).mean() < 1e-3


def test_vrt_builders_are_exported():
    assert pysent.build_sentinel_s1_vrt is build_sentinel_s1_vrt
    assert pysent.build_sentinel_s2_vrt is build_sentinel_s2_vrt
    assert "vrt" in dir(pysent)
