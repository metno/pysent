"""SLSTR land and sea surface temperature.

What is new here, and what the tests are about: a member whose name is
generated rather than fixed, a mask built from two different variables, a
threshold on an ordered enumeration rather than a bit field, a conversion from
kelvin to the unit people read, and a 1 km instrument that must not be put on a
300 m grid.
"""
import numpy as np
import pytest

pytest.importorskip("osgeo")

from osgeo import gdal  # noqa: E402

from pysent._sen3 import (  # noqa: E402
    extract_member,
    find_member,
    open_sen3_variable,
    read_flag_table,
    resolve_variable,
    sen3_root,
)
from pysent.errors import EmptySceneError  # noqa: E402
from pysent.s3 import (  # noqa: E402
    OLCI_GROUND_SAMPLING_M,
    S3_PRODUCTS,
    S3_TYPE_PRODUCTS,
    SLSTR_GROUND_SAMPLING_M,
    _apply_extra_mask,
    normalize_sentinel_s3_product_map,
    process_sentinel_s3_safe,
)


# --------------------------------------------------------------------------- #
# A member whose name is generated
# --------------------------------------------------------------------------- #
def test_a_generated_member_name_is_found_by_pattern(s3_slstr_sst_window):
    """The GHRSST files are named with their own timestamps, as in
    ``20260715000042-MAR-L2P_GHRSST-SSTskin-SLSTRA-NTC_DUAL_...nc``, so they can
    only be found by shape."""
    root = sen3_root(str(s3_slstr_sst_window))
    member = find_member(root, "*_DUAL_*.nc")
    assert member.endswith(".nc")
    assert "_DUAL_" in member
    assert "GHRSST" in member


def test_a_pattern_matching_nothing_says_what_the_product_has(s3_slstr_sst_window):
    root = sen3_root(str(s3_slstr_sst_window))
    with pytest.raises(ValueError, match="no member of this product matches"):
        find_member(root, "*_TRIPLE_*.nc")


def test_a_pattern_matching_several_members_is_refused(tmp_path):
    """Two matches would mean the pattern cannot say which view was used, and
    the two-view and nadir-only retrievals are different measurements.

    Built here rather than taken from the fixture, which carries only the
    two-view file: a real product has both, and that is exactly when an
    ambiguous pattern is dangerous rather than merely wrong.
    """
    import zipfile

    path = tmp_path / "twoviews.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("X.SEN3/20260101-L2P_GHRSST_DUAL_x.nc", b"stub")
        archive.writestr("X.SEN3/20260101-L2P_GHRSST_NADIR_x.nc", b"stub")
        archive.writestr("X.SEN3/xfdumanifest.xml", b"<x/>")
    root = sen3_root(str(path))
    assert "_DUAL_" in find_member(root, "*_DUAL_*.nc")
    with pytest.raises(ValueError, match="the pattern has to name one"):
        find_member(root, "*GHRSST*.nc")


def test_the_provenance_names_the_member_used_not_the_pattern(s3_slstr_sst_window, tmp_path):
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_slstr_sst_window), output_dir=tmp_path, products={"sst": {}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    assert "_DUAL_" in result["member"]
    assert "*" not in result["member"], "the pattern should be resolved before it is recorded"
    dataset = gdal.Open(result["path"])
    assert "_DUAL_" in dataset.GetMetadata()["PYSENT_MEMBER"]
    dataset = None


# --------------------------------------------------------------------------- #
# A mask from more than one variable
# --------------------------------------------------------------------------- #
def test_land_temperature_is_masked_by_two_different_variables():
    """The confidence word and the exception word live in different members -
    the exception is in the science file - and both have to apply."""
    entry = S3_PRODUCTS["lst"]
    assert entry["flag_member"] == "flags_in.nc"
    assert entry["flag_variable"] == "confidence_in"
    extra = entry["extra_masks"]
    assert len(extra) == 1
    assert extra[0]["member"] == "LST_in.nc"
    assert extra[0]["variable"] == "exception"
    assert extra[0]["reject_any"] is True


def test_any_bit_of_an_exception_word_rejects_the_pixel(s3_slstr_lst_window, tmp_path):
    """An exception says the retrieval did not complete, so there is no bit of
    it worth keeping."""
    root = sen3_root(str(s3_slstr_lst_window))
    rejected, label = _apply_extra_mask(
        spec={"member": "LST_in.nc", "variable": "exception", "reject_any": True},
        root=root, scratch=tmp_path, shape=(256, 256), decimate=1,
    )
    assert label == "exception!=0"
    # Verified against the variable itself.
    dataset = open_sen3_variable(extract_member(root, "LST_in.nc", tmp_path), "exception")
    raw = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    assert np.array_equal(rejected, raw != 0)


def test_an_ordered_quality_level_is_a_threshold_not_a_bit_test(s3_slstr_sst_window, tmp_path):
    """``quality_level`` runs 0 no-data, 1 cloud, 2 worst, 3 low, 4 acceptable,
    5 best. A bit test on that would be nonsense; the only sensible reading is
    "at or above"."""
    root = sen3_root(str(s3_slstr_sst_window))
    member = find_member(root, "*_DUAL_*.nc")
    rejected, label = _apply_extra_mask(
        spec={"member": member, "variable": "quality_level", "min_value": 4},
        root=root, scratch=tmp_path, shape=(256, 256), decimate=1,
    )
    assert label == "quality_level>=4"
    dataset = open_sen3_variable(extract_member(root, member, tmp_path), "quality_level")
    levels = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    assert np.array_equal(rejected, levels < 4)


def test_the_quality_level_enumeration_is_what_the_file_says(s3_slstr_sst_window, tmp_path):
    root = sen3_root(str(s3_slstr_sst_window))
    member = find_member(root, "*_DUAL_*.nc")
    dataset = open_sen3_variable(extract_member(root, member, tmp_path), "quality_level")
    table = read_flag_table(dataset.GetRasterBand(1))
    dataset = None
    by_value = {row["value"]: row["name"] for row in table}
    assert by_value[0] == "no_data"
    assert by_value[4] == "acceptable_quality"
    assert by_value[5] == "best_quality"


def test_a_lower_threshold_keeps_more(s3_slstr_sst_window, tmp_path):
    """Q13's open half: 4 or better is the mission's recommendation for a
    quantitative use, 3 buys coverage at a cost in accuracy."""
    strict = process_sentinel_s3_safe(
        input_dataset=str(s3_slstr_sst_window), output_dir=tmp_path / "strict", products={"sst": {}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    loose = process_sentinel_s3_safe(
        input_dataset=str(s3_slstr_sst_window), output_dir=tmp_path / "loose",
        products={"sst": {"extra_masks": (
            {"member": "*_DUAL_*.nc", "variable": "quality_level", "min_value": 3},
        )}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    assert loose["valid_fraction"] >= strict["valid_fraction"]


# --------------------------------------------------------------------------- #
# The unit people read
# --------------------------------------------------------------------------- #
def test_temperature_is_stored_in_celsius_not_kelvin(s3_slstr_lst_window, tmp_path):
    """The files hold kelvin; nobody reads a map in kelvin. The conversion is
    applied before writing and recorded, so the file says what its numbers are.
    """
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_slstr_lst_window), output_dir=tmp_path, products={"lst": {}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    assert result["units"] == "degC"
    statistics = result["value_stats"]
    # A land surface, so well away from zero kelvin and plausible as celsius.
    assert -60.0 < statistics["p50"] < 70.0
    assert result["provenance"]["PYSENT_UNITS"] == "degC"


def test_asking_for_kelvin_gives_kelvin(s3_slstr_lst_window, tmp_path):
    celsius = process_sentinel_s3_safe(
        input_dataset=str(s3_slstr_lst_window), output_dir=tmp_path / "c", products={"lst": {}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    kelvin = process_sentinel_s3_safe(
        input_dataset=str(s3_slstr_lst_window), output_dir=tmp_path / "k",
        products={"lst": {"unit": "kelvin"}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    assert kelvin["value_stats"]["p50"] == pytest.approx(celsius["value_stats"]["p50"] + 273.15, abs=0.05)


def test_an_unknown_unit_is_refused():
    from pysent.s3 import normalize_sentinel_s3_product_map

    with pytest.raises(ValueError, match="unit must be one of"):
        normalize_sentinel_s3_product_map({"lst": {"unit": "fahrenheit"}})


# --------------------------------------------------------------------------- #
# A 1 km instrument on a 1 km grid
# --------------------------------------------------------------------------- #
def test_slstr_products_ask_for_a_kilometre_grid():
    """SLSTR samples at 1 km. Put on OLCI's 300 m grid it is oversampled more
    than threefold, which costs three times the time and four times the memory
    and adds nothing - the same mistake the ozone profile caught in phase 2.
    """
    assert SLSTR_GROUND_SAMPLING_M == 1000.0
    assert SLSTR_GROUND_SAMPLING_M > OLCI_GROUND_SAMPLING_M
    for name in ("lst", "sst"):
        assert S3_PRODUCTS[name]["ground_sampling_m"] == SLSTR_GROUND_SAMPLING_M
    # An OLCI product has no override and takes the family default.
    assert "ground_sampling_m" not in S3_PRODUCTS["chl"]


def test_the_output_grid_is_about_a_kilometre(s3_slstr_lst_window, tmp_path):
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_slstr_lst_window), output_dir=tmp_path, products={"lst": {}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    assert result["epsg"] == 4326
    assert result["resolution"] == pytest.approx(1000.0 / 111_320.0, rel=1e-6)


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fixture_name, product", [
    ("s3_slstr_lst_window", "lst"),
    ("s3_slstr_sst_window", "sst"),
])
def test_each_product_makes_a_plausible_map(request, fixture_name, product, tmp_path):
    window = request.getfixturevalue(fixture_name)
    result = process_sentinel_s3_safe(
        input_dataset=str(window), output_dir=tmp_path, products={product: {}}
    )[0]
    assert result["valid_fraction"] > 0.05
    for key in ("path", "styled_path", "thumbnail_path"):
        assert key in result
    statistics = result["value_stats"]
    # Temperatures on Earth's surface, in celsius.
    assert -90.0 < statistics["min"] <= statistics["max"] < 80.0
    assert result["units"] == "degC"


def test_sea_temperature_is_near_freezing_or_above_not_below(s3_slstr_sst_window, tmp_path):
    """Sea water does not go far below -2 C, so a colder median would mean the
    packing or the unit conversion is wrong."""
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_slstr_sst_window), output_dir=tmp_path, products={"sst": {}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    assert result["value_stats"]["p01"] > -3.0
    assert result["value_stats"]["p99"] < 40.0


def test_a_land_product_on_a_sea_frame_finds_nothing(s3_slstr_sst_window, tmp_path):
    with pytest.raises((EmptySceneError, ValueError)):
        process_sentinel_s3_safe(
            input_dataset=str(s3_slstr_sst_window), output_dir=tmp_path, products={"lst": {}}
        )


def test_the_slstr_types_are_routable_and_name_real_products():
    assert S3_TYPE_PRODUCTS["SL_2_LST"] == ("lst",)
    assert S3_TYPE_PRODUCTS["SL_2_WST"] == ("sst",)
    from pysent.profiles import supported_sentinel_product_types

    ready = supported_sentinel_product_types()
    assert "S3 SL_2_LST" in ready
    assert "S3 SL_2_WST" in ready


def test_an_all_masked_swath_frame_can_be_written_instead_of_raising(s3_slstr_lst_window, tmp_path):
    """Regression: ``on_empty="write"`` swapped one exception for another.

    The empty-scene guard is skipped for ``write``, but the warp was still
    given ``minimum_valid=1``, and ``warp_swath`` refuses a warp that produced
    no valid pixel. So a frame whose mask keeps nothing - a winter frame with
    ``HISOLZEN`` rejected, say - could not be written at all.

    A ``min_value`` above anything the file holds is the deterministic way to
    keep nothing; the real trigger was a flag option.
    """
    products = {"lst": {"extra_masks": ({"member": "LST_in.nc", "variable": "exception",
                                         "min_value": 10_000},)}}

    with pytest.raises(EmptySceneError):
        process_sentinel_s3_safe(
            input_dataset=str(s3_slstr_lst_window),
            output_dir=tmp_path / "raise",
            products=products,
            processing_options={"styled": False, "thumbnail": False},
        )

    result = process_sentinel_s3_safe(
        input_dataset=str(s3_slstr_lst_window),
        output_dir=tmp_path / "write",
        products=products,
        processing_options={"styled": False, "thumbnail": False, "on_empty": "write"},
    )[0]

    assert result["valid_fraction"] == 0.0
    dataset = gdal.Open(result["path"])
    values = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    # An empty product, not a truncated one: the grid is there and every cell
    # is no-data.
    assert values.size > 0
    assert not np.isfinite(values).any()


# --------------------------------------------------------------------------- #
# The SST view is a request option (review-study item R3)
# --------------------------------------------------------------------------- #
def test_the_sst_view_sets_the_member_the_geolocation_and_the_flags():
    """Regression: NADIR was only reachable by patching the product table.

    A view spans three table entries, so overriding ``variable`` - the only
    one the request could reach - could never select it, and on the swath path
    even that was ignored.
    """
    dual = normalize_sentinel_s3_product_map({"sst": {}})["sst"]
    assert dual["view"] == "dual"
    assert dual["candidates"] == (("*_DUAL_*.nc", "sea_surface_temperature"),)
    assert dual["geolocation"] == ("*_DUAL_*.nc", "lat", "lon")
    assert dual["flag_member"] == "*_DUAL_*.nc"
    assert dual["extra_masks"][0]["member"] == "*_DUAL_*.nc"

    nadir = normalize_sentinel_s3_product_map({"sst": {"view": "nadir"}})["sst"]
    assert nadir["view"] == "nadir"
    assert nadir["candidates"] == (("*_NADIR_*.nc", "sea_surface_temperature"),)
    assert nadir["geolocation"] == ("*_NADIR_*.nc", "lat", "lon")
    assert nadir["flag_member"] == "*_NADIR_*.nc"
    assert nadir["extra_masks"][0]["member"] == "*_NADIR_*.nc"
    # Everything else is the product's own: a view is not a different product.
    assert nadir["value_range"] == dual["value_range"]
    assert list(nadir["reject_flags"]) == list(dual["reject_flags"])


def test_an_unknown_view_is_refused_and_a_view_on_the_wrong_product_too():
    with pytest.raises(ValueError, match="view must be one of"):
        normalize_sentinel_s3_product_map({"sst": {"view": "triple"}})
    with pytest.raises(ValueError, match="no views to choose between"):
        normalize_sentinel_s3_product_map({"lst": {"view": "nadir"}})


def test_a_requested_variable_reaches_the_swath_resolver():
    """A swath product resolves its variable from the candidates, so a
    requested variable has to be put there or it is silently ignored.
    """
    asked = normalize_sentinel_s3_product_map(
        {"sst": {"variable": "sst_theoretical_uncertainty"}}
    )["sst"]
    assert asked["candidates"] == (("*_DUAL_*.nc", "sst_theoretical_uncertainty"),)
    # The member pattern is still the table's: a caller names a variable, not a file.
    assert asked["geolocation"] == ("*_DUAL_*.nc", "lat", "lon")


def test_a_single_file_sst_product_says_what_layout_it_has(tmp_path):
    """Regression: the orbit-long WST dumps hold one GHRSST file, not a pair.

    ``find_member('*_DUAL_*.nc')`` could only say "no member matches", which is
    true and useless. The stub here reproduces the layout the review study
    found - a single file with no view in its name - because no product of
    that vintage is on this machine; what is asserted is the diagnosis, not
    that pysent can read it, which it cannot yet (R2).
    """
    import zipfile

    from pysent._sen3 import find_member, sen3_root

    name = "S3A_SL_2_WST____20251118T192522_20251118T210621_20251120T004133_6059_133_013______MAR_O_NT_003"
    product = tmp_path / f"{name}.zip"
    single = "20251118192522-MAR-L2P_GHRSST-SSTskin-SLSTRA-20251120004133-v02.0-fv01.0.nc"
    with zipfile.ZipFile(product, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(f"{name}.SEN3/{single}", b"stub")
        archive.writestr(f"{name}.SEN3/xfdumanifest.xml", b"<xfdu:XFDU/>")

    with pytest.raises(ValueError) as caught:
        find_member(sen3_root(str(product)), "*_DUAL_*.nc")
    message = str(caught.value)
    assert "single GHRSST file" in message
    assert single in message
    assert "R2" in message


def test_a_product_with_the_pair_is_unaffected(tmp_path):
    """The hint must not fire where the pair is present - only the match matters."""
    import zipfile

    from pysent._sen3 import find_member, sen3_root

    product = tmp_path / "pair.zip"
    with zipfile.ZipFile(product, "w", compression=zipfile.ZIP_STORED) as archive:
        for view in ("DUAL", "NADIR"):
            archive.writestr(f"P.SEN3/2026-MAR-L2P_GHRSST-SSTskin-SLSTRA-NTC_{view}_x.nc", b"stub")
        archive.writestr("P.SEN3/xfdumanifest.xml", b"<xfdu:XFDU/>")

    assert "_DUAL_" in find_member(sen3_root(str(product)), "*_DUAL_*.nc")


def test_the_slstr_products_ask_for_average_and_a_caller_still_wins():
    """Q4: the SLSTR values are averaged; everything else keeps nearest.

    A product default only fills in where the caller named nothing, so a
    request for nearest is still honoured.
    """
    from pysent.s3 import _resampling_for, _resolve_processing_settings

    default = _resolve_processing_settings({})
    assert default["resampling"] == "near"
    for product in ("lst", "sst"):
        options = normalize_sentinel_s3_product_map({product: {}})[product]
        assert _resampling_for(options, default) == "average", product
    # A grid product is untouched by this.
    ndvi = normalize_sentinel_s3_product_map({"ndvi": {}})["ndvi"]
    assert _resampling_for(ndvi, default) == "near"

    asked = _resolve_processing_settings({"resampling": "near"})
    lst = normalize_sentinel_s3_product_map({"lst": {}})["lst"]
    assert _resampling_for(lst, asked) == "near"
    bilinear = _resolve_processing_settings({"resampling": "bilinear"})
    assert _resampling_for(lst, bilinear) == "bilinear"
