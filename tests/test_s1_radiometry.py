"""The Sentinel-1 grayscale stretch: dB versus linear.

See ``PLANNING/PLANNING_s1_radiometry.md``. Synthetic amplitude fields here;
``test_real_scenes.py`` exercises the same code against real radiometry.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("rasterio")
pytest.importorskip("osgeo")

from pysent.s1 import (  # noqa: E402
    S1_STRETCH_METHOD,
    S1_STRETCH_PERCENTILES,
    _resolve_sentinel_s1_processing_settings,
    stretch_sentinel_s1_grayscale,
)


@pytest.fixture
def amplitude() -> np.ndarray:
    """A speckled field spanning three orders of magnitude, with a fill strip.

    Gamma-distributed intensity is what a multi-looked SAR scene actually looks
    like; the three patches stand in for water, land and a bright target.
    """
    rng = np.random.default_rng(0)
    intensity = np.empty((256, 256), dtype=np.float32)
    intensity[:, :128] = rng.gamma(4.4, 50.0 / 4.4, size=(256, 128))      # dark: water
    intensity[:, 128:224] = rng.gamma(4.4, 4000.0 / 4.4, size=(256, 96))  # mid: land
    intensity[:, 224:] = rng.gamma(4.4, 90000.0 / 4.4, size=(256, 32))    # bright: urban
    data = np.sqrt(intensity).astype(np.float32)
    data[:16, :] = 0.0  # fill
    return data


def test_the_default_is_a_db_stretch(amplitude):
    gray, alpha, stats = stretch_sentinel_s1_grayscale(amplitude, nodata=0.0)

    assert S1_STRETCH_METHOD == "db"
    assert stats["unit"] == "dB"
    # The range is reported in dB, so it is far smaller than the amplitudes.
    assert 0 < stats["p_low"] < stats["p_high"] < 120
    assert gray.dtype == np.uint8 and alpha.dtype == np.uint8


def test_db_gives_the_dark_end_more_room_than_linear(amplitude):
    dark = amplitude[:, :128] > 0
    db_gray, _, _ = stretch_sentinel_s1_grayscale(amplitude, nodata=0.0)
    linear_gray, _, linear_stats = stretch_sentinel_s1_grayscale(amplitude, nodata=0.0, method="linear")

    # The whole point of the curve: water stops being a black smear.
    db_water = db_gray[16:, :128]
    linear_water = linear_gray[16:, :128]
    assert db_water.mean() > linear_water.mean() * 3
    assert db_water.std() > linear_water.std() * 3
    assert linear_stats["unit"] == "amplitude"
    assert dark.any()


def test_valid_pixels_keep_alpha_and_fill_does_not(amplitude):
    for method in ("db", "linear"):
        gray, alpha, _ = stretch_sentinel_s1_grayscale(amplitude, nodata=0.0, method=method)
        valid = amplitude > 0
        assert (alpha[valid] == 255).all(), method
        assert (alpha[~valid] == 0).all(), method
        assert (gray[~valid] == 0).all(), method


def test_percentiles_set_the_clip(amplitude):
    wide = stretch_sentinel_s1_grayscale(amplitude, nodata=0.0, percentiles=(1.0, 99.0))[2]
    narrow = stretch_sentinel_s1_grayscale(amplitude, nodata=0.0, percentiles=(20.0, 80.0))[2]

    assert S1_STRETCH_PERCENTILES == (1.0, 99.0)
    assert narrow["p_low"] > wide["p_low"]
    assert narrow["p_high"] < wide["p_high"]


def test_an_empty_scene_reports_the_unit(amplitude):
    gray, alpha, stats = stretch_sentinel_s1_grayscale(np.zeros_like(amplitude), nodata=0.0)

    assert not gray.any() and not alpha.any()
    assert stats["unit"] == "dB" and stats["p_high"] == 0.0


@pytest.mark.parametrize("method", ["db", "linear"])
def test_the_numba_kernel_matches_numpy(amplitude, method):
    pytest.importorskip("numba")
    reference = stretch_sentinel_s1_grayscale(amplitude, nodata=0.0, method=method)
    accelerated = stretch_sentinel_s1_grayscale(amplitude, nodata=0.0, method=method, use_numba=True)

    # float32 rounding differs by at most one grey level, as in the linear path.
    assert np.abs(accelerated[0].astype(int) - reference[0]).max() <= 1
    assert np.array_equal(accelerated[1], reference[1])


def test_an_unknown_method_is_rejected(amplitude):
    with pytest.raises(ValueError, match="method"):
        stretch_sentinel_s1_grayscale(amplitude, nodata=0.0, method="sqrt")
    # Case and stray whitespace are tolerated.
    assert stretch_sentinel_s1_grayscale(amplitude, nodata=0.0, method=" DB ")[2]["unit"] == "dB"


def test_processing_options_carry_the_method():
    assert _resolve_sentinel_s1_processing_settings({}, output_count=1)["stretch_method"] == "db"
    assert _resolve_sentinel_s1_processing_settings(
        {"stretch_method": "linear"}, output_count=1
    )["stretch_method"] == "linear"
    with pytest.raises(ValueError, match="stretch_method"):
        _resolve_sentinel_s1_processing_settings({"stretch_method": "log"}, output_count=1)


# --------------------------------------------------------------------------- #
# Speckle filtering
# --------------------------------------------------------------------------- #
from pysent.s1 import (  # noqa: E402
    _box_sum,
    despeckle_sentinel_s1,
    estimate_equivalent_looks,
)


@pytest.fixture
def speckled_edge() -> np.ndarray:
    """Two homogeneous fields either side of a hard edge, with 4-look speckle."""
    rng = np.random.default_rng(1)
    truth = np.full((256, 256), 400.0, dtype=np.float64)
    truth[:, 128:] = 3600.0
    intensity = rng.gamma(4.0, truth / 4.0)
    return np.sqrt(intensity).astype(np.float32)


def test_the_filter_removes_speckle(speckled_edge):
    before = estimate_equivalent_looks(speckled_edge)
    filtered, stats = despeckle_sentinel_s1(speckled_edge, window=5)
    after = estimate_equivalent_looks(filtered)

    assert 3 < before < 6, f"a 4-look field should estimate near 4, got {before}"
    assert after > before * 3
    assert stats["filter"] == "lee" and stats["window"] == 5
    assert stats["looks"] == pytest.approx(before, rel=0.2)


def test_the_filter_keeps_the_edge_where_a_blur_would_not(speckled_edge):
    filtered, _ = despeckle_sentinel_s1(speckled_edge, window=5)
    valid = np.ones_like(speckled_edge, dtype=bool)
    blurred = np.sqrt(_box_sum(speckled_edge.astype(np.float64) ** 2, 5) / _box_sum(valid.astype(np.float64), 5))

    # Across the edge (columns 126-130), the step should stay sharp.
    def step(image):
        return float(image[:, 130:134].mean() - image[:, 124:128].mean())

    truth_step = 60.0 - 20.0  # sqrt(3600) - sqrt(400)
    assert step(filtered) > step(blurred)
    assert step(filtered) > 0.8 * truth_step


def test_fill_is_left_alone_and_does_not_bleed_in(speckled_edge):
    data = speckled_edge.copy()
    data[:32, :] = 0.0  # fill strip

    filtered, _ = despeckle_sentinel_s1(data, nodata=0.0, window=5)

    assert (filtered[:32, :] == 0).all()
    # The first valid rows keep the level of their own side of the image.
    assert filtered[32:40, :128].mean() == pytest.approx(20.0, rel=0.25)


def test_a_window_of_one_changes_nothing(speckled_edge):
    filtered, stats = despeckle_sentinel_s1(speckled_edge, window=1)

    assert np.array_equal(filtered, speckled_edge)
    assert stats["looks"] is None


def test_an_explicit_looks_value_overrides_the_estimate(speckled_edge):
    _, stats = despeckle_sentinel_s1(speckled_edge, window=5, looks=2.0)
    assert stats["looks"] == 2.0


def test_a_bad_window_is_rejected(speckled_edge):
    with pytest.raises(ValueError, match="window"):
        despeckle_sentinel_s1(speckled_edge, window=0)


def test_an_empty_raster_is_returned_untouched(speckled_edge):
    empty = np.zeros_like(speckled_edge)
    filtered, stats = despeckle_sentinel_s1(empty, nodata=0.0)
    assert not filtered.any() and stats["looks"] is None


def test_the_filter_is_off_by_default_and_reaches_the_writer():
    off = _resolve_sentinel_s1_processing_settings({}, output_count=1)
    assert off["speckle_filter"] == "none" and off["speckle_window"] == 5

    on = _resolve_sentinel_s1_processing_settings(
        {"speckle_filter": "lee", "speckle_window": 7, "speckle_looks": 3.0}, output_count=1
    )
    assert (on["speckle_filter"], on["speckle_window"], on["speckle_looks"]) == ("lee", 7, 3.0)

    with pytest.raises(ValueError, match="speckle_filter"):
        _resolve_sentinel_s1_processing_settings({"speckle_filter": "frost"}, output_count=1)


def test_the_filter_matches_a_direct_implementation(speckled_edge):
    """The integral-image version must agree with the textbook O(n*w^2) form."""
    small = speckled_edge[:48, :48].astype(np.float64)
    window, looks, radius = 5, 4.0, 2
    intensity = small ** 2
    expected = np.zeros_like(small)
    for row in range(small.shape[0]):
        for col in range(small.shape[1]):
            box = intensity[max(0, row - radius):row + radius + 1, max(0, col - radius):col + radius + 1]
            mean, variance = box.mean(), box.var()
            weight = max(variance - mean ** 2 / looks, 0.0) / max(variance, 1e-9)
            expected[row, col] = np.sqrt(max(mean + weight * (intensity[row, col] - mean), 0.0))

    filtered, _ = despeckle_sentinel_s1(small.astype(np.float32), window=window, looks=looks)

    assert np.allclose(filtered, expected, rtol=1e-4, atol=1e-3)


def test_striping_does_not_change_the_result(speckled_edge, monkeypatch):
    from pysent import s1 as s1_module

    whole, _ = despeckle_sentinel_s1(speckled_edge, window=5, looks=4.0)
    monkeypatch.setattr(s1_module, "_SPECKLE_STRIP_ROWS", 37)  # force many ragged strips
    striped, _ = despeckle_sentinel_s1(speckled_edge, window=5, looks=4.0)

    assert np.allclose(whole, striped, rtol=1e-5, atol=1e-3)
