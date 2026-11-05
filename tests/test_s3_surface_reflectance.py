"""The ``surface_reflectance`` product and ``truecolor``'s correction option.

Two things are being protected here. The first is that asking for a correction
gets you one, with the uncertainty and quality bands the owner's Q5 answer makes
mandatory. The second is that **not** asking changes nothing at all: the frozen
Sentinel-3 true-colour style has already been invalidated twice this month, and
this phase is the one that could do it a third time by accident.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysent.s3 import (
    S3_OPT_IN_PRODUCTS,
    S3_PRODUCTS,
    S3_TYPE_PRODUCTS,
    default_sentinel_s3_products,
    normalize_sentinel_s3_product_map,
)


# --------------------------------------------------------------------------- #
# Nothing moves unless it is asked to
# --------------------------------------------------------------------------- #
def test_the_frozen_truecolor_style_is_untouched():
    # The numbers themselves, pinned. They were chosen on pictures that turned
    # out to be 2.1x too blue and are open as Q5 of the Sentinel-3 review plan;
    # until that study answers, the only safe thing is that they do not move.
    frozen = S3_PRODUCTS["truecolor"]
    assert frozen["value_range"] == (0.0, 0.6)
    assert frozen["gamma"] == 0.6
    assert frozen["bands"] == (8, 6, 4)
    assert frozen["atmospheric_correction"] == "none"


def test_truecolor_defaults_to_no_correction_and_keeps_its_stretch():
    options = normalize_sentinel_s3_product_map({"truecolor": {}})["truecolor"]
    assert options["atmospheric_correction"] == "none"
    assert options["value_range"] == (0.0, 0.6)
    assert options["gamma"] == 0.6
    assert options["aerosol"] is None


def test_an_ol_1_efr_request_still_yields_only_the_picture():
    # The completeness invariant lets surface_reflectance be reachable; this is
    # the other half - it must not become a default, or every existing Level-1
    # request silently starts paying for an aerosol retrieval.
    assert default_sentinel_s3_products("S3A_OL_1_EFR____20261105T095140.zip") == ("truecolor",)
    assert "surface_reflectance" not in S3_TYPE_PRODUCTS["OL_1_EFR"]
    assert "surface_reflectance" in S3_OPT_IN_PRODUCTS["OL_1_EFR"]


# --------------------------------------------------------------------------- #
# The option
# --------------------------------------------------------------------------- #
def test_a_corrected_picture_gets_the_measured_second_stretch():
    # Q6, answered "separate stretch per mode" on 2026-11-05. The corrected
    # composite is 44 % darker than the top-of-atmosphere one, so held at the
    # frozen ramp it would render 29 % darker - correct and apparently broken.
    options = normalize_sentinel_s3_product_map(
        {"truecolor": {"atmospheric_correction": "rayleigh"}}
    )["truecolor"]
    assert options["value_range"] == (0.0, 1.0)
    assert options["gamma"] == pytest.approx(0.33)


def test_the_corrected_stretch_keeps_clear_land_at_the_tone_it_had():
    # The criterion behind the numbers, so a future change has to beat it rather
    # than just prefer something else. Measured medians: clear-land composite
    # 0.157 at top of atmosphere, 0.088 corrected, over six S3A frames.
    def render(value, low, high, gamma):
        return float(np.clip((value - low) / (high - low), 0, 1) ** gamma)

    frozen = render(0.1571, 0.0, 0.6, 0.6)
    corrected = render(0.0879, 0.0, 1.0, 0.33)
    assert frozen == pytest.approx(0.448, abs=5e-3)
    assert corrected == pytest.approx(frozen, rel=0.02)
    # And held at the frozen stretch it would be visibly darker, which is the
    # thing Q6 was asked about.
    assert render(0.0879, 0.0, 0.6, 0.6) < frozen * 0.75


def test_an_explicit_stretch_still_wins_over_both():
    options = normalize_sentinel_s3_product_map(
        {"truecolor": {"atmospheric_correction": "full", "aerosol": "retrieve",
                       "value_range": (0.0, 0.3), "gamma": 1.0}}
    )["truecolor"]
    assert options["value_range"] == (0.0, 0.3)
    assert options["gamma"] == 1.0


def test_an_unknown_tier_is_refused_with_the_tiers_listed():
    with pytest.raises(ValueError, match="atmospheric_correction must be one of"):
        normalize_sentinel_s3_product_map({"truecolor": {"atmospheric_correction": "lots"}})


def test_a_product_with_no_atmosphere_to_correct_refuses_the_option():
    # chl and the rest arrive from ESA with their correction already applied, so
    # the option is not merely unsupported there - asking for it means the
    # caller has misunderstood what the product is.
    with pytest.raises(ValueError, match="no atmosphere to correct"):
        normalize_sentinel_s3_product_map({"chl": {"atmospheric_correction": "full"}})


def test_the_full_tier_will_not_choose_an_aerosol_source_for_you():
    # Q4: the source is always named. A default here would mean two runs of the
    # same command could differ because of what happened to be on disk.
    with pytest.raises(ValueError, match="needs an aerosol source"):
        normalize_sentinel_s3_product_map({"truecolor": {"atmospheric_correction": "full"}})


def test_an_aerosol_source_without_the_full_tier_is_refused_not_ignored():
    # Silently ignoring it would let someone believe they had an aerosol
    # correction when they had a Rayleigh one.
    with pytest.raises(ValueError, match="aerosol only applies to the 'full' tier"):
        normalize_sentinel_s3_product_map(
            {"truecolor": {"atmospheric_correction": "rayleigh", "aerosol": 0.1}}
        )


def test_lowering_the_tier_of_a_product_whose_table_names_a_loading_is_allowed():
    # surface_reflectance's table carries aerosol="retrieve". A caller asking it
    # for the rayleigh tier is being sensible, not contradictory, so the table's
    # own default must not trigger the refusal above.
    options = normalize_sentinel_s3_product_map(
        {"surface_reflectance": {"atmospheric_correction": "rayleigh"}}
    )["surface_reflectance"]
    assert options["atmospheric_correction"] == "rayleigh"
    assert options["aerosol"] is None


# --------------------------------------------------------------------------- #
# The product
# --------------------------------------------------------------------------- #
def test_surface_reflectance_is_corrected_by_default_because_that_is_its_point():
    options = normalize_sentinel_s3_product_map({"surface_reflectance": {}})[
        "surface_reflectance"
    ]
    assert options["atmospheric_correction"] == "full"
    assert options["aerosol"] == "retrieve"
    assert options["kind"] == "surface"


def test_the_product_never_calls_itself_water_leaving_reflectance():
    # The owner's Q5 answer, enforced. Over clear open ocean our error is about
    # nine times ESA's water-leaving signal, so the nearest wrong word would
    # make this look like a substitute for OL_2_WFR.
    from pysent._olci_correction import correct_frame  # noqa: F401
    from pysent._olci_uncertainty import uncertainty_note

    banned = ("water-leaving", "water leaving")

    # Every string the product table publishes - its label, units, and anything
    # else a caller or a GeoTIFF tag would carry.
    published = [
        value
        for value in S3_PRODUCTS["surface_reflectance"].values()
        if isinstance(value, str)
    ]
    assert published, "the product must publish some strings for this to be a check"
    for value in published:
        assert not any(phrase in value.lower() for phrase in banned), value

    # And the uncertainty note, which travels into the tags for every tier.
    for tier in ("gas", "rayleigh", "full"):
        note = uncertainty_note(tier).lower()
        assert not any(phrase in note for phrase in banned), note


def test_the_label_says_it_is_corrected_rather_than_validated():
    label = S3_PRODUCTS["surface_reflectance"]["label"]
    assert "corrected" in label.lower()
    assert "reflectance" in label.lower()


# --------------------------------------------------------------------------- #
# Against a real Level-1 product, when one is available
# --------------------------------------------------------------------------- #
def test_surface_reflectance_from_a_real_frame(olci_l1_archive, tmp_path):
    """The whole path, including the things only a real frame exercises.

    What cannot be checked synthetically: that the tie-point grids expand onto
    the image, that the retrieval finds water to invert, and that the seven
    bands come out in the order the result claims.
    """
    from pysent.s3 import process_sentinel_s3_safe

    candidates = sorted(olci_l1_archive.glob("*OL_1_EFR*.zip"))
    if not candidates:
        pytest.skip("no OL_1_EFR product in the archive directory")

    result = process_sentinel_s3_safe(
        input_dataset=str(candidates[0]), output_dir=tmp_path,
        products={"surface_reflectance": {}},
        processing_options={
            "preset": "quicklook", "gdal_cachemax_mb": 256,
            "styled": False, "thumbnail": False,
        },
    )[0]

    assert result["atmospheric_correction"] == "full"
    assert result["aerosol_source"] == "retrieve"

    # Three reflectances, three uncertainties, one quality band - and the result
    # says which is which rather than leaving a caller to count.
    assert len(result["layers"]) == 7
    assert result["quality_band"] == 7
    names = [layer["name"] for layer in result["layers"]]
    assert all("surface_reflectance" in name for name in names[:3])
    assert all("uncertainty" in name for name in names[3:6])
    assert names[6] == "quality_flags"

    # The uncertainty band is what makes shipping a dark-water value honest, so
    # it has to be present and to mean something on a real frame.
    assert "not a propagated" in result["uncertainty_meaning"]
    assert 0.0 <= result["uncertainty_unmeasured_fraction"] <= 1.0

    from osgeo import gdal

    dataset = gdal.Open(result["path"])
    assert dataset.RasterCount == 7
    quality = dataset.GetRasterBand(7).ReadAsArray()
    reflectance = dataset.GetRasterBand(1).ReadAsArray()
    uncertainty_band = dataset.GetRasterBand(4).ReadAsArray()
    dataset = None

    # The quality band is a bitfield, so every finite value must be a whole
    # number: a fractional code means it was averaged with its neighbour
    # somewhere in the warp, which produces a plausible-looking wrong answer.
    finite = np.isfinite(quality)
    assert finite.any()
    assert np.allclose(quality[finite], np.round(quality[finite]))

    # A corrected reflectance may be negative - that is kept deliberately, not
    # masked - but it should not be wild.
    valid = np.isfinite(reflectance)
    assert valid.any()
    assert reflectance[valid].min() > -0.5
    assert np.nanpercentile(reflectance[valid], 50) < 1.0

    # And wherever there is an uncertainty it is positive and of the order the
    # validation measured, not the ten-times-smaller propagated figure.
    has_error = np.isfinite(uncertainty_band)
    if has_error.any():
        assert uncertainty_band[has_error].min() > 0.0
        assert np.nanmedian(uncertainty_band[has_error]) > 0.001
