"""The four correction tiers, where each pixel's aerosol comes from, and the quality band.

These run without a product: the frame-level reads are the one part that needs
a real granule, and they are exercised by the real-frame test in
``test_s3_surface_reflectance.py``. What is checked here is the logic that
decides things - tier validation, the loading's provenance, the quality bits -
because that is where a wrong answer would be silent rather than loud.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysent._aerosol_retrieval import CLIMATOLOGICAL_ANGSTROM, CLIMATOLOGICAL_DEPTH_550
from pysent._olci_correction import (
    CORRECTING_TIERS,
    CORRECTION_TIERS,
    QUALITY_BITS,
    VALIDATED_SOLAR_ZENITH_DEG,
    quality_bit_names,
    resolve_loading,
)

SHAPE = (3, 4)


def geometry(**overrides):
    base = dict(
        pressure_hpa=np.full(SHAPE, 1013.0),
        solar_zenith_deg=np.full(SHAPE, 70.0),
        view_zenith_deg=np.full(SHAPE, 20.0),
        relative_azimuth_deg=np.full(SHAPE, 90.0),
        ozone_kg_m2=np.full(SHAPE, 0.006),
        water_kg_m2=np.full(SHAPE, 5.0),
    )
    base.update(overrides)
    return base


def black_water_toa(depth=0.12, angstrom=0.8):
    """Top-of-atmosphere reflectance over a black surface at a known loading."""
    from pysent._aerosol_retrieval import RETRIEVAL_BANDS
    from pysent._atmosphere import atmosphere_terms

    geo = geometry()
    toa = {}
    for band in RETRIEVAL_BANDS:
        terms = atmosphere_terms(
            "S3A", band,
            optical_depth_550=np.full(SHAPE, depth),
            angstrom=np.full(SHAPE, angstrom),
            **geo,
        )
        toa[band] = terms["path_reflectance"] * terms["gas_transmittance"]
    return toa, geo


def test_the_four_tiers_are_named_and_none_is_not_a_correction():
    assert CORRECTION_TIERS == ("none", "gas", "rayleigh", "full")
    # "none" is askable so a caller can request the frozen behaviour by name,
    # but it must not be counted among the tiers that divide an atmosphere out.
    assert "none" not in CORRECTING_TIERS
    assert set(CORRECTING_TIERS) == {"gas", "rayleigh", "full"}


def test_every_quality_bit_has_a_distinct_place_and_a_name():
    bits = sorted(QUALITY_BITS.values())
    assert bits == list(range(len(bits))), "bits must be contiguous and unique"
    # They go in a uint16 band, so there is room for six more before the type
    # has to change - which is worth knowing before adding a seventh.
    assert max(bits) < 16
    assert quality_bit_names()[QUALITY_BITS["bright_surface"]] == "bright_surface"


def test_a_scalar_loading_is_a_product_tier_not_a_retrieval():
    # A caller who passes a number has supplied a measurement from somewhere,
    # so those pixels index the "product" validation row - not the retrieved
    # row, which is better, and not the climatological one, which is worse.
    toa, geo = black_water_toa()
    out = resolve_loading(
        "S3A", aerosol=0.2, atmosphere=geo, toa=toa,
        water=np.ones(SHAPE, bool), shape=SHAPE,
    )
    assert np.allclose(out["optical_depth_550"], 0.2)
    assert np.allclose(out["angstrom"], CLIMATOLOGICAL_ANGSTROM)
    assert (out["source"] == out["source_codes"]["product"]).all()


def test_a_depth_and_exponent_pair_is_taken_as_given():
    toa, geo = black_water_toa()
    out = resolve_loading(
        "S3A", aerosol=(0.25, 1.3), atmosphere=geo, toa=toa,
        water=np.ones(SHAPE, bool), shape=SHAPE,
    )
    assert np.allclose(out["optical_depth_550"], 0.25)
    assert np.allclose(out["angstrom"], 1.3)


def test_asking_for_the_climatology_says_so_in_the_source():
    # The fallback is a stated assumption, and the source array is what keeps it
    # from being mistaken for a measurement downstream - the uncertainty band
    # refuses to publish a figure for exactly these pixels.
    toa, geo = black_water_toa()
    out = resolve_loading(
        "S3A", aerosol="climatological", atmosphere=geo, toa=toa,
        water=np.ones(SHAPE, bool), shape=SHAPE,
    )
    assert np.allclose(out["optical_depth_550"], CLIMATOLOGICAL_DEPTH_550)
    assert (out["source"] == out["source_codes"]["climatological"]).all()


def test_retrieving_recovers_the_loading_and_marks_it_retrieved():
    toa, geo = black_water_toa(depth=0.12, angstrom=0.8)
    out = resolve_loading(
        "S3A", aerosol="retrieve", atmosphere=geo, toa=toa,
        water=np.ones(SHAPE, bool), shape=SHAPE,
    )
    assert np.allclose(out["optical_depth_550"], 0.12)
    assert (out["source"] == out["source_codes"]["retrieved"]).all()
    assert out["refused"], "a retrieval must report its refusal reasons"


def test_a_refused_pixel_falls_to_the_assumption_and_records_the_drop():
    # The retrieval covers about two thirds of clear water and nothing else, so
    # the interesting case is the pixel it declines: it must come back filled
    # *and* flagged, never filled silently.
    toa, geo = black_water_toa()
    water = np.ones(SHAPE, bool)
    water[0, 0] = False
    out = resolve_loading(
        "S3A", aerosol="retrieve", atmosphere=geo, toa=toa,
        water=water, shape=SHAPE,
    )
    assert out["optical_depth_550"][0, 0] == pytest.approx(CLIMATOLOGICAL_DEPTH_550)
    assert out["source"][0, 0] == out["source_codes"]["climatological"]
    assert out["source"][1, 1] == out["source_codes"]["retrieved"]


def test_a_loading_past_the_table_is_flagged_as_well_as_clamped():
    # Clamping at the top is right - the value beyond the edge is unknown - but
    # a clamped pixel that says nothing turns a smoke plume into ordinary haze.
    # This is what aerosol_grid_limits was added to the atmosphere module for.
    toa, geo = black_water_toa()
    out = resolve_loading(
        "S3A", aerosol=1.5, atmosphere=geo, toa=toa,
        water=np.ones(SHAPE, bool), shape=SHAPE,
    )
    assert out["table_ceiling"] < 1.5
    assert out["haze_beyond_table"].all()


def test_a_loading_inside_the_table_is_not_flagged():
    toa, geo = black_water_toa()
    out = resolve_loading(
        "S3A", aerosol=0.1, atmosphere=geo, toa=toa,
        water=np.ones(SHAPE, bool), shape=SHAPE,
    )
    assert not out["haze_beyond_table"].any()


def test_an_unnamed_aerosol_source_is_refused_with_the_options_listed():
    toa, geo = black_water_toa()
    with pytest.raises(ValueError, match="aerosol must be a number"):
        resolve_loading(
            "S3A", aerosol="whatever", atmosphere=geo, toa=toa,
            water=np.ones(SHAPE, bool), shape=SHAPE,
        )


def test_the_validated_geometry_range_is_the_one_the_sweep_measured():
    # A9 killed the solar-zenith ceiling: accuracy does not degrade towards the
    # horizon inside the archive's range. But "no evidence of degradation inside
    # 63-80" is not "accurate at 85", so the range is published and a pixel
    # outside it is flagged rather than refused or passed off as validated.
    assert VALIDATED_SOLAR_ZENITH_DEG == (63.0, 80.0)
    assert "outside_validated_geometry" in QUALITY_BITS
