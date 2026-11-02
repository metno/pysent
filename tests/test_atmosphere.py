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
    PRESSURE_EXTRAPOLATION_HPA,
    STANDARD_PRESSURE_HPA,
    aerosol_grid_limits,
    aerosol_models,
    aerosol_terms,
    air_mass,
    angstrom_exponent,
    atmosphere_terms,
    gas_transmittance,
    model_angstrom,
    ozone_transmittance,
    pixel_pressure,
    platform_of,
    rayleigh_grid_limits,
    rayleigh_terms,
    relative_azimuth,
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


# band, pressure hPa, solar zenith, view zenith, relative azimuth,
# then 6S's path reflectance, downward and upward transmittance, spherical albedo.
# Every one of these is deliberately *off* the generated grid in all four
# coordinates, so what is being tested is the interpolation and the Fourier
# reconstruction, not the table's own rows.
SIXS_RAYLEIGH_OFF_GRID = [
    (4, 926.34, 71.5, 28.0, 37.5, 0.12331, 0.81779, 0.92525, 0.11427),
    (6, 983.58, 67.5, 20.0, 112.5, 0.04847, 0.89682, 0.95514, 0.07526),
    (8, 746.83, 57.5, 44.0, 7.5, 0.03101, 0.96992, 0.97736, 0.03105),
    (4, 871.82, 78.5, 52.0, 165.0, 0.23402, 0.75264, 0.90173, 0.10863),
]


@pytest.mark.parametrize(
    "band,pressure,sza,vza,azimuth,path,down,up,albedo", SIXS_RAYLEIGH_OFF_GRID
)
def test_the_rayleigh_table_reproduces_6s_off_grid(
    band, pressure, sza, vza, azimuth, path, down, up, albedo
):
    terms = rayleigh_terms(
        "S3A",
        band,
        pressure_hpa=pressure,
        solar_zenith_deg=sza,
        view_zenith_deg=vza,
        relative_azimuth_deg=azimuth,
    )
    # 1.5e-3 is the interpolation's own error between grid nodes, not slack:
    # the Fourier reconstruction is good to 7e-6, so what is left is the
    # multilinear step, and interpolating in secant space rather than in
    # degrees is what brought the worst corner from 0.0041 to 0.0011.
    assert float(terms["path_reflectance"]) == pytest.approx(path, abs=1.5e-3)
    assert float(terms["transmittance_down"]) == pytest.approx(down, abs=2e-3)
    assert float(terms["transmittance_up"]) == pytest.approx(up, abs=2e-3)
    assert float(terms["spherical_albedo"]) == pytest.approx(albedo, abs=2e-3)


def test_rayleigh_scattering_is_strongest_at_blue():
    # The whole reason the correction exists. Same geometry, three bands.
    def path(band):
        return float(
            rayleigh_terms(
                "S3A", band, pressure_hpa=1013.25, solar_zenith_deg=70.0,
                view_zenith_deg=20.0, relative_azimuth_deg=90.0,
            )["path_reflectance"]
        )

    assert path(4) > path(6) > path(8)
    # 490 nm scatters about three times as much as 665 nm.
    assert path(4) / path(8) == pytest.approx(3.0, abs=0.6)


def test_the_path_reflectance_grows_towards_the_horizon():
    def path(solar_zenith):
        return float(
            rayleigh_terms(
                "S3A", 4, pressure_hpa=1013.25, solar_zenith_deg=solar_zenith,
                view_zenith_deg=20.0, relative_azimuth_deg=90.0,
            )["path_reflectance"]
        )

    assert path(20.0) < path(50.0) < path(70.0) < path(85.0)
    # And the transmittance falls the other way.
    low = rayleigh_terms(
        "S3A", 4, pressure_hpa=1013.25, solar_zenith_deg=20.0,
        view_zenith_deg=20.0, relative_azimuth_deg=90.0,
    )
    high = rayleigh_terms(
        "S3A", 4, pressure_hpa=1013.25, solar_zenith_deg=85.0,
        view_zenith_deg=20.0, relative_azimuth_deg=90.0,
    )
    assert float(high["transmittance_down"]) < float(low["transmittance_down"])


def test_a_thinner_atmosphere_scatters_less():
    # Rayleigh optical depth is proportional to pressure, so ground at
    # altitude must come out darker. Treating a mountain as sea level is the
    # mistake this guards.
    def path(pressure):
        return float(
            rayleigh_terms(
                "S3A", 4, pressure_hpa=pressure, solar_zenith_deg=70.0,
                view_zenith_deg=20.0, relative_azimuth_deg=90.0,
            )["path_reflectance"]
        )

    assert path(700.0) < path(850.0) < path(1013.25) < path(1040.0)
    # About a third less at 700 hPa, which is roughly 3 km of elevation.
    assert path(700.0) / path(1013.25) == pytest.approx(0.70, abs=0.08)


def test_the_azimuth_dependence_is_not_flat():
    def path(azimuth):
        return float(
            rayleigh_terms(
                "S3A", 4, pressure_hpa=1013.25, solar_zenith_deg=70.0,
                view_zenith_deg=40.0, relative_azimuth_deg=azimuth,
            )["path_reflectance"]
        )

    values = [path(a) for a in (0.0, 45.0, 90.0, 135.0, 180.0)]
    # The scattering angle sweeps through the Rayleigh phase function, so the
    # spread across azimuth is a real effect and a table that ignored it would
    # be wrong by this much.
    assert max(values) - min(values) > 0.02


def test_the_azimuth_is_folded_and_unsigned():
    assert relative_azimuth(10.0, 350.0) == pytest.approx(20.0)
    assert relative_azimuth(350.0, 10.0) == pytest.approx(20.0)
    assert relative_azimuth(0.0, 180.0) == pytest.approx(180.0)
    assert relative_azimuth(0.0, 270.0) == pytest.approx(90.0)
    # And the terms must not care about the sign either.
    def path(solar, view):
        return float(
            rayleigh_terms(
                "S3A", 4, pressure_hpa=1013.25, solar_zenith_deg=70.0, view_zenith_deg=20.0,
                relative_azimuth_deg=relative_azimuth(solar, view),
            )["path_reflectance"]
        )

    assert path(10.0, 350.0) == pytest.approx(path(350.0, 10.0), rel=1e-12)


def test_pressure_comes_from_sea_level_pressure_and_elevation():
    assert pixel_pressure(STANDARD_PRESSURE_HPA, 0.0) == pytest.approx(STANDARD_PRESSURE_HPA)
    # The standard atmosphere at 1.5 km is about 845 hPa, which is also the
    # ratio 6S's own optical depth showed between sea level and 1.5 km.
    assert pixel_pressure(STANDARD_PRESSURE_HPA, 1500.0) == pytest.approx(845.6, abs=1.0)
    assert pixel_pressure(STANDARD_PRESSURE_HPA, 3000.0) == pytest.approx(701.1, abs=1.5)
    # A high-pressure day at sea level is above standard, and elevation still
    # scales it.
    assert pixel_pressure(1035.0, 0.0) == pytest.approx(1035.0)
    assert pixel_pressure(1035.0, 1000.0) < 1035.0


def test_pressure_above_the_table_is_extrapolated_not_clamped():
    # The table stops at sea-level standard because 6SV cannot be asked for a
    # negative target altitude, but a winter anticyclone reaches 1040 hPa.
    # Clamping there would cost about a quarter of this phase's error budget,
    # and the dependence is linear enough to extrapolate safely.
    def path(pressure):
        return float(
            rayleigh_terms(
                "S3A", 4, pressure_hpa=pressure, solar_zenith_deg=70.0,
                view_zenith_deg=20.0, relative_azimuth_deg=90.0,
            )["path_reflectance"]
        )

    top = rayleigh_grid_limits("S3A", 4)["pressure_hpa"][1]
    assert top == pytest.approx(STANDARD_PRESSURE_HPA, abs=0.01)
    assert path(1040.0) > path(top)
    # Linear in pressure: the step up should match the step down in size.
    step = 1040.0 - top
    assert path(1040.0) - path(top) == pytest.approx(path(top) - path(top - step), rel=0.05)
    # But not without limit - far above, it is held.
    assert path(top + 500.0) == pytest.approx(path(top + PRESSURE_EXTRAPOLATION_HPA), rel=1e-12)


def test_an_angle_outside_the_grid_is_clamped_not_extrapolated():
    limits = rayleigh_grid_limits("S3A", 4)
    top = limits["solar_zenith"][1]

    def path(solar_zenith):
        return float(
            rayleigh_terms(
                "S3A", 4, pressure_hpa=1013.25, solar_zenith_deg=solar_zenith,
                view_zenith_deg=20.0, relative_azimuth_deg=90.0,
            )["path_reflectance"]
        )

    # Past the last node the curve is steep, and extrapolating a steep curve
    # invents brightness; holding the last value is wrong in a way a caller
    # can see and bound.
    assert path(top + 4.0) == pytest.approx(path(top), rel=1e-12)
    assert path(top + 40.0) == pytest.approx(path(top), rel=1e-12)


def test_the_terms_interpolate_elementwise_over_an_array():
    # The pipeline hands whole tie-point fields in, so the lookup has to be
    # vectorised rather than called per pixel.
    solar = np.array([[60.0, 70.0], [75.0, 80.0]])
    terms = rayleigh_terms(
        "S3A", 4, pressure_hpa=np.full((2, 2), 1000.0), solar_zenith_deg=solar,
        view_zenith_deg=np.full((2, 2), 20.0), relative_azimuth_deg=np.full((2, 2), 90.0),
    )
    path = terms["path_reflectance"]
    assert path.shape == (2, 2)
    assert path[0, 0] < path[0, 1] < path[1, 0] < path[1, 1]


def test_the_whole_chain_lands_on_a_plausible_surface_reflectance():
    # Gas and Rayleigh together, on the geometry of the land frame the plan
    # validates against, with that frame's measured top-of-atmosphere value at
    # 490 nm. The answer has to be near Synergy's 0.038, not near zero and not
    # negative - this is Phase 2's acceptance in miniature.
    terms = rayleigh_terms(
        "S3A", 4, pressure_hpa=1034.8, solar_zenith_deg=70.9,
        view_zenith_deg=20.0, relative_azimuth_deg=90.0,
    )
    gas = gas_transmittance(
        "S3A", 4, ozone_kg_m2=0.00611, water_kg_m2=7.53,
        solar_zenith_deg=70.9, view_zenith_deg=20.0,
    )
    surface = float(
        surface_from_toa(
            0.1339,
            gas_transmittance=gas,
            path_reflectance=terms["path_reflectance"],
            transmittance_down=terms["transmittance_down"],
            transmittance_up=terms["transmittance_up"],
            spherical_albedo=terms["spherical_albedo"],
        )
    )
    assert 0.0 < surface < 0.12


# band, 6SV model, optical depth at 550, solar zenith, view zenith, relative
# azimuth, then 6S's total path reflectance, downward and upward transmittance
# and spherical albedo. Off the stored grid in **every** coordinate: the depths
# lie between 0.05, 0.15 and 0.4, the zeniths between the table's nodes, and the
# azimuths on the midpoints of the ten-degree axis, where linear interpolation
# is at its worst. Reproduce with ``tools/rt6s/offgrid.py --what reference``.
SIXS_AEROSOL_OFF_GRID = [
    (4, "Continental", 0.08, 62.0, 28.0, 35.0, 0.11725, 0.81345, 0.89808, 0.13761),
    (6, "Maritime", 0.22, 71.5, 44.0, 95.0, 0.11953, 0.77401, 0.90640, 0.12134),
    (8, "Continental", 0.12, 45.0, 12.0, 155.0, 0.02297, 0.93484, 0.95601, 0.06660),
    (17, "Maritime", 0.09, 77.5, 52.0, 25.0, 0.06916, 0.88276, 0.97039, 0.03728),
]


@pytest.mark.parametrize(
    "band,name,depth,sza,vza,azimuth,path,down,up,albedo", SIXS_AEROSOL_OFF_GRID
)
def test_the_aerosol_table_reproduces_6s_off_grid(
    band, name, depth, sza, vza, azimuth, path, down, up, albedo
):
    # Asking for exactly one model's own exponent clamps the blend onto that
    # model, so what is measured here is the table and the interpolation rather
    # than the two-model blend.
    terms = aerosol_terms(
        "S3A", band,
        optical_depth_550=depth, angstrom=model_angstrom("S3A", name),
        solar_zenith_deg=sza, view_zenith_deg=vza, relative_azimuth_deg=azimuth,
    )
    molecular = rayleigh_terms(
        "S3A", band, pressure_hpa=STANDARD_PRESSURE_HPA,
        solar_zenith_deg=sza, view_zenith_deg=vza, relative_azimuth_deg=azimuth,
    )
    # 2e-3 is the measured cost of interpolating this grid, not slack: the
    # worst of these four is band 17 at solar zenith 77.5, which is the
    # sharpest phase function at the steepest geometry the table carries.
    assert float(molecular["path_reflectance"] + terms["path_increment"]) == pytest.approx(
        path, abs=2e-3
    )
    assert float(
        molecular["transmittance_down"] * terms["transmittance_down_ratio"]
    ) == pytest.approx(down, abs=4e-3)
    assert float(
        molecular["transmittance_up"] * terms["transmittance_up_ratio"]
    ) == pytest.approx(up, abs=2e-3)
    assert float(
        molecular["spherical_albedo"] + terms["spherical_albedo_increment"]
    ) == pytest.approx(albedo, abs=2e-3)


def test_three_fourier_terms_cannot_hold_the_azimuth_curve():
    # Why the aerosol table stores samples where the Rayleigh table stores a
    # fit. This reconstructs the stored curve, fits the Rayleigh table's own
    # three-term form to it, and shows the fit missing by more than the whole
    # Phase 2 error budget. Should anyone compress this axis again to save
    # rows, this is the test that says what it costs.
    azimuths = np.arange(0.0, 181.0, 10.0)
    curve = np.array(
        [
            float(
                aerosol_terms(
                    "S3A", 17,
                    optical_depth_550=0.4, angstrom=model_angstrom("S3A", "Maritime"),
                    solar_zenith_deg=85.0, view_zenith_deg=60.0,
                    relative_azimuth_deg=azimuth,
                )["path_increment"]
            )
            for azimuth in azimuths
        ]
    )
    # rho = a0 + 2 a1 cos d + 2 a2 cos 2d, least squares over the samples.
    radians = np.radians(azimuths)
    design = np.stack(
        [np.ones_like(radians), 2.0 * np.cos(radians), 2.0 * np.cos(2.0 * radians)], axis=-1
    )
    coefficients, *_ = np.linalg.lstsq(design, curve, rcond=None)
    fitted = design @ coefficients
    assert np.abs(fitted - curve).max() > 1e-2
    # And the curve really does have that much shape in it to lose.
    assert curve.max() - curve.min() > 0.5


def test_the_angstrom_exponent_comes_from_the_measured_depths():
    # The aerosol product's own Angstrom field is empty in every granule
    # checked, so the exponent is derived from the optical depths at 440 and
    # 670 nm. These two are the land frame's medians.
    assert angstrom_exponent(0.0436, 0.0265) == pytest.approx(1.184, abs=1e-3)
    # A flat spectrum is exponent zero, whichever pair of wavelengths.
    assert angstrom_exponent(0.1, 0.1) == pytest.approx(0.0)
    # Coarse marine aerosol scatters almost greyly; fine smoke steeply.
    assert angstrom_exponent(0.11, 0.1) < 0.5
    assert angstrom_exponent(0.2, 0.1) > 1.5


def test_the_model_blend_follows_the_measured_exponent():
    models = aerosol_models("S3A")
    assert set(models) == {"Continental", "Maritime"}
    exponents = {name: model_angstrom("S3A", name) for name in models}
    # Read off the table's own band optical depths rather than asserted: the
    # continental model is the fine-mode one, so its exponent is the larger.
    assert exponents["Continental"] > exponents["Maritime"]

    def increment(angstrom):
        return float(
            aerosol_terms(
                "S3A", 4, optical_depth_550=0.15, angstrom=angstrom,
                solar_zenith_deg=70.0, view_zenith_deg=20.0, relative_azimuth_deg=90.0,
            )["path_increment"]
        )

    maritime, continental = increment(exponents["Maritime"]), increment(exponents["Continental"])
    assert maritime != pytest.approx(continental, abs=1e-4)
    # Between the two exponents the answer is between the two models, and
    # outside them it is clamped rather than extrapolated into a model that
    # does not exist.
    middle = increment(0.5 * (exponents["Maritime"] + exponents["Continental"]))
    assert min(maritime, continental) < middle < max(maritime, continental)
    assert increment(exponents["Maritime"] - 1.0) == pytest.approx(maritime, rel=1e-12)
    assert increment(exponents["Continental"] + 1.0) == pytest.approx(continental, rel=1e-12)


def test_an_unknown_exponent_takes_the_midpoint_rather_than_an_extreme():
    # The exponent needs two optical depths, and the aerosol product is sparse,
    # so some pixels will have none. Picking either model there would be a
    # silent choice; the midpoint is wrong by a bounded amount instead.
    def increment(angstrom):
        return float(
            aerosol_terms(
                "S3A", 4, optical_depth_550=0.15, angstrom=angstrom,
                solar_zenith_deg=70.0, view_zenith_deg=20.0, relative_azimuth_deg=90.0,
            )["path_increment"]
        )

    exponents = [model_angstrom("S3A", name) for name in aerosol_models("S3A")]
    assert increment(np.nan) == pytest.approx(increment(0.5 * (min(exponents) + max(exponents))))


def test_aerosol_brightens_the_path_and_dims_the_transmission():
    terms = aerosol_terms(
        "S3A", 4, optical_depth_550=0.15, angstrom=1.1,
        solar_zenith_deg=70.0, view_zenith_deg=20.0, relative_azimuth_deg=90.0,
    )
    assert float(terms["path_increment"]) > 0.0
    assert float(terms["transmittance_down_ratio"]) < 1.0
    assert float(terms["transmittance_up_ratio"]) < 1.0
    assert float(terms["spherical_albedo_increment"]) > 0.0


GEOMETRY = dict(solar_zenith_deg=70.9, view_zenith_deg=20.0, relative_azimuth_deg=58.1)


def _increment(depth, band=4, angstrom=1.18):
    return aerosol_terms(
        "S3A", band, optical_depth_550=depth, angstrom=angstrom, **GEOMETRY
    )


def test_no_aerosol_means_no_aerosol_correction():
    # The table's lightest optical depth is 0.05, and clamping to it handed a
    # clear-air pixel the whole correction for the lightest haze it carries.
    # The value at zero is not unknown and must not be guessed: an atmosphere
    # with no aerosol in it adds no path reflectance, dims nothing and adds
    # nothing to the spherical albedo.
    terms = _increment(0.0)
    assert float(terms["path_increment"]) == 0.0
    assert float(terms["transmittance_down_ratio"]) == 1.0
    assert float(terms["transmittance_up_ratio"]) == 1.0
    assert float(terms["spherical_albedo_increment"]) == 0.0


def test_a_light_loading_is_not_rounded_up_to_the_table_floor():
    # This archive's measured loading is 0.02 to 0.10, so most of it sits at or
    # below the table's lightest row: rounding up over-corrected the majority
    # of the holding by up to half again.
    floor = aerosol_grid_limits("S3A", 4)["optical_depth_550"][0]
    full = float(_increment(floor)["path_increment"])
    assert float(_increment(0.5 * floor)["path_increment"]) == pytest.approx(0.5 * full)
    light = float(_increment(0.033)["path_increment"])
    assert light < full
    assert light == pytest.approx(full * 0.033 / floor)


def test_an_optical_depth_above_the_table_is_clamped_not_extrapolated():
    # The aerosol grid shares its interpolation with the Rayleigh grid, whose
    # first axis is pressure and is allowed to extrapolate 60 units past its
    # last node. Inherited by an axis measured in optical depth, that allowance
    # is unbounded in practice, and past about 1.5 it drives the transmittance
    # ratio **negative** - which flips the sign of the retrieved surface
    # reflectance and reports it with no indication of trouble. A smoke plume
    # is a real way to reach 1.5.
    top = aerosol_grid_limits("S3A", 4)["optical_depth_550"][1]
    edge = _increment(top)
    for beyond in (1.5, 5.0, 20.0):
        terms = _increment(beyond)
        assert float(terms["path_increment"]) == pytest.approx(
            float(edge["path_increment"])
        )
        assert float(terms["transmittance_down_ratio"]) > 0.0
        assert float(terms["transmittance_up_ratio"]) > 0.0


def test_the_aerosol_table_publishes_its_own_limits():
    # So a caller - and the per-pixel quality band - can say "hazier than the
    # table goes" rather than silently receiving the nearest edge.
    limits = aerosol_grid_limits("S3A", 4)
    assert limits["optical_depth_550"] == (0.05, 0.4)
    assert limits["solar_zenith"][1] >= 85.0
    assert limits["view_zenith"][1] >= 60.0
    with pytest.raises(ValueError, match="no rows for"):
        aerosol_grid_limits("S3A", 11)


def test_the_pressure_axis_keeps_its_extrapolation_after_the_split():
    # The fix separates the two tables' interpolation rules. Rayleigh's
    # pressure axis must still open at the top, because surface pressure above
    # sea-level standard is ordinary and clamping it costs 0.0025.
    def path(pressure):
        return float(
            rayleigh_terms(
                "S3A", 4, pressure_hpa=pressure, solar_zenith_deg=70.9,
                view_zenith_deg=20.0, relative_azimuth_deg=58.1,
            )["path_reflectance"]
        )

    assert path(1040.0) > path(STANDARD_PRESSURE_HPA)


def test_a_thicker_aerosol_brightens_the_path_further():
    def increment(depth):
        return float(
            aerosol_terms(
                "S3A", 4, optical_depth_550=depth, angstrom=1.1,
                solar_zenith_deg=70.0, view_zenith_deg=20.0, relative_azimuth_deg=90.0,
            )["path_increment"]
        )

    assert increment(0.05) < increment(0.15) < increment(0.4)


def test_the_interpolation_error_is_reported_with_the_terms():
    # The table's own leave-one-out estimate of what the azimuth step costs at
    # this grid point, so a caller can bound a geometry instead of trusting a
    # single number quoted for the whole table. It is worst where the phase
    # function is sharpest.
    def error(band, depth, sza, vza):
        return float(
            aerosol_terms(
                "S3A", band, optical_depth_550=depth, angstrom=model_angstrom("S3A", "Maritime"),
                solar_zenith_deg=sza, view_zenith_deg=vza, relative_azimuth_deg=90.0,
            )["interpolation_error"]
        )

    assert error(17, 0.4, 85.0, 60.0) > error(17, 0.05, 40.0, 16.0)
    assert error(4, 0.05, 40.0, 16.0) < 1e-3


def test_an_aerosol_atmosphere_is_the_molecular_one_plus_the_increment():
    # The two tiers compose: without an optical depth this is Phase 2's
    # atmosphere exactly, and with one the path reflectances add, the
    # transmittances multiply and the spherical albedos add.
    geometry = dict(
        pressure_hpa=1004.0, solar_zenith_deg=71.0, view_zenith_deg=22.0,
        relative_azimuth_deg=48.0, ozone_kg_m2=0.00611, water_kg_m2=7.53,
    )
    clear = atmosphere_terms("S3A", 4, **geometry)
    hazy = atmosphere_terms("S3A", 4, optical_depth_550=0.05, angstrom=1.184, **geometry)
    assert float(hazy["gas_transmittance"]) == pytest.approx(float(clear["gas_transmittance"]))
    assert float(hazy["path_reflectance"]) > float(clear["path_reflectance"])
    assert float(hazy["transmittance_down"]) < float(clear["transmittance_down"])
    assert float(hazy["spherical_albedo"]) > float(clear["spherical_albedo"])

    increment = aerosol_terms(
        "S3A", 4, optical_depth_550=0.05, angstrom=1.184,
        solar_zenith_deg=71.0, view_zenith_deg=22.0, relative_azimuth_deg=48.0,
    )
    assert float(hazy["path_reflectance"]) == pytest.approx(
        float(clear["path_reflectance"] + increment["path_increment"])
    )


def test_correcting_for_aerosol_lowers_the_surface_reflectance():
    # The point of the tier. Same measurement, same geometry, with and without
    # the aerosol term: the aerosol adds brightness the molecular atmosphere
    # cannot account for, so leaving it out credits that brightness to the
    # ground.
    geometry = dict(
        pressure_hpa=1034.8, solar_zenith_deg=70.9, view_zenith_deg=20.0,
        relative_azimuth_deg=90.0, ozone_kg_m2=0.00611, water_kg_m2=7.53,
    )

    def surface(**extra):
        terms = atmosphere_terms("S3A", 4, **geometry, **extra)
        return float(surface_from_toa(0.1339, **terms))

    without = surface()
    with_aerosol = surface(optical_depth_550=0.033, angstrom=1.184)
    assert with_aerosol < without
    # The archive's loading is light, so this is a nudge rather than a
    # rewrite - about a hundredth of a reflectance unit at 490 nm.
    assert 0.002 < without - with_aerosol < 0.02


def test_the_terms_interpolate_elementwise_over_an_aerosol_field():
    # Aerosol optical depth arrives as a field of its own, coarser than the
    # radiance grid and with holes in it, so every axis has to be an array.
    depth = np.array([[0.05, 0.15], [0.25, 0.4]])
    terms = aerosol_terms(
        "S3A", 4, optical_depth_550=depth, angstrom=np.full((2, 2), 1.1),
        solar_zenith_deg=np.full((2, 2), 70.0), view_zenith_deg=np.full((2, 2), 20.0),
        relative_azimuth_deg=np.full((2, 2), 90.0),
    )
    increment = terms["path_increment"]
    assert increment.shape == (2, 2)
    assert increment[0, 0] < increment[0, 1] < increment[1, 0] < increment[1, 1]
