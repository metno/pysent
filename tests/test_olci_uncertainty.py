"""The uncertainty band, and the ways it could quietly overstate the product.

The owner's answer to the plan's Q5 is that a value ships on every clear pixel,
dark open ocean included, with its error beside it. That makes this band
load-bearing rather than decorative, so most of these tests are about what it
must *refuse* to claim.

Two of them exist because the first version got it wrong in ways that measured
fine and read fine: it published a **bias** where it needed a spread, and a
**median** where it needed a p68. Phase 6 caught both by measuring coverage -
the share of pixels whose real disagreement with ESA fell inside the published
figure - which ran at 8 to 15 % where a one-sigma band should reach 68 %.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysent._olci_uncertainty import (
    FITTED_BANDS,
    INFERRED_FROM,
    UNMEASURED_TIERS,
    VALIDATION_RESIDUAL,
    VALIDATION_SOURCE,
    loading_sensitivity,
    surface_classes,
    uncertainty,
    uncertainty_note,
)

SHAPE = (2, 3)
WATER = np.zeros(SHAPE, dtype=bool)       # land=False, bright=False
LAND = np.ones(SHAPE, dtype=bool)
BRIGHT = np.ones(SHAPE, dtype=bool)


def test_the_published_figure_is_the_measured_disagreement():
    # The whole point of the module. Over clear water at 490 nm the measured
    # p68 disagreement with OL_2_WFR is 0.0486; a propagated loading
    # uncertainty would have reported about 0.003.
    values, _ = uncertainty(4, land=WATER, tier="full")
    assert np.allclose(values, 0.0486)
    assert values.min() > 0.01, "a propagated-only figure would be ten times smaller"


def test_a_propagated_term_is_ten_times_smaller_than_the_measured_one():
    # Pins the finding that shaped the design, so nobody replaces the lookup
    # with the derivation. A 0.03 error in optical depth - four times the
    # retrieval's measured bias - moves 490 nm by about 0.003.
    measured, _ = uncertainty(4, land=WATER, tier="full")
    both, _ = uncertainty(4, land=WATER, tier="full",
                          depth_uncertainty=np.full(SHAPE, 0.03),
                          sensitivity=np.full(SHAPE, -0.106))
    propagated = abs(-0.106 * 0.03)
    assert propagated == pytest.approx(0.0032, abs=5e-4)
    assert propagated < measured[0, 0] / 10
    assert both[0, 0] == pytest.approx(np.hypot(measured[0, 0], propagated))


def test_the_figure_is_a_p68_not_a_median():
    # A median absolute residual is exceeded by half of all pixels by
    # construction, so publishing one as an uncertainty guarantees it is wrong
    # half the time. Phase 6 measured both; these are the p68s, and each is
    # comfortably above the median it replaced.
    for band, median_that_was_measured in ((4, 0.0327), (6, 0.0167), (8, 0.0049)):
        published = VALIDATION_RESIDUAL["water"][band]
        assert published > median_that_was_measured


def test_bright_surfaces_have_their_own_row_and_it_is_the_worst_one():
    # The defect Phase 6 found: before it, ice over ocean took the water row
    # and ice over land took the land row, both of which understate it. At
    # 665 nm the bright figure is eight times the water one.
    bright, _ = uncertainty(8, land=WATER, tier="full", bright=BRIGHT)
    water, _ = uncertainty(8, land=WATER, tier="full")
    assert bright[0, 0] == pytest.approx(VALIDATION_RESIDUAL["bright"][8])
    assert bright[0, 0] > water[0, 0] * 5
    for band in (4, 6, 8):
        assert VALIDATION_RESIDUAL["bright"][band] > VALIDATION_RESIDUAL["water"][band]
        assert VALIDATION_RESIDUAL["bright"][band] > VALIDATION_RESIDUAL["land"][band]


def test_a_band_the_retrieval_fits_does_not_report_its_own_residual():
    # 665 nm is driven to agreement with a near-zero reference, so its residual
    # understates the error. It takes 560 nm's figure and says that it did -
    # but only where the loading really was retrieved.
    assert 8 in FITTED_BANDS
    values, why = uncertainty(8, land=WATER, tier="full",
                              aerosol_source=np.zeros(SHAPE, int))
    assert np.allclose(values, VALIDATION_RESIDUAL["water"][INFERRED_FROM])
    assert values[0, 0] > VALIDATION_RESIDUAL["water"][8]
    assert why["inferred_from_neighbour"].all()


def test_a_band_the_retrieval_does_not_fit_reports_its_own_figure():
    assert 6 not in FITTED_BANDS
    values, why = uncertainty(6, land=WATER, tier="full",
                              aerosol_source=np.zeros(SHAPE, int))
    assert np.allclose(values, VALIDATION_RESIDUAL["water"][6])
    assert not why["inferred_from_neighbour"].any()


def test_a_pixel_whose_loading_was_not_retrieved_keeps_its_own_band():
    # The inference exists because *fitting* makes a band's residual circular.
    # A pixel filled from the climatology was never fitted, so there is nothing
    # circular about its 665 nm figure.
    values, why = uncertainty(8, land=WATER, tier="full",
                              aerosol_source=np.full(SHAPE, 2))
    assert np.allclose(values, VALIDATION_RESIDUAL["water"][8])
    assert not why["inferred_from_neighbour"].any()


def test_the_gas_only_tier_claims_nothing():
    # Every end-to-end residual includes the Rayleigh term, which over water is
    # the largest term there is, so there is no figure to lend this tier.
    assert "gas" in UNMEASURED_TIERS
    values, why = uncertainty(4, land=WATER, tier="gas")
    assert np.isnan(values).all()
    assert why["unmeasured"].all()
    assert "not measured" in uncertainty_note("gas")


def test_each_surface_is_indexed_separately():
    water, _ = uncertainty(4, land=WATER, tier="full")
    land, _ = uncertainty(4, land=LAND, tier="full")
    bright, _ = uncertainty(4, land=WATER, tier="full", bright=BRIGHT)
    assert water[0, 0] == pytest.approx(0.0486)
    assert land[0, 0] == pytest.approx(0.0329)
    assert bright[0, 0] == pytest.approx(0.0696)
    assert len({round(float(v[0, 0]), 6) for v in (water, land, bright)}) == 3


def test_a_mixed_frame_gets_each_pixel_its_own_figure():
    # A real frame is part land, part water and part ice, and nothing may
    # collapse to a frame-wide scalar.
    land = np.array([[True, False, False]])
    bright = np.array([[False, False, True]])
    values, why = uncertainty(6, land=land, tier="full", bright=bright)
    assert values[0, 0] == pytest.approx(VALIDATION_RESIDUAL["land"][6])
    assert values[0, 1] == pytest.approx(VALIDATION_RESIDUAL["water"][6])
    assert values[0, 2] == pytest.approx(VALIDATION_RESIDUAL["bright"][6])
    assert not why["unmeasured"].any()


def test_a_bright_pixel_is_bright_whichever_surface_is_under_it():
    # Ice sits on both. Before Phase 6 the two took different rows, differing
    # by a factor of four at 665 nm, for the same physical surface.
    over_water, _ = uncertainty(8, land=WATER, tier="full", bright=BRIGHT)
    over_land, _ = uncertainty(8, land=LAND, tier="full", bright=BRIGHT)
    assert over_water[0, 0] == over_land[0, 0]


def test_an_unmeasured_band_is_not_filled_in():
    # The validation covers the three visible bands. Band 17 must not silently
    # inherit one of them.
    values, why = uncertainty(17, land=WATER, tier="full")
    assert np.isnan(values).all()
    assert why["unmeasured"].all()


def test_nothing_is_added_on_top_of_an_end_to_end_measurement():
    # The tables' interpolation error, the scalar-Rayleigh error and the gas
    # coefficients' error are all already inside these numbers, because the
    # numbers are end-to-end disagreements measured through the chain that
    # commits them. The guard is that no hook exists to add them.
    with pytest.raises(TypeError):
        uncertainty(6, land=WATER, tier="full",
                    interpolation_error=np.full(SHAPE, 0.005))


def test_the_note_says_what_the_number_is_and_is_not():
    note = uncertainty_note("full")
    assert "not a propagated" in note
    assert "ESA Level-2" in note
    assert "p68" in note


def test_every_row_says_where_it_came_from():
    # A figure this load-bearing has to be traceable to the run that produced it.
    assert set(VALIDATION_SOURCE) == set(VALIDATION_RESIDUAL)
    for name, source in VALIDATION_SOURCE.items():
        assert "comparison" in source and "p68" in source, name


def test_surface_classes_are_exclusive_and_complete():
    land = np.array([[True, False, True, False]])
    bright = np.array([[True, True, False, False]])
    classes = surface_classes(land, bright)
    stack = np.stack([classes[name] for name in ("bright", "water", "land")])
    # Every pixel in exactly one class: overlapping classes would make the
    # figure depend on dict ordering.
    assert (stack.sum(axis=0) == 1).all()


def test_the_sensitivity_is_measured_through_the_real_forward_model():
    # Not an analytic derivative: the path reflectance is piecewise linear in
    # depth with a kink at every table node.
    geometry = dict(
        pressure_hpa=np.full(SHAPE, 1013.0),
        solar_zenith_deg=np.full(SHAPE, 70.0),
        view_zenith_deg=np.full(SHAPE, 20.0),
        relative_azimuth_deg=np.full(SHAPE, 90.0),
        ozone_kg_m2=np.full(SHAPE, 0.006),
        water_kg_m2=np.full(SHAPE, 5.0),
    )
    slope = loading_sensitivity(
        "S3A", 4, toa=np.full(SHAPE, 0.176),
        optical_depth_550=np.full(SHAPE, 0.044),
        angstrom=np.full(SHAPE, 0.55), geometry=geometry,
    )
    assert (slope < 0).all()
    assert np.allclose(slope, -0.10, atol=0.03)
