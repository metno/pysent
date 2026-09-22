"""The Sentinel-1 grayscale stretch: dB versus linear.

See ``PLANNING/TODO_PLANNING_s1_radiometry.md``. Synthetic amplitude fields here;
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
