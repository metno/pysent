"""Retrieving the aerosol loading from the frame, and refusing to.

The round-trip tests invert a forward-modelled frame, so they check the
inversion against the same tables the correction uses rather than against a
remembered number. The refusal tests matter at least as much: this method
inverts to confident nonsense over anything bright, and sea ice is bright.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysent._aerosol_retrieval import (
    CLIMATOLOGICAL_ANGSTROM,
    CLIMATOLOGICAL_DEPTH_550,
    RETRIEVAL_BANDS,
    breakpoints,
    filled_loading,
    retrieval_source_names,
    retrieve_optical_depth,
)
from pysent._atmosphere import aerosol_grid_limits, atmosphere_terms

SHAPE = (4, 3)


def geometry(**overrides):
    base = dict(
        pressure_hpa=np.full(SHAPE, 1013.0),
        solar_zenith_deg=np.full(SHAPE, 60.0),
        view_zenith_deg=np.full(SHAPE, 20.0),
        relative_azimuth_deg=np.full(SHAPE, 90.0),
        ozone_kg_m2=np.full(SHAPE, 0.006),
        water_kg_m2=np.full(SHAPE, 5.0),
    )
    base.update(overrides)
    return base


def forward(depth, angstrom, **overrides):
    """Top-of-atmosphere reflectance over a black surface at this loading."""
    geo = geometry(**overrides)
    toa = {}
    for band in RETRIEVAL_BANDS:
        terms = atmosphere_terms(
            "S3A", band,
            optical_depth_550=np.full(SHAPE, depth),
            angstrom=np.full(SHAPE, angstrom),
            **geo,
        )
        # A black surface, so the whole signal is the path, dimmed by the gas
        # the retrieval will divide back out.
        toa[band] = terms["path_reflectance"] * terms["gas_transmittance"]
    return toa, geo


@pytest.mark.parametrize(
    "depth,angstrom",
    [(0.04, 0.5), (0.12, 0.8), (0.2, 1.2), (0.3, 0.3)],
)
def test_the_retrieval_inverts_the_forward_model(depth, angstrom):
    # The strongest check available without a reference: forward-model a known
    # loading over a black surface, then ask what loading explains it.
    toa, geo = forward(depth, angstrom)
    out = retrieve_optical_depth(
        "S3A", toa=toa, usable=np.ones(SHAPE, bool), **geo
    )
    assert np.allclose(out["optical_depth_550"], depth)
    assert np.allclose(out["angstrom"], angstrom)
    # **Exact, and none of these depths is one of the table's own nodes** -
    # they are 0, 0.05, 0.15 and 0.4. The inversion is the exact inverse of
    # the forward model rather than a search over a grid of candidates, because
    # the path reflectance is piecewise linear in optical depth between those
    # nodes. A grid search would snap each of these to its nearest candidate
    # and cost forty times as much.
    assert np.nanmax(out["residual"]) == pytest.approx(0.0, abs=1e-9)
    assert depth not in list(np.asarray(breakpoints("S3A")))


def test_the_retrieval_refuses_over_anything_not_clear_water():
    # Sea ice, snow and cloud reflect 0.3 to 0.9 where this method assumes
    # zero, and OLCI Level-1 has no ice flag - ice, snow and cloud all arrive
    # as "bright". So the caller's mask is the only line, and a masked pixel
    # must come back empty with the reason named, not merely empty.
    toa, geo = forward(0.12, 0.8)
    usable = np.ones(SHAPE, bool)
    usable[0, 0] = False
    out = retrieve_optical_depth("S3A", toa=toa, usable=usable, **geo)
    assert np.isnan(out["optical_depth_550"][0, 0])
    assert np.isnan(out["angstrom"][0, 0])
    assert out["refused"]["not_clear_water"][0, 0]
    assert not out["refused"]["not_clear_water"][1, 1]
    assert np.isfinite(out["optical_depth_550"][1, 1])


def test_a_bright_surface_is_refused_even_if_the_caller_forgets_the_mask():
    # Defence in depth. A sea-ice pixel let through by mistake reflects far
    # more than any atmosphere in the table can explain, so the search runs to
    # its top and the pixel is refused as hazier than the table rather than
    # being handed back a confident maximum.
    toa, geo = forward(0.12, 0.8)
    ice = {band: np.full(SHAPE, 0.6) for band in RETRIEVAL_BANDS}
    out = retrieve_optical_depth(
        "S3A", toa=ice, usable=np.ones(SHAPE, bool), **geo
    )
    assert out["refused"]["brighter_than_the_table_explains"].all()
    assert np.isnan(out["optical_depth_550"]).all()


def test_a_pixel_darker_than_clean_air_is_refused_not_called_clean():
    # Below the path reflectance of an aerosol-free atmosphere there is no
    # optical depth that fits. Reporting zero would be a retrieval of "clean
    # air" where the honest answer is that the molecular model already
    # over-explains the measurement - the same reasoning as keeping a negative
    # surface reflectance rather than clipping it.
    toa, geo = forward(0.0, 0.5)
    darker = {band: values * 0.5 for band, values in toa.items()}
    out = retrieve_optical_depth(
        "S3A", toa=darker, usable=np.ones(SHAPE, bool), **geo
    )
    assert out["refused"]["darker_than_clean_air"].all()
    assert np.isnan(out["optical_depth_550"]).all()


def test_a_haze_beyond_the_table_is_refused_rather_than_clamped():
    top = aerosol_grid_limits("S3A", RETRIEVAL_BANDS[-1])["optical_depth_550"][1]
    toa, geo = forward(top, 0.8)
    out = retrieve_optical_depth("S3A", toa=toa, usable=np.ones(SHAPE, bool), **geo)
    # At the very top of the search the real depth may be beyond the table, so
    # the pixel says so instead of reporting the edge as a measurement.
    assert out["refused"]["brighter_than_the_table_explains"].all()
    assert np.isnan(out["optical_depth_550"]).all()


def test_a_missing_measurement_is_refused_with_its_own_reason():
    toa, geo = forward(0.12, 0.8)
    toa = {band: values.copy() for band, values in toa.items()}
    toa[RETRIEVAL_BANDS[0]][2, 1] = np.nan
    out = retrieve_optical_depth("S3A", toa=toa, usable=np.ones(SHAPE, bool), **geo)
    assert out["refused"]["missing_input"][2, 1]
    assert np.isnan(out["optical_depth_550"][2, 1])


def test_the_retrieval_says_which_band_it_is_missing():
    toa, geo = forward(0.12, 0.8)
    del toa[RETRIEVAL_BANDS[0]]
    with pytest.raises(ValueError, match="top-of-atmosphere reflectance for bands"):
        retrieve_optical_depth("S3A", toa=toa, usable=np.ones(SHAPE, bool), **geo)


def test_the_retrieval_uses_two_bands_with_enough_separation():
    # Why this pair: both are nearly black over clear water, and ln(865/665)
    # is 0.262 against 0.165 for ln(1020/865), so these two carry more
    # spectral leverage than adding the 1020 nm band would - and the committed
    # table already covers them.
    assert RETRIEVAL_BANDS == (8, 17)
    assert np.log(865.0 / 665.0) > np.log(1020.0 / 865.0)


def test_the_fill_order_is_measured_then_product_then_stated_assumption():
    retrieved = {
        "optical_depth_550": np.array([0.08, np.nan, np.nan]),
        "angstrom": np.array([0.9, np.nan, np.nan]),
    }
    filled = filled_loading(
        retrieved,
        product_depth=np.array([0.2, 0.15, np.nan]),
        product_angstrom=np.array([1.1, 1.0, np.nan]),
    )
    # The retrieval wins where it has an answer, the product fills the next
    # gap, and only the last pixel falls back to the stated assumption.
    assert filled["optical_depth_550"] == pytest.approx([0.08, 0.15, CLIMATOLOGICAL_DEPTH_550])
    assert filled["angstrom"] == pytest.approx([0.9, 1.0, CLIMATOLOGICAL_ANGSTROM])
    assert filled["source"].tolist() == [0, 1, 2]
    assert retrieval_source_names()[0] == "retrieved"
    assert retrieval_source_names()[2] == "climatological"


def test_the_fallback_is_this_archive_s_median_and_not_clean_air():
    # Assuming no aerosol where there is typically some under-corrects every
    # filled pixel, so the fallback is the measured median rather than zero -
    # and it is recorded as an assumption, which is what the source array is
    # for.
    assert CLIMATOLOGICAL_DEPTH_550 > 0.0
    assert 0.01 < CLIMATOLOGICAL_DEPTH_550 < 0.1
    assert 0.2 < CLIMATOLOGICAL_ANGSTROM < 1.5


def test_filling_without_a_product_falls_straight_to_the_assumption():
    retrieved = {
        "optical_depth_550": np.array([0.08, np.nan]),
        "angstrom": np.array([0.9, np.nan]),
    }
    filled = filled_loading(retrieved)
    assert filled["optical_depth_550"] == pytest.approx([0.08, CLIMATOLOGICAL_DEPTH_550])
    assert filled["source"].tolist() == [0, 2]


def test_the_retrieval_is_elementwise_over_a_field():
    # A frame has a different loading in every pixel, so nothing may collapse
    # to a scalar on the way through.
    geo = geometry()
    depths = np.array([[0.04, 0.08, 0.12], [0.16, 0.2, 0.24], [0.04, 0.08, 0.12],
                       [0.16, 0.2, 0.24]])
    toa = {}
    for band in RETRIEVAL_BANDS:
        terms = atmosphere_terms(
            "S3A", band, optical_depth_550=depths,
            angstrom=np.full(SHAPE, 0.8), **geo,
        )
        toa[band] = terms["path_reflectance"] * terms["gas_transmittance"]
    out = retrieve_optical_depth("S3A", toa=toa, usable=np.ones(SHAPE, bool), **geo)
    assert out["optical_depth_550"].shape == SHAPE
    assert np.allclose(out["optical_depth_550"], depths)
