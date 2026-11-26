"""Catalogue thumbnails from the coarsest data (pysent.thumbnail).

Sentinel-2 runs through the real boundary: a SAFE-shaped zip holding JPEG 2000
bands made from the committed window, read from the files' own reduced levels,
and compared with the quicklook product made from the same files and averaged
down. Sentinel-1 reads strip TIFFs made from the VV and VH windows, through a
VRT that carries what the SAFE driver would (the polarisation of each band, the
measurement files, ground control points).
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("rasterio")
gdal = pytest.importorskip("osgeo.gdal")
osr = pytest.importorskip("osgeo.osr")

from pysent import s2 as s2_module  # noqa: E402
from pysent import thumbnail as thumbnail_module  # noqa: E402
from pysent.errors import EmptySceneError  # noqa: E402
from pysent.s1 import stretch_sentinel_s1_grayscale  # noqa: E402
from pysent.s2 import S2_DEFAULT_PRODUCTS, process_sentinel_s2_safe  # noqa: E402
from pysent.thumbnail import (  # noqa: E402
    sentinel_s1_thumbnail,
    sentinel_s2_thumbnail,
    write_thumbnail_png,
)

gdal.UseExceptions()

TILE = "T35VPE"
SAFE = f"S2C_MSIL1C_20260810T092031_N0512_R093_{TILE}_20260810T124910.SAFE"
GRANULE = f"L1C_{TILE}_A004567_20260810T092031"


# --------------------------------------------------------------------------- #
# Sentinel-2
# --------------------------------------------------------------------------- #
def _jp2(path: Path, data: np.ndarray, geotransform, wkt: str) -> None:
    memory = gdal.GetDriverByName("MEM").Create("", data.shape[1], data.shape[0], 1, gdal.GDT_UInt16)
    memory.SetGeoTransform(geotransform)
    memory.SetProjection(wkt)
    memory.GetRasterBand(1).WriteArray(data)
    # Lossless, tiled, with reduced levels: what the archive's band files are.
    gdal.GetDriverByName("JP2OpenJPEG").CreateCopy(
        str(path), memory,
        options=["REVERSIBLE=YES", "QUALITY=100", "RESOLUTIONS=4", "BLOCKXSIZE=128", "BLOCKYSIZE=128"],
    )


def _s2_bands(s2_rgb, *, empty_rows: int = 0) -> dict[str, tuple[np.ndarray, tuple]]:
    """B4, B3 at 10 m from the window; B8A at 20 m (B4's 2 x 2 mean, brighter)."""
    source = gdal.Open(str(s2_rgb))
    geotransform = source.GetGeoTransform()
    rgb = source.ReadAsArray().astype(np.uint16)
    if empty_rows:
        rgb[:, :empty_rows, :] = 0  # the swath does not cover the top of the tile
    coarse = rgb[0].reshape(256, 2, 256, 2).mean(axis=(1, 3)).astype(np.uint16)
    coarse[coarse > 0] += 500
    coarse_gt = (geotransform[0], 20.0, 0.0, geotransform[3], 0.0, -20.0)
    return {"B4": (rgb[0], geotransform), "B3": (rgb[1], geotransform), "B8A": (coarse, coarse_gt)}


def _s2_zip(
    tmp_path: Path, s2_rgb, *, empty_rows: int = 0, level2a: bool = False, deflate: bool = False
) -> Path:
    wkt = gdal.Open(str(s2_rgb)).GetProjectionRef()
    work = tmp_path / "bands"
    work.mkdir(exist_ok=True)
    members = {}
    for name, (data, geotransform) in _s2_bands(s2_rgb, empty_rows=empty_rows).items():
        code = name if name == "B8A" else f"B{int(name[1:]):02d}"
        if level2a:
            resolution = "20m" if name == "B8A" else "10m"
            member = f"IMG_DATA/R{resolution}/{TILE}_20260810T092031_{code}_{resolution}.jp2"
        else:
            member = f"IMG_DATA/{TILE}_20260810T092031_{code}.jp2"
        path = work / Path(member).name
        _jp2(path, data, geotransform, wkt)
        members[member] = path
    if level2a:
        # A coarser copy of a band, as Level-2A carries: the finest must be the one read.
        data, geotransform = _s2_bands(s2_rgb)["B4"]
        path = work / "b4_60m.jp2"
        _jp2(path, data[::6, ::6].copy(), (geotransform[0], 60.0, 0, geotransform[3], 0, -60.0), wkt)
        members[f"IMG_DATA/R60m/{TILE}_20260810T092031_B04_60m.jp2"] = path
    archive = tmp_path / f"{SAFE[:-5]}.zip"
    # The archive stores its band files; the N0500 reprocessing deflates them.
    compression = zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED
    with zipfile.ZipFile(archive, "w", compression=compression) as handle:
        handle.writestr(f"{SAFE}/MTD_MSIL1C.xml", "<metadata/>")
        for member, path in members.items():
            handle.write(path, f"{SAFE}/GRANULE/{GRANULE}/{member}")
    return archive


@pytest.fixture
def s2_zip(tmp_path, s2_rgb):
    return _s2_zip(tmp_path, s2_rgb)


def _quicklook_shrunk(archive: Path, tmp_path: Path, size: int, product: str = "false_color_vegetation"):
    """The quicklook product of the same files, made the long way and averaged down."""
    files = thumbnail_module._s2_listed_band_files(str(archive))
    sources = {
        name: {"subdataset_name": path, "band_index": 1, "resolution": 20.0 if name == "B8A" else 10.0, "epsg": "EPSG:32635"}
        for name, path in files.items()
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(s2_module, "_collect_sentinel_s2_band_sources", lambda _input: dict(sources))
        result = process_sentinel_s2_safe(
            input_dataset=str(archive),
            output_dir=tmp_path / "quicklook",
            product_bands={product: S2_DEFAULT_PRODUCTS[product]},
            output_names={product: "q.tif"},
            processing_options={"histogram_stretch": True},
        )[0]
    rgb = gdal.Open(result["path"]).ReadAsArray()
    alpha = np.where(rgb.any(axis=0), 255, 0).astype(np.uint8)
    *small, small_alpha = thumbnail_module._shrink([*rgb, alpha], size, size)
    return result, np.stack(small), small_alpha


def test_s2_thumbnail_reads_a_reduced_level_on_the_tiles_grid(s2_zip, s2_rgb):
    result = sentinel_s2_thumbnail(str(s2_zip), size=64)
    assert result["bands"] == ["B8A", "B4", "B3"]
    assert result["rgb"].shape == (3, 64, 64) and result["rgb"].dtype == np.uint8
    assert result["alpha"].shape == (64, 64)
    # The 512 px tile's smallest level at least 128 across: its 1/4 level.
    assert result["read_side"] == 128
    source = gdal.Open(str(s2_rgb))
    x0, dx, _, y0, _, dy = source.GetGeoTransform()
    assert result["geotransform"] == pytest.approx((x0, dx * 8, 0.0, y0, 0.0, dy * 8))
    assert result["crs"] == source.GetProjectionRef()
    assert result["stretch"]["percentiles"] == [0.5, 99.5] and result["stretch"]["gamma"] == 0.7


def test_s2_thumbnail_looks_like_the_quicklook_averaged_down(tmp_path, s2_zip):
    result = sentinel_s2_thumbnail(str(s2_zip), size=64)
    reference, expected, _ = _quicklook_shrunk(s2_zip, tmp_path, 64)
    difference = np.abs(result["rgb"].astype(int) - expected.astype(int))
    assert difference.mean() < 4
    assert result["stretch"]["p_low"] == pytest.approx(reference["stretch"]["p_low"], rel=0.03)
    assert result["stretch"]["p_high"] == pytest.approx(reference["stretch"]["p_high"], rel=0.05)


def test_s2_thumbnail_edge_of_the_swath_does_not_wash_it_out(tmp_path, s2_rgb):
    """Pixels part NoData at the reduced level stay out of the stretch."""
    archive = _s2_zip(tmp_path, s2_rgb, empty_rows=200)
    result = sentinel_s2_thumbnail(str(archive), size=64)
    reference, expected, _ = _quicklook_shrunk(archive, tmp_path, 64)
    assert (result["alpha"][:20] == 0).all() and (result["alpha"][30:] == 255).all()
    assert result["stretch"]["p_low"] == pytest.approx(reference["stretch"]["p_low"], rel=0.03)
    inside = result["alpha"] == 255
    assert np.abs(result["rgb"].astype(int) - expected.astype(int))[:, inside].mean() < 4


def _vsimem_files() -> set[str]:
    return set(gdal.ReadDirRecursive("/vsimem/") or [])


def test_s2_deflated_bands_are_decoded_from_a_copy_in_memory(tmp_path, s2_zip, s2_rgb, monkeypatch):
    """A deflated band file gives the stored one's pixels, decoded from a copy that is then removed."""
    (tmp_path / "deflated").mkdir()
    deflated = _s2_zip(tmp_path / "deflated", s2_rgb, deflate=True)
    files = thumbnail_module._s2_listed_band_files(str(deflated))
    assert all(thumbnail_module._deflated(path) for path in files.values())
    assert not any(thumbnail_module._deflated(path) for path in thumbnail_module._s2_listed_band_files(str(s2_zip)).values())
    opened = []
    real_open = thumbnail_module.gdal.Open
    monkeypatch.setattr(thumbnail_module.gdal, "Open", lambda path, *a: opened.append(path) or real_open(path, *a))
    before = _vsimem_files()
    result = sentinel_s2_thumbnail(str(deflated), size=64)
    assert _vsimem_files() == before
    # The grid is taken from the zip; each of the three bands is decoded from memory.
    assert sum(path.startswith("/vsimem/") for path in opened) == 3
    stored = sentinel_s2_thumbnail(str(s2_zip), size=64)
    for key in ("rgb", "alpha"):
        np.testing.assert_array_equal(result[key], stored[key])
    assert result["stretch"] == stored["stretch"] and result["read_side"] == stored["read_side"]


def test_s2_stored_bands_are_read_in_place(s2_zip, monkeypatch):
    copies = []
    monkeypatch.setattr(thumbnail_module, "_copy_to_memory", lambda path: copies.append(path) or path)
    sentinel_s2_thumbnail(str(s2_zip), size=64)
    assert copies == []


def test_s2_thumbnail_takes_a_presets_or_three_bands(s2_zip):
    named = sentinel_s2_thumbnail(str(s2_zip), size=32, bands="false_color_vegetation")
    listed = sentinel_s2_thumbnail(str(s2_zip), size=32, bands=["8a", "B04", "b3"])
    np.testing.assert_array_equal(named["rgb"], listed["rgb"])
    with pytest.raises(ValueError, match="no B2"):
        sentinel_s2_thumbnail(str(s2_zip), size=32, bands="true_color")


def test_s2_level2a_layout_reads_each_bands_finest_file(tmp_path, s2_rgb):
    archive = _s2_zip(tmp_path, s2_rgb, level2a=True)
    files = thumbnail_module._s2_listed_band_files(str(archive))
    assert files["B4"].endswith("_B04_10m.jp2") and files["B8A"].endswith("_B8A_20m.jp2")
    l2a = sentinel_s2_thumbnail(str(archive), size=64)
    (tmp_path / "l1c").mkdir()
    l1c = sentinel_s2_thumbnail(str(_s2_zip(tmp_path / "l1c", s2_rgb)), size=64)
    np.testing.assert_array_equal(l2a["rgb"], l1c["rgb"])


def test_s2_without_one_granule_asks_the_driver(tmp_path, s2_zip, monkeypatch):
    files = thumbnail_module._s2_listed_band_files(str(s2_zip))
    sources = {name: {"subdataset_name": f"SENTINEL2_L1C:{name}"} for name in files}
    monkeypatch.setattr(thumbnail_module, "_s2_listed_band_files", lambda _input: None)
    monkeypatch.setattr(thumbnail_module, "_collect_sentinel_s2_band_sources", lambda _input: sources)
    import pysent.vrt as vrt_module

    monkeypatch.setattr(vrt_module, "_s2_band_file", lambda subdataset, name: files[subdataset.split(":")[1]])
    via_driver = sentinel_s2_thumbnail("old-product.zip", size=32)
    monkeypatch.undo()
    np.testing.assert_array_equal(via_driver["rgb"], sentinel_s2_thumbnail(str(s2_zip), size=32)["rgb"])


def test_s2_listing_declines_what_it_cannot_list(tmp_path):
    assert thumbnail_module._s2_listed_band_files("S2A_OPER_MTD_SAFL1C_X.xml") is None
    safe = tmp_path / "S2A_MSIL1C_X.SAFE"
    for granule in ("L1C_A", "L1C_B"):  # a pre-2016 product: several granules
        (safe / "GRANULE" / granule / "IMG_DATA").mkdir(parents=True)
    assert thumbnail_module._s2_listed_band_files(str(safe)) is None


def test_s2_thumbnail_empty_band_raises(tmp_path, s2_rgb):
    archive = _s2_zip(tmp_path, s2_rgb, empty_rows=512)
    with pytest.raises(EmptySceneError):
        sentinel_s2_thumbnail(str(archive), size=32)


# --------------------------------------------------------------------------- #
# Sentinel-1
# --------------------------------------------------------------------------- #
def _strip_tiff(path: Path, data: np.ndarray) -> None:
    dataset = gdal.GetDriverByName("GTiff").Create(
        str(path), data.shape[1], data.shape[0], 1, gdal.GDT_UInt16, options=["BLOCKYSIZE=1"]
    )
    dataset.GetRasterBand(1).WriteArray(data)
    dataset = None


@pytest.fixture
def s1_safe(tmp_path, s1_vv, s1_vh, monkeypatch):
    """VV and VH as one-row-strip measurement TIFFs, behind a VRT standing in for manifest.safe."""
    measurement = tmp_path / "S1X.SAFE" / "measurement"
    measurement.mkdir(parents=True)
    paths = {}
    for polarization, window in (("vv", s1_vv), ("vh", s1_vh)):
        data = gdal.Open(str(window)).ReadAsArray().astype(np.uint16)
        data[:, :24] = 0  # the swath edge
        paths[polarization] = measurement / f"s1x-iw-grd-{polarization}-20260810t052300-002.tiff"
        _strip_tiff(paths[polarization], data)
    manifest = tmp_path / "manifest.vrt"
    vrt = gdal.BuildVRT(str(manifest), [str(paths["vh"]), str(paths["vv"])], separate=True)
    for index, polarization in enumerate(("VH", "VV"), start=1):
        vrt.GetRasterBand(index).SetMetadataItem("POLARIZATION", polarization)
    gcps = []
    for row in np.linspace(0, 512, 5):
        for col in np.linspace(0, 512, 5):
            lon = 20.0 + col * 2.6e-4 + row * 3.0e-5
            lat = 70.0 - row * 9.0e-5 + col * 1.0e-5
            gcps.append(gdal.GCP(lon, lat, 0.0, float(col), float(row)))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    vrt.SetGCPs(gcps, srs.ExportToWkt())
    vrt = None
    monkeypatch.setattr(thumbnail_module, "_resolve_safe_manifest_path", lambda _input: str(manifest))
    return {"input": "S1X.zip", "paths": paths}


def test_s1_thumbnail_reads_both_polarisations_onto_one_grid(s1_safe):
    result = sentinel_s1_thumbnail(s1_safe["input"], size=64)
    assert result["variables"] == ["Amplitude_VH", "Amplitude_VV"]
    assert max(result["width"], result["height"]) == 64
    for gray in result["gray"].values():
        assert gray.shape == (result["height"], result["width"]) and gray.dtype == np.uint8
    assert result["alpha"].any() and not result["alpha"].all()
    assert result["rows_read"] == 128  # twice the thumbnail
    assert result["warp_transform"] == "polynomial-gcp"
    assert "32661" in osr.SpatialReference(result["crs"]).GetAuthorityCode(None)
    assert {stats["unit"] for stats in result["stretch"].values()} == {"dB"}


def test_s1_thumbnail_range_is_the_quicklooks_range(s1_safe):
    """The percentiles of the 32-sample means sit where the full-resolution stretch puts them."""
    result = sentinel_s1_thumbnail(s1_safe["input"], size=64)
    for variable, polarization in (("Amplitude_VV", "vv"), ("Amplitude_VH", "vh")):
        data = gdal.Open(str(s1_safe["paths"][polarization])).ReadAsArray().astype(np.float32)
        # A 160 m-like mean grid of the window: 16 x 16 blocks.
        blocks = data.reshape(32, 16, 32, 16)
        count = (blocks > 0).sum(axis=(1, 3))
        mean = np.where(count > 0, blocks.sum(axis=(1, 3)) / np.maximum(count, 1), 0)
        _, _, want = stretch_sentinel_s1_grayscale(mean, nodata=0.0)
        assert result["stretch"][variable]["p_low"] == pytest.approx(want["p_low"], abs=1.0)
        assert result["stretch"][variable]["p_high"] == pytest.approx(want["p_high"], abs=1.0)


def test_s1_thumbnail_composite_is_co_cross_and_their_ratio(s1_safe):
    result = sentinel_s1_thumbnail(s1_safe["input"], size=64)
    composite = result["composite"]
    assert composite["bands"] == ["Amplitude_VV", "Amplitude_VH", "Amplitude_VV/VH"]
    assert composite["rgb"].shape == (3, result["height"], result["width"])
    inside = result["alpha"] == 255
    np.testing.assert_array_equal(composite["rgb"][0][inside], result["gray"]["Amplitude_VV"][inside])
    np.testing.assert_array_equal(composite["rgb"][1][inside], result["gray"]["Amplitude_VH"][inside])
    assert composite["ratio_stretch"]["unit"] == "dB"
    assert composite["ratio_stretch"]["p_high"] > composite["ratio_stretch"]["p_low"]
    assert composite["rgb"][2][inside].std() > 0


def test_s1_thumbnail_one_polarisation_or_linear_has_no_composite(s1_safe):
    single = sentinel_s1_thumbnail(s1_safe["input"], size=32, polarizations=["Amplitude_VV"])
    assert single["composite"] is None and list(single["gray"]) == ["Amplitude_VV"]
    linear = sentinel_s1_thumbnail(s1_safe["input"], size=32, stretch_method="linear")
    assert linear["composite"] is None
    assert {stats["unit"] for stats in linear["stretch"].values()} == {"amplitude"}


def test_s1_thumbnail_does_not_depend_on_the_thread_count(s1_safe):
    one = sentinel_s1_thumbnail(s1_safe["input"], size=48, threads=1)
    many = sentinel_s1_thumbnail(s1_safe["input"], size=48, threads=12)
    np.testing.assert_array_equal(one["composite"]["rgb"], many["composite"]["rgb"])
    assert one["stretch"] == many["stretch"]


def test_s1_thumbnail_refuses_what_it_cannot_draw(s1_safe):
    with pytest.raises(RuntimeError, match="no Amplitude_HH"):
        sentinel_s1_thumbnail(s1_safe["input"], polarizations=["Amplitude_HH"])
    with pytest.raises(ValueError, match="stretch_method"):
        sentinel_s1_thumbnail(s1_safe["input"], stretch_method="log")


def test_s1_thumbnail_empty_scene_raises(tmp_path, s1_safe):
    for path in s1_safe["paths"].values():
        _strip_tiff(path, np.zeros((512, 512), dtype=np.uint16))
    with pytest.raises(EmptySceneError):
        sentinel_s1_thumbnail(s1_safe["input"], size=32)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def test_write_thumbnail_png_writes_rgba_and_nothing_else(tmp_path, s2_zip):
    result = sentinel_s2_thumbnail(str(s2_zip), size=32)
    path = write_thumbnail_png(list(result["rgb"]), result["alpha"], tmp_path / "out" / "t.png")
    dataset = gdal.Open(str(path))
    assert dataset.GetDriver().ShortName == "PNG" and dataset.RasterCount == 4
    assert dataset.GetRasterBand(4).GetColorInterpretation() == gdal.GCI_AlphaBand
    np.testing.assert_array_equal(dataset.ReadAsArray()[:3], result["rgb"])
    dataset = None
    assert sorted(p.name for p in path.parent.iterdir()) == ["t.png"]


def test_thumbnail_functions_are_exported():
    import pysent

    assert set(thumbnail_module.__all__) >= {"sentinel_s1_thumbnail", "sentinel_s2_thumbnail", "write_thumbnail_png"}
    assert pysent.sentinel_s1_thumbnail is sentinel_s1_thumbnail
    assert pysent.sentinel_s2_thumbnail is sentinel_s2_thumbnail
    assert pysent.thumbnail is thumbnail_module
