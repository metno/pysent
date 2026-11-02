"""The atmospheric-correction frame and the gaseous-absorption tables.

The reference values below come from 6SV itself, at geometries and columns
that are **not** on the generated tables' grid, so these tests check the
interpolation and the unit conversions against the radiative-transfer code
rather than against the tables they were built from. Reproduce them with:

    docker run --rm pysent-rt6s:1.1 python3 -c ...   # see tools/rt6s/check.py
"""
from __future__ import annotations

import numpy as np
import pytest

from pysent._atmosphere import (
    KG_M2_PER_DOBSON,
    air_mass,
    gas_transmittance,
    ozone_transmittance,
    platform_of,
    surface_from_toa,
    toa_from_surface,
    water_path_amount_limit,
    water_transmittance,
)

# band, solar zenith, view zenith, water g cm-2, ozone DU, 6S ozone T, 6S water T
SIXS_REFERENCE = [
    (7, 65.0, 15.0, 0.0, 350.0, 0.882360, 1.000000),
    (8, 65.0, 15.0, 0.0, 350.0, 0.943310, 1.000000),
    (20, 55.0, 25.0, 1.3, 300.0, 1.000000, 0.296060),
    (17, 55.0, 25.0, 1.3, 300.0, 1.000000, 0.999100),
    (6, 72.0, 30.0, 0.9, 311.0, 0.871460, 1.000000),
]


def test_the_air_mass_is_the_sum_of_both_legs():
    assert air_mass(0.0, 0.0) == pytest.approx(2.0)
    assert air_mass(60.0, 0.0) == pytest.approx(3.0)
    assert air_mass(65.0, 15.0) == pytest.approx(3.4015, abs=1e-4)


def test_an_angle_at_the_horizon_has_no_air_mass():
    # A path that never reaches the surface must not come back as a huge
    # number: that would give a transmittance of zero and a reflectance of
    # infinity rather than a pixel that says it is unusable.
    assert np.isnan(air_mass(90.0, 10.0))
    assert np.isnan(air_mass(10.0, 90.0))
    assert np.isnan(air_mass(95.0, 10.0))


@pytest.mark.parametrize("band,sza,vza,water,ozone_du,expected_ozone,expected_water", SIXS_REFERENCE)
def test_the_gas_tables_reproduce_6s_off_grid(
    band, sza, vza, water, ozone_du, expected_ozone, expected_water
):
    mass = air_mass(sza, vza)
    ozone_kg = ozone_du * KG_M2_PER_DOBSON
    water_kg = water / 0.1  # g cm-2 back to the kg m-2 the product reports

    assert float(ozone_transmittance("S3A", band, ozone_kg, mass)) == pytest.approx(
        expected_ozone, abs=2e-4
    )
    assert float(water_transmittance("S3A", band, water_kg, mass)) == pytest.approx(
        expected_water, abs=2e-3
    )


def test_ozone_absorption_peaks_in_the_chappuis_band():
    # 620 nm sits on the Chappuis peak and 400 nm is below it entirely. A
    # table that had the rows the wrong way round - the trap that made every
    # true-colour picture too blue - would fail here.
    mass = 2.0
    column = 300 * KG_M2_PER_DOBSON
    at_400 = float(ozone_transmittance("S3A", 1, column, mass))
    at_620 = float(ozone_transmittance("S3A", 7, column, mass))
    at_865 = float(ozone_transmittance("S3A", 17, column, mass))
    assert at_400 == pytest.approx(1.0, abs=1e-9)
    assert at_865 == pytest.approx(1.0, abs=1e-3)
    assert at_620 < 0.95


def test_ozone_is_beer_lambert_in_the_column():
    # Doubling the column squares the transmittance. This is what makes one
    # coefficient per band enough, so it is worth asserting rather than
    # trusting the table's own recorded spread.
    single = float(ozone_transmittance("S3A", 7, 300 * KG_M2_PER_DOBSON, 3.0))
    double = float(ozone_transmittance("S3A", 7, 600 * KG_M2_PER_DOBSON, 3.0))
    assert double == pytest.approx(single**2, rel=1e-9)


def test_water_vapour_saturates_rather_than_halving():
    # The reason water needs a curve and not a coefficient: doubling the path
    # amount removes much less than it did the first time.
    one = float(water_transmittance("S3A", 20, 10.0, 2.0))
    two = float(water_transmittance("S3A", 20, 20.0, 2.0))
    assert two > one**2 * 1.5


def test_a_water_path_beyond_the_curve_is_held_not_extrapolated():
    limit = water_path_amount_limit("S3A", 20)
    assert limit > 0
    # Far past the end, where a linear extrapolation would go negative.
    held = float(water_transmittance("S3A", 20, 10_000.0, 9.0))
    at_limit = float(water_transmittance("S3A", 20, limit / 0.1 / 2.0, 2.0))
    assert held == pytest.approx(at_limit, abs=1e-9)
    assert held > 0.0


def test_gas_transmittance_multiplies_the_two_absorbers():
    kwargs = dict(
        ozone_kg_m2=311 * KG_M2_PER_DOBSON,
        water_kg_m2=9.0,
        solar_zenith_deg=72.0,
        view_zenith_deg=30.0,
    )
    both = float(gas_transmittance("S3A", 6, **kwargs))
    mass = air_mass(72.0, 30.0)
    separately = float(
        ozone_transmittance("S3A", 6, kwargs["ozone_kg_m2"], mass)
    ) * float(water_transmittance("S3A", 6, kwargs["water_kg_m2"], mass))
    assert both == pytest.approx(separately, rel=1e-12)


def test_the_tables_cover_both_platforms_and_differ():
    # The band filters are not the same on S3A and S3B, so the coefficients
    # should not be identical - if they were, one platform's table would be a
    # copy and the per-platform lookup would be pointless.
    column, mass = 300 * KG_M2_PER_DOBSON, 3.0
    a = float(ozone_transmittance("S3A", 7, column, mass))
    b = float(ozone_transmittance("S3B", 7, column, mass))
    assert a == pytest.approx(b, abs=5e-3)
    assert a != b


def test_an_unknown_band_says_what_the_tables_cover():
    with pytest.raises(ValueError, match="no gas table row"):
        ozone_transmittance("S3A", 99, 0.006, 2.0)


def test_the_platform_comes_from_the_product_name():
    assert platform_of("S3A_OL_1_EFR____20261025T111740.SEN3") == "S3A"
    assert platform_of("/mnt/data/S3B_OL_1_EFR____20261025T111740.zip") == "S3B"
    with pytest.raises(ValueError, match="which platform"):
        platform_of("S2A_MSIL1C_20261025T111740.SAFE")


def test_no_atmosphere_returns_the_measurement_unchanged():
    # The phases fill the terms in one at a time, so an unfilled term must not
    # alter a pixel: the defaults have to be an exact identity, not nearly one.
    toa = np.array([0.0, 0.05, 0.3, 1.4])
    assert surface_from_toa(toa) == pytest.approx(toa, abs=0.0)


def test_the_inversion_undoes_the_forward_model():
    terms = dict(
        gas_transmittance=0.87,
        path_reflectance=0.096,
        transmittance_down=0.815,
        transmittance_up=0.923,
        spherical_albedo=0.124,
    )
    surface = np.array([0.01, 0.05, 0.2, 0.6])
    measured = toa_from_surface(surface, **terms)
    assert surface_from_toa(measured, **terms) == pytest.approx(surface, rel=1e-12)


def test_the_transmittance_division_is_the_term_a_subtraction_drops():
    # The point of the module docstring, as a number: at the two-way
    # transmittance 6S gives for 490 nm at solar zenith 70, dividing lifts the
    # answer by a third. A correction that only subtracts is not nearly right.
    toa, path = 0.1339, 0.0961
    subtraction_only = toa - path
    with_transmittance = float(
        surface_from_toa(
            toa, path_reflectance=path, transmittance_down=0.8145, transmittance_up=0.9229
        )
    )
    assert with_transmittance / subtraction_only == pytest.approx(1.33, abs=0.01)


def test_the_spherical_albedo_only_matters_for_a_bright_surface():
    # The coupling term is why the inversion is not a straight division: it is
    # negligible over dark water and several per cent over bright land, which
    # is the claim worth pinning.
    terms = dict(path_reflectance=0.05, transmittance_down=0.9, transmittance_up=0.95)

    def effect(measured):
        without = float(surface_from_toa(measured, **terms))
        with_coupling = float(surface_from_toa(measured, spherical_albedo=0.124, **terms))
        return abs(with_coupling - without)

    assert effect(0.06) < 0.002
    assert effect(0.5) > 0.01


def test_a_negative_result_is_kept_rather_than_masked():
    # Where the atmosphere is brighter than the measurement the surface comes
    # out below zero. That is a retrieval outcome, not a failure - ESA's own
    # water-leaving reflectance is negative on 28 % of its valid pixels in the
    # scene this plan validates against - and masking it would hide the error
    # distribution and bias every statistic towards agreement.
    negative = float(surface_from_toa(0.02, path_reflectance=0.09, transmittance_down=0.8))
    assert negative < 0
    assert np.isfinite(negative)


def test_an_undefined_inversion_is_nan():
    # Only genuinely undefined cases: no transmitted light at all, or an input
    # that was already missing.
    assert np.isnan(surface_from_toa(0.2, gas_transmittance=0.0))
    assert np.isnan(surface_from_toa(0.2, transmittance_down=0.0))
    assert np.isnan(surface_from_toa(0.2, transmittance_up=0.0))
    assert np.isnan(surface_from_toa(np.nan, path_reflectance=0.01))
