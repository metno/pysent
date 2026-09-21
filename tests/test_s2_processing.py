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
    S2_DEFAULT_PRODUCTS,
    _translate_sentinel_s2_rgb,
    _write_stretched_sentinel_s2_rgb,
    normalize_sentinel_s2_product_map,
    process_sentinel_s2_safe,
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
    assert stats["method"] == "percentile"
    assert len(stats["p_low"]) == 3 and len(stats["p_high"]) == 3
    assert all(high > low for low, high in zip(stats["p_low"], stats["p_high"]))


def test_normalize_product_map_rejects_unknown_bands():
    assert normalize_sentinel_s2_product_map({"rgb": ["B4", "B3", "B2"]}) == {"rgb": ("B4", "B3", "B2")}
    with pytest.raises(ValueError):
        normalize_sentinel_s2_product_map({"rgb": ["B4", "B3", "NOPE"]})


# --------------------------------------------------------------------------- #
# Phase 3: what the stretch does to valid pixels and to NoData (B5, B9)
# --------------------------------------------------------------------------- #
def _read(path):
    with rasterio.open(path) as ds:
        return ds.read(), ds.nodata


def test_valid_pixels_never_land_on_the_nodata_value(tmp_path, warped_rgb):
    # B5: with the old [0,255] scaling the darkest valid pixels became NoData,
    # so a map server drew them as holes.
    out = tmp_path / "stretched.tif"
    _write_stretched_sentinel_s2_rgb(warped_rgb, out, block_size=128, overview_factors=(2,))

    rendered, nodata = _read(out)
    source, _ = _read(warped_rgb)
    valid = np.any(source != 0, axis=0)
    assert nodata == 0
    assert rendered[:, valid].min() >= 1, "a valid pixel was rendered as NoData"
    # Real fill is still NoData, and still transparent.
    assert (rendered[:, ~valid] == 0).all()


def test_the_default_stretch_is_percentile_with_gamma(tmp_path, warped_rgb):
    out = tmp_path / "stretched.tif"
    stats = _write_stretched_sentinel_s2_rgb(warped_rgb, out, block_size=128, overview_factors=(2,))

    assert stats["method"] == "percentile"
    assert stats["percentiles"] == [0.5, 99.5]
    assert stats["gamma"] == 0.7
    # The percentile range sits inside the band's own range.
    source, _ = _read(warped_rgb)
    assert all(low >= float(source[band][source[band] > 0].min()) for band, low in enumerate(stats["p_low"]))


def test_minmax_method_uses_the_full_range(tmp_path, warped_rgb):
    out = tmp_path / "minmax.tif"
    stats = _write_stretched_sentinel_s2_rgb(
        warped_rgb, out, block_size=128, overview_factors=(2,), method="minmax", gamma=1.0
    )

    source, _ = _read(warped_rgb)
    assert stats["method"] == "minmax" and "gamma" not in stats
    for band, (low, high) in enumerate(zip(stats["p_low"], stats["p_high"])):
        values = source[band][source[band] > 0]
        assert low == float(values.min()) and high == float(values.max())


def test_percentiles_and_gamma_change_the_output(tmp_path, warped_rgb):
    wide = tmp_path / "wide.tif"
    narrow = tmp_path / "narrow.tif"
    wide_stats = _write_stretched_sentinel_s2_rgb(
        warped_rgb, wide, percentiles=(0.5, 99.5), block_size=128, overview_factors=(2,)
    )
    narrow_stats = _write_stretched_sentinel_s2_rgb(
        warped_rgb, narrow, percentiles=(20.0, 80.0), block_size=128, overview_factors=(2,)
    )

    # B9a: stretch_percentiles was ignored by the old min/max writer.
    assert narrow_stats["p_low"][0] > wide_stats["p_low"][0]
    assert narrow_stats["p_high"][0] < wide_stats["p_high"][0]

    straight = tmp_path / "gamma1.tif"
    _write_stretched_sentinel_s2_rgb(
        warped_rgb, straight, block_size=128, overview_factors=(2,), gamma=1.0
    )
    # Gamma below 1 lifts the mid-tones.
    assert _read(wide)[0].mean() > _read(straight)[0].mean()


def test_an_empty_band_still_raises_empty_scene_error(tmp_path):
    from pysent.errors import EmptySceneError

    empty = tmp_path / "empty.tif"
    with rasterio.open(
        empty, "w", driver="GTiff", height=32, width=32, count=3, dtype="uint16",
        crs="EPSG:32633", transform=from_origin(0, 1000, 10, 10), nodata=0,
    ) as dst:
        dst.write(np.zeros((3, 32, 32), dtype="uint16"))

    with pytest.raises(EmptySceneError):
        _write_stretched_sentinel_s2_rgb(empty, tmp_path / "out.tif", block_size=32, overview_factors=(2,))


def test_options_that_would_do_nothing_are_reported(tmp_path, monkeypatch):
    from pysent import s2

    monkeypatch.setattr(s2, "_prepare_sentinel_s2_stacks", lambda *a, **k: {})

    def run(**options):
        return process_sentinel_s2_safe(
            input_dataset="unused.zip", output_dir=tmp_path / "out",
            product_bands={"p": S2_DEFAULT_PRODUCTS["true_color_vegetation"]}, output_names={},
            processing_options=options,
        )

    with pytest.warns(RuntimeWarning, match="no effect with stretch_method='minmax'"):
        run(histogram_stretch=True, stretch_method="minmax", stretch_percentiles=(2, 98))
    with pytest.warns(RuntimeWarning, match="histogram_stretch is off"):
        run(stretch_percentiles=(2, 98))
    # The Sentinel-1 spelling still works, with a nudge towards the current one.
    with pytest.deprecated_call(match="stretch_percentiles"):
        run(histogram_stretch={"percentiles": (2, 98)})


def test_an_unknown_stretch_method_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="stretch_method"):
        process_sentinel_s2_safe(
            input_dataset="unused.zip", output_dir=tmp_path / "out",
            product_bands={}, output_names={},
            processing_options={"histogram_stretch": True, "stretch_method": "sqrt"},
        )
