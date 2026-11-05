"""The uncertainty band, and the four ways it could quietly overstate the product.

The owner's answer to the plan's Q5 is that a value ships on every clear pixel,
dark open ocean included, with its error beside it. That makes this band
load-bearing rather than decorative, so most of these tests are about what it
must *refuse* to claim.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysent._olci_uncertainty import (
    CLIMATOLOGICAL_EXTRA,
    FITTED_BANDS,
    INFERRED_FROM,
    UNMEASURED_TIERS,
    VALIDATION_RESIDUAL,
    loading_sensitivity,
    surface_classes,
    uncertainty,
    uncertainty_note,
)

SHAPE = (2, 3)
WATER = np.zeros(SHAPE, dtype=bool)       # land=False, so every pixel is water
LAND = np.ones(SHAPE, dtype=bool)


def test_the_published_figure_is_the_measured_residual_not_a_propagated_one():
    # The whole point of the module. Over clear water at 490 nm the measured
    # disagreement with OL_2_WFR is 0.0300; a propagated loading uncertainty
    # would have reported about 0.003. If this ever returns the smaller number,
    # the product has started lying about itself.
    values, _ = uncertainty(4, land=WATER, tier="full",
                            aerosol_source=np.zeros(SHAPE, int))
    assert np.allclose(values, 0.0300)
    assert values.min() > 0.01, "a propagated-only figure would be ten times smaller"


def test_a_propagated_term_is_ten_times_smaller_than_the_measured_one():
    # Pins the finding that motivated the design, so nobody "simplifies" the
    # lookup away in favour of the derivation. A 0.03 error in optical depth -
    # four times the retrieval's measured bias - moves 490 nm by about 0.003.
    sensitivity = np.full(SHAPE, -0.106)
    measured, _ = uncertainty(4, land=WATER, tier="full",
                              aerosol_source=np.zeros(SHAPE, int))
    both, _ = uncertainty(4, land=WATER, tier="full",
                          aerosol_source=np.zeros(SHAPE, int),
                          depth_uncertainty=np.full(SHAPE, 0.03),
                          sensitivity=sensitivity)
    propagated = abs(-0.106 * 0.03)
    assert propagated == pytest.approx(0.0032, abs=5e-4)
    assert propagated < measured[0, 0] / 5
    # Added in quadrature, so it moves the total barely at all - which is the
    # honest effect of a small term, not a reason to drop it.
    assert both[0, 0] == pytest.approx(np.hypot(measured[0, 0], propagated))
    assert both[0, 0] > measured[0, 0]


def test_a_band_the_retrieval_fits_does_not_report_its_own_residual():
    # 665 nm is one of the two bands the retrieval drives to agreement with a
    # near-zero reference, so its 0.0037 residual is close to a tautology and
    # must never be published as an accuracy. It takes 560 nm's figure instead
    # and says that it did.
    assert 8 in FITTED_BANDS
    values, why = uncertainty(8, land=WATER, tier="full",
                              aerosol_source=np.zeros(SHAPE, int))
    assert np.allclose(values, VALIDATION_RESIDUAL[("water", "retrieved")][INFERRED_FROM])
    assert not np.allclose(values, 0.0037)
    assert why["inferred_from_neighbour"].all()


def test_a_band_the_retrieval_does_not_fit_reports_its_own_figure():
    # The counterpart: 560 nm is never looked at by the retrieval, so its own
    # measured residual is the honest figure and is used unflagged.
    assert 6 not in FITTED_BANDS
    values, why = uncertainty(6, land=WATER, tier="full",
                              aerosol_source=np.zeros(SHAPE, int))
    assert np.allclose(values, 0.0142)
    assert not why["inferred_from_neighbour"].any()


def test_a_guessed_loading_pays_the_measured_price_of_guessing():
    # The climatological fallback has no validation row of its own, but unlike
    # the gas tier it is measurable: across the 47 frames with a reported
    # AOD_550 the stated 0.044 is off by 0.0346 at one sigma, and the
    # sensitivity is 0.10 per unit depth, so guessing costs about 0.0035 of
    # reflectance. It borrows the product row and adds that in quadrature.
    assert "climatological" not in UNMEASURED_TIERS
    assert CLIMATOLOGICAL_EXTRA == pytest.approx(0.00346, abs=1e-4)
    borrowed = VALIDATION_RESIDUAL[("water", "product")][6]
    values, why = uncertainty(6, land=WATER, tier="full",
                              aerosol_source=np.full(SHAPE, 2))
    assert np.allclose(values, np.hypot(borrowed, CLIMATOLOGICAL_EXTRA))
    # Strictly worse than a measured loading, which is the point of the term.
    assert values[0, 0] > borrowed
    assert not why["unmeasured"].any()


def test_guessing_over_land_is_measurable_rather_than_unknowable():
    # This is most of a land frame, and leaving it NaN was wasteful rather than
    # honest: on the first real frame it put 67 % of the output beyond the
    # uncertainty band's reach.
    values, why = uncertainty(4, land=LAND, tier="full",
                              aerosol_source=np.full(SHAPE, 2))
    assert np.isfinite(values).all()
    assert not why["unmeasured"].any()
    assert values[0, 0] == pytest.approx(
        np.hypot(VALIDATION_RESIDUAL[("land", "product")][4], CLIMATOLOGICAL_EXTRA)
    )


def test_the_gas_only_tier_claims_nothing():
    # Every end-to-end residual in the plan includes the Rayleigh term, which
    # over water is the largest term there is. Lending the gas tier the
    # gas+Rayleigh figures would understate it by most of the Rayleigh path
    # reflectance, so it reports nothing instead.
    assert "gas" in UNMEASURED_TIERS
    values, why = uncertainty(4, land=WATER, tier="gas")
    assert np.isnan(values).all()
    assert why["unmeasured"].all()
    assert "not measured" in uncertainty_note("gas")


def test_land_and_water_are_indexed_separately():
    # The two references are different products and disagree by a factor of
    # five at 490 nm, so one figure for both surfaces would be wrong on each.
    water, _ = uncertainty(4, land=WATER, tier="rayleigh")
    land, _ = uncertainty(4, land=LAND, tier="rayleigh")
    assert water[0, 0] == pytest.approx(0.0592)
    assert land[0, 0] == pytest.approx(0.0107)
    assert water[0, 0] > land[0, 0] * 4


def test_a_mixed_frame_gets_each_pixel_its_own_figure():
    # A real frame is part land and part water, and the per-pixel aerosol source
    # varies within each. Nothing may collapse to a frame-wide scalar.
    land = np.array([[True, False, False], [True, True, False]])
    source = np.array([[1, 0, 2], [1, 1, 0]])
    values, why = uncertainty(6, land=land, tier="full", aerosol_source=source)
    assert values[0, 0] == pytest.approx(VALIDATION_RESIDUAL[("land", "product")][6])
    assert values[0, 1] == pytest.approx(VALIDATION_RESIDUAL[("water", "retrieved")][6])
    # The climatological pixel is water here, so it borrows the water product
    # row and pays the stand-in term - a different number from either neighbour.
    assert values[0, 2] == pytest.approx(
        np.hypot(VALIDATION_RESIDUAL[("water", "product")][6], CLIMATOLOGICAL_EXTRA)
    )
    assert not why["unmeasured"].any()
    # The three sources must not collapse to one figure.
    assert len({round(float(v), 6) for v in values[0]}) == 3


def test_a_land_pixel_with_a_retrieved_loading_is_unmeasured():
    # The retrieval is water-only by design (OLCI has no shortwave-infrared
    # band, so there is no dark target over land), so there is no land row for
    # it and a pixel claiming one gets no figure rather than the product row's.
    assert ("land", "retrieved") not in VALIDATION_RESIDUAL
    values, why = uncertainty(6, land=LAND, tier="full",
                              aerosol_source=np.zeros(SHAPE, int))
    assert np.isnan(values).all()
    assert why["unmeasured"].all()


def test_an_unmeasured_band_is_not_filled_in():
    # The validation covers the three visible bands. Asking about band 17 must
    # not silently inherit a visible band's figure.
    values, why = uncertainty(17, land=WATER, tier="rayleigh")
    assert np.isnan(values).all()
    assert why["unmeasured"].all()


def test_the_table_interpolation_error_is_not_added_on_top():
    # It looks like a missing term and it is not. The figures are residuals
    # measured *through these same tables*, so every interpolation error the
    # tables commit is already inside the number. Adding it again double-counts
    # it - and the same argument rules out the scalar-Rayleigh and gas terms.
    # The guard here is that no interpolation hook exists to be passed.
    with pytest.raises(TypeError):
        uncertainty(6, land=WATER, tier="rayleigh",
                    interpolation_error=np.full(SHAPE, 0.005))


def test_the_note_says_what_the_number_is_and_is_not():
    # The wording is part of the deliverable: "uncertainty" alone invites a
    # reader to treat it as a propagated budget.
    note = uncertainty_note("full")
    assert "not a propagated" in note
    assert "ESA Level-2" in note
    assert "OL_2_WFR" in note


def test_surface_classes_use_the_land_flag_not_brightness():
    # Q5 established that bright-against-dark is the right axis for *reading*
    # the error, but land-against-water is the axis it was *measured* along,
    # because that is how the two reference products divide. Indexing by
    # brightness would look up a figure against something it was not measured
    # against.
    land = np.array([[True, False]])
    classes = surface_classes(land)
    assert classes["land"].tolist() == [[True, False]]
    assert classes["water"].tolist() == [[False, True]]


def test_the_sensitivity_is_measured_through_the_real_forward_model():
    # Not an analytic derivative: the path reflectance is piecewise linear in
    # depth with a kink at every table node, so a centred difference over the
    # uncertainty's own width is the slope that matters.
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
    # Negative - more haze means the same measurement implies a darker surface -
    # and around a tenth of a reflectance unit per unit depth, which is the
    # number that makes propagation alone inadequate.
    assert (slope < 0).all()
    assert np.allclose(slope, -0.10, atol=0.03)
