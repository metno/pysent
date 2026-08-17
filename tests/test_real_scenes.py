"""Tests against real Sentinel radiometry.

The synthetic tests elsewhere check mechanics; these check that the stretch
behaves sensibly on actual scenes, where the histogram is skewed, speckle is
present and part of the frame is fill. See ``tests/conftest.py`` for how the
artifacts are produced.
"""
import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")

from pysent.s1 import S1_STRETCH_PERCENTILES, stretch_sentinel_s1_grayscale  # noqa: E402
from pysent.s2 import S2_STRETCH_PERCENTILES, stretch_sentinel_s2_rgb  # noqa: E402


def _read(path, indexes=1):
    with rasterio.open(path) as src:
        return src.read(indexes).astype(np.float32), src.nodata


# --------------------------------------------------------------------------- #
# Sentinel-1
# --------------------------------------------------------------------------- #
def test_s1_stretch_produces_usable_grayscale(s1_vv):
    data, nodata = _read(s1_vv)
    gray, alpha, stats = stretch_sentinel_s1_grayscale(
        data, nodata=nodata, percentiles=S1_STRETCH_PERCENTILES
    )

    assert gray.dtype == np.uint8 and alpha.dtype == np.uint8
    assert gray.shape == data.shape
    assert stats["p_high"] > stats["p_low"] > 0

    # A real SAR window should use most of the 8-bit range without collapsing
    # to a single value - the failure mode when percentiles are misapplied.
    assert gray.max() > 200, "stretch did not reach the top of the range"
    assert gray.std() > 20, f"suspiciously flat output (std={gray.std():.1f})"


def test_s1_stretch_clips_roughly_the_requested_tails(s1_vv):
    data, nodata = _read(s1_vv)
    gray, _, _ = stretch_sentinel_s1_grayscale(data, nodata=nodata, percentiles=(2.0, 98.0))
    valid = data > 0
    # 2/98 percentiles clip ~2% per tail; allow generous slack for ties in the
    # integer histogram, but catch order-of-magnitude mistakes.
    assert 0.0 < (gray[valid] == 0).mean() < 0.10
    assert 0.0 < (gray[valid] == 255).mean() < 0.10


def test_s1_narrower_percentiles_clip_more(s1_vv):
    data, nodata = _read(s1_vv)
    wide, _, _ = stretch_sentinel_s1_grayscale(data, nodata=nodata, percentiles=(1.0, 99.0))
    narrow, _, _ = stretch_sentinel_s1_grayscale(data, nodata=nodata, percentiles=(10.0, 90.0))
    valid = data > 0
    wide_clipped = ((wide[valid] == 0) | (wide[valid] == 255)).mean()
    narrow_clipped = ((narrow[valid] == 0) | (narrow[valid] == 255)).mean()
    assert narrow_clipped > wide_clipped


def test_s1_polarizations_are_co_registered(s1_vv, s1_vh):
    """The generator cuts both polarisations from the same window."""
    with rasterio.open(s1_vv) as vv, rasterio.open(s1_vh) as vh:
        assert (vv.width, vv.height) == (vh.width, vh.height)
        assert vv.transform == vh.transform


def test_s1_vv_is_brighter_than_vh(s1_vv, s1_vh):
    """Co-polarised backscatter exceeds cross-polarised - a physical sanity check
    that the two fixtures were not accidentally written from the same band."""
    vv, _ = _read(s1_vv)
    vh, _ = _read(s1_vh)
    assert vv[vv > 0].mean() > vh[vh > 0].mean()


# --------------------------------------------------------------------------- #
# Sentinel-2
# --------------------------------------------------------------------------- #
def test_s2_stretch_produces_usable_rgb(s2_rgb):
    data, nodata = _read(s2_rgb, indexes=[1, 2, 3])
    stretched, stats = stretch_sentinel_s2_rgb(
        data, nodata=nodata, percentiles=S2_STRETCH_PERCENTILES
    )

    assert stretched.dtype == np.uint8
    assert stretched.shape == data.shape
    assert len(stats["p_low"]) == 3 and len(stats["p_high"]) == 3
    assert all(hi > lo for lo, hi in zip(stats["p_low"], stats["p_high"]))
    for band in range(3):
        assert stretched[band].std() > 10, f"band {band} came out flat"


def test_s2_per_band_ranges_differ(s2_rgb):
    """Per-band stretch: red/green/blue have genuinely different distributions,
    so identical ranges across bands would mean the per-band path is not running."""
    data, nodata = _read(s2_rgb, indexes=[1, 2, 3])
    _, stats = stretch_sentinel_s2_rgb(data, nodata=nodata, percentiles=(2.0, 98.0))
    assert len(set(stats["p_high"])) > 1


def test_s2_fixture_is_georeferenced(s2_rgb):
    """S2 windows keep a real UTM geotransform (gdal.Translate shifts the origin)."""
    with rasterio.open(s2_rgb) as src:
        assert src.crs is not None and src.crs.to_epsg() is not None
        assert src.res[0] == pytest.approx(10.0), "expected the 10 m bands"


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #
def test_manifest_records_real_product_provenance(data_manifest):
    for platform in ("S1", "S2"):
        product = data_manifest["products"][platform]
        assert product["identifier"], f"{platform} artifact has no source UUID"
        assert product["title"].startswith(platform[:2])
        assert "nbsArchive/" in product["download_url"]
