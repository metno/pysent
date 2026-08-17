"""Processing-level tests for the Sentinel-2 writer.

These need the geo stack (rasterio + GDAL bindings) but no SAFE product: the
input is a synthetic warped raster.
"""
import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
pytest.importorskip("osgeo")

from rasterio.transform import from_origin  # noqa: E402

from pysent.s2 import (  # noqa: E402
    _translate_sentinel_s2_rgb,
    _write_stretched_sentinel_s2_rgb,
    normalize_sentinel_s2_product_map,
)


@pytest.fixture
def warped_rgb(tmp_path):
    """A small synthetic UInt16 RGB raster standing in for a warped S2 product."""
    warped = tmp_path / "warp.tif"
    height = width = 256
    rng = np.random.default_rng(0)
    rgb = rng.integers(1, 9000, size=(3, height, width)).astype("uint16")
    rgb[:, :20, :] = 0  # nodata strip
    with rasterio.open(
        warped, "w", driver="GTiff", height=height, width=width, count=3,
        dtype="uint16", crs="EPSG:32633", transform=from_origin(0, 1000, 10, 10), nodata=0,
    ) as dst:
        dst.write(rgb)
    return warped


def test_non_histogram_translator_is_defined():
    # Regression: the call site referenced `_translate_sentinel_s2_rgb` while only
    # an `_old`-suffixed variant existed, so the non-histogram branch raised NameError.
    assert callable(_translate_sentinel_s2_rgb)


def test_write_stretched_rgb_outputs_tiled_compressed_uint8(tmp_path, warped_rgb):
    out = tmp_path / "stretched.tif"
    stats = _write_stretched_sentinel_s2_rgb(
        warped_rgb, out, percentiles=(2.0, 98.0), block_size=128,
        overview_factors=(2, 4), compression="DEFLATE",
    )

    # A proper tiled + compressed 8-bit RGB with overviews - not the old striped,
    # uncompressed Translate that dropped its creation options.
    assert out.exists()
    with rasterio.open(out) as ds:
        assert ds.count == 3 and ds.dtypes[0] == "uint8"
        assert ds.profile.get("tiled") is True
        assert ds.profile.get("blockxsize") == 128
        assert (ds.profile.get("compress") or "").lower() == "deflate"
        assert ds.overviews(1) == [2, 4]

    # Stats are returned (the old writer returned None).
    assert stats["method"] == "minmax"
    assert len(stats["p_low"]) == 3 and len(stats["p_high"]) == 3
    assert all(high > low for low, high in zip(stats["p_low"], stats["p_high"]))


def test_normalize_product_map_rejects_unknown_bands():
    assert normalize_sentinel_s2_product_map({"rgb": ["B4", "B3", "B2"]}) == {"rgb": ("B4", "B3", "B2")}
    with pytest.raises(ValueError):
        normalize_sentinel_s2_product_map({"rgb": ["B4", "B3", "NOPE"]})
