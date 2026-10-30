"""OLCI Level-2: the geolocation warp, baseline drift, and keep/reject masks.

The warp traps here are the expensive kind. None of them raises; each one
produces a raster of exactly the right size that is empty, shifted, or
plausible and wrong. The measurements quoted in the tests come from real OLCI
frames and are reproduced by ``PLANNING/s5p_s3_audit``.
"""
import numpy as np
import pytest

pytest.importorskip("osgeo")

from osgeo import gdal  # noqa: E402

from pysent._output import styled_bytes  # noqa: E402
from pysent._sen3 import (  # noqa: E402
    flag_selection,
    is_log_packed,
    open_sen3_variable,
    read_packed,
    resolve_variable,
    sen3_root,
)
from pysent._swath import (  # noqa: E402
    GEOLOCATION_STEPS,
    REFUSED_GEOLOCATION_STEP,
    warp_swath,
)
from pysent.errors import EmptySceneError  # noqa: E402
from pysent.s3 import (  # noqa: E402
    AUTO_GEOLOCATION_STEP,
    EXACT_WARP_PIXEL_LIMIT,
    S3_PRODUCTS,
    S3_TYPE_PRODUCTS,
    _resolve_geoloc_step,
    normalize_sentinel_s3_product_map,
    process_sentinel_s3_safe,
)


def _simple_swath(rows=24, columns=24):
    lat = np.repeat(np.linspace(60.0, 61.0, rows)[:, None], columns, axis=1)
    lon = np.repeat(np.linspace(10.0, 11.0, columns)[None, :], rows, axis=0)
    return lat, lon


# --------------------------------------------------------------------------- #
# The warp traps
# --------------------------------------------------------------------------- #
def test_an_unusable_geolocation_array_is_caught_rather_than_returned_empty(tmp_path):
    """The trap that matters most, because GDAL does not report it.

    Handed a geolocation array it cannot use - unscaled integer micro-degrees
    are the real case - GDAL logs "too many points failed to transform", writes
    a raster of exactly the right size containing **no valid pixels**, and
    returns successfully. Measured on a real frame: 0 valid pixels where a
    scaled array gave 2,171,012. So the warp checks its own output.
    """
    lat, lon = _simple_swath()
    values = np.ones(lat.shape, dtype=np.float32)
    # Micro-degrees, as the file stores them, with the scale not applied.
    unscaled_lat = lat * 1e6
    unscaled_lon = lon * 1e6

    with pytest.raises(ValueError, match="truncated or empty"):
        warp_swath(
            values=values, lat=unscaled_lat, lon=unscaled_lon,
            output_path=str(tmp_path / "bad.tif"),
            epsg=4326, resolution=0.01, bounds=(10.0, 60.0, 11.0, 61.0),
            resampling="near", scratch=tmp_path,
            minimum_valid=max(1, values.size // 100),
        )


def test_a_good_warp_passes_the_same_check(tmp_path):
    lat, lon = _simple_swath()
    values = np.ones(lat.shape, dtype=np.float32)
    output = warp_swath(
        values=values, lat=lat, lon=lon, output_path=str(tmp_path / "good.tif"),
        epsg=4326, resolution=0.01, bounds=(10.0, 60.0, 11.0, 61.0),
        resampling="near", scratch=tmp_path, minimum_valid=max(1, values.size // 100),
    )
    dataset = gdal.Open(output)
    grid = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    assert np.count_nonzero(np.isfinite(grid)) > values.size // 100


def test_the_step_that_has_been_seen_to_truncate_is_refused(tmp_path):
    """Step 4 measured correct on a 296-row frame but returned a truncated warp
    on a full 4091-row one. Steps 2 and 8 are indistinguishable from it in
    accuracy, so allowing it would buy nothing."""
    lat, lon = _simple_swath()
    with pytest.raises(ValueError, match="refused"):
        warp_swath(
            values=np.ones(lat.shape, dtype=np.float32), lat=lat, lon=lon,
            output_path=str(tmp_path / "x.tif"), epsg=4326, resolution=0.01,
            resampling="near", scratch=tmp_path, geoloc_step=REFUSED_GEOLOCATION_STEP,
        )
    assert REFUSED_GEOLOCATION_STEP not in GEOLOCATION_STEPS


def test_an_unknown_step_is_refused_with_the_allowed_ones(tmp_path):
    lat, lon = _simple_swath()
    with pytest.raises(ValueError, match="geoloc_step must be one of"):
        warp_swath(
            values=np.ones(lat.shape, dtype=np.float32), lat=lat, lon=lon,
            output_path=str(tmp_path / "x.tif"), epsg=4326, resolution=0.01,
            resampling="near", scratch=tmp_path, geoloc_step=7,
        )


ROWS = COLUMNS = 64


def _ramp_swath():
    lat = np.repeat(np.linspace(60.0, 60.63, ROWS)[:, None], COLUMNS, axis=1)
    lon = np.repeat(np.linspace(10.0, 10.63, COLUMNS)[None, :], ROWS, axis=0)
    # Each pixel identifiable, so a shifted result is a different number.
    values = (np.arange(ROWS)[:, None] * COLUMNS + np.arange(COLUMNS)[None, :]).astype(np.float32)
    return lat, lon, values


BOUNDS = (10.0, 60.0, 10.64, 60.64)
RESOLUTION = 0.01


def _warp_with_library(tmp_path, step, name):
    lat, lon, values = _ramp_swath()
    output = warp_swath(
        values=values, lat=lat, lon=lon, output_path=str(tmp_path / f"{name}.tif"),
        epsg=4326, resolution=RESOLUTION, bounds=BOUNDS,
        resampling="near", scratch=tmp_path, geoloc_step=step,
    )
    dataset = gdal.Open(output)
    grid = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    return grid


def _warp_without_offset(tmp_path, step, name):
    """The same warp with PIXEL_OFFSET left at zero, built by hand.

    Nothing in the library can produce this, which is the point: the offset is
    not an option. It is reproduced here so the cost of omitting it is measured
    rather than asserted.
    """
    from pysent._swath import _GEOLOCATION_SRS, _write_geolocation_tiff, _write_value_tiff

    lat, lon, values = _ramp_swath()
    value_path = str(tmp_path / f"{name}_v.tif")
    lat_path = str(tmp_path / f"{name}_la.tif")
    lon_path = str(tmp_path / f"{name}_lo.tif")
    vrt_path = str(tmp_path / f"{name}.vrt")
    _write_value_tiff(value_path, values)
    _write_geolocation_tiff(lat_path, lat[::step, ::step])
    _write_geolocation_tiff(lon_path, lon[::step, ::step])

    vrt = gdal.Translate(vrt_path, value_path, format="VRT")
    vrt.SetMetadata(
        {
            "SRS": _GEOLOCATION_SRS,
            "X_DATASET": lon_path, "X_BAND": "1",
            "Y_DATASET": lat_path, "Y_BAND": "1",
            "PIXEL_OFFSET": "0", "LINE_OFFSET": "0",
            "PIXEL_STEP": str(step), "LINE_STEP": str(step),
            "GEOREFERENCING_CONVENTION": "PIXEL_CENTER",
        },
        "GEOLOCATION",
    )
    vrt.FlushCache()
    vrt = None

    output = str(tmp_path / f"{name}.tif")
    gdal.Warp(output, vrt_path, format="GTiff", dstSRS="EPSG:4326", geoloc=True,
              outputBounds=BOUNDS, xRes=RESOLUTION, yRes=RESOLUTION, resampleAlg="near",
              dstNodata=float("nan"), outputType=gdal.GDT_Float32)
    dataset = gdal.Open(output)
    grid = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    return grid


def _agreement(left, right):
    """Share of cells valid in both that hold the same source pixel."""
    both = np.isfinite(left) & np.isfinite(right)
    if not both.any():
        return 0.0
    return float(np.mean(left[both] == right[both]))


# Step 2 is left out on purpose. Its shift is half a source pixel, and on this
# synthetic grid - source spacing equal to the output resolution - half a pixel
# rounds into the same cell, so both configurations agree exactly. On a real
# frame, where the output grid is finer than the along-track sampling, step 2
# did show the error: 47.5 % of source pixels in their own cell without the
# offset against 87.9 % with it.
@pytest.mark.parametrize("step", [8, 16])
def test_a_decimated_geolocation_needs_the_half_block_offset(step, tmp_path):
    """GDAL reads every n-th sample as the *centre* of an n x n block.

    Without a ``-(n-1)/2`` shift everything lands (n-1)/2 source pixels away.
    Measured on a real OLCI frame as the share of source pixels reaching the
    output cell holding their own coordinates: about 88 % with the offset at
    every step from 1 to 16, against 47.5 % at step 2 and **0 %** at steps 4, 8
    and 16 without it - a miss growing to roughly 3 km.

    Here the exact warp is the reference and the question is which decimated
    warp reproduces it.
    """
    exact = _warp_with_library(tmp_path, 1, "exact")
    with_offset = _warp_with_library(tmp_path, step, f"on{step}")
    without_offset = _warp_without_offset(tmp_path, step, f"off{step}")

    kept = _agreement(exact, with_offset)
    dropped = _agreement(exact, without_offset)
    assert kept > 0.75, f"step {step} with the offset should reproduce the exact warp, got {kept:.2f}"
    assert dropped < kept, (
        f"omitting the half-block offset should disagree with the exact warp: "
        f"with {kept:.2f}, without {dropped:.2f}"
    )
    # The miss grows with the step, so a bigger step without the offset is worse.
    assert dropped < 0.9


def test_an_exact_warp_is_its_own_reference(tmp_path):
    assert _agreement(_warp_with_library(tmp_path, 1, "a"), _warp_with_library(tmp_path, 1, "b")) == 1.0


# --------------------------------------------------------------------------- #
# Choosing the step
# --------------------------------------------------------------------------- #
def test_auto_warps_a_small_frame_exactly_and_a_large_one_decimated():
    """Accuracy hardly depends on the step once the offset is right, so the
    choice is made on cost - and the cheaper option reverses with frame size.
    Measured at 0.003 deg: a 1.4 Mpx frame took 2.8 s exact against 3.5 s at
    step 8; a 19.9 Mpx frame took 39.4 s exact against 5.6 s at step 8."""
    assert _resolve_geoloc_step("auto", EXACT_WARP_PIXEL_LIMIT // 2) == 1
    assert _resolve_geoloc_step("auto", EXACT_WARP_PIXEL_LIMIT * 5) == AUTO_GEOLOCATION_STEP
    assert _resolve_geoloc_step(None, EXACT_WARP_PIXEL_LIMIT * 5) == AUTO_GEOLOCATION_STEP


def test_an_explicit_step_overrides_the_policy():
    assert _resolve_geoloc_step(16, 100) == 16


def test_a_bad_step_in_a_request_is_refused_before_anything_is_opened():
    with pytest.raises(ValueError, match="geoloc_step must be 'auto' or one of"):
        normalize_sentinel_s3_product_map({"chl": {"geoloc_step": 3}})


# --------------------------------------------------------------------------- #
# Packing that announces itself
# --------------------------------------------------------------------------- #
def test_log_packing_is_detected_from_the_units(s3_olci_window, tmp_path):
    """``CHL_NN`` has units ``lg(re mg.m-3)``: the file says it holds a
    logarithm. Detected rather than hard-coded, so it survives a baseline that
    renames the variable - and checked against the variables that are *not*
    log-packed, whose units carry no ``lg(``."""
    root = sen3_root(str(s3_olci_window))
    local, member, variable = resolve_variable(root, [("chl_nn.nc", "CHL_NN")], tmp_path)
    dataset = open_sen3_variable(local, variable)
    band = dataset.GetRasterBand(1)
    assert is_log_packed(band)

    linear = read_packed(band, log10=False)
    unpacked = read_packed(band)  # auto
    dataset = None

    finite = np.isfinite(unpacked)
    assert np.allclose(unpacked[finite], 10.0 ** linear[finite], rtol=1e-4)
    # Forgetting the power of ten maps chlorophyll onto roughly -2..2 instead of
    # a concentration, which is the whole point of detecting it.
    assert linear[finite].max() < 3.0
    assert unpacked[finite].max() > 3.0
    assert unpacked[finite].min() > 0.0


def test_a_variable_with_plain_units_is_not_treated_as_logarithmic():
    class Band:
        def GetMetadata(self):
            return {"units": "kg.m-2"}

    assert not is_log_packed(Band())


# --------------------------------------------------------------------------- #
# Baseline drift
# --------------------------------------------------------------------------- #
def test_the_first_candidate_this_product_has_wins(s3_olci_window, tmp_path):
    """Variable names move between baselines, so a list of candidates is tried.

    ``trsp.nc:kd_490`` under baseline 004 is ``trsp.nc:KD490_M07`` under 003,
    and the packing differs too - offsets of -6 and -2 - which is why values
    are read from the file rather than from a table.
    """
    root = sen3_root(str(s3_olci_window))
    local, member, variable = resolve_variable(
        root, [("nothing.nc", "NOPE"), ("chl_nn.nc", "CHL_NN")], tmp_path
    )
    assert (member, variable) == ("chl_nn.nc", "CHL_NN")


def test_no_matching_candidate_names_everything_it_looked_for(s3_olci_window, tmp_path):
    """"Variable not found" alone does not say whether the product is unexpected
    or the table is out of date."""
    root = sen3_root(str(s3_olci_window))
    with pytest.raises(ValueError, match="none of absent.nc:A, other.nc:B"):
        resolve_variable(root, [("absent.nc", "A"), ("other.nc", "B")], tmp_path)


def test_a_default_mask_tolerates_a_flag_an_older_baseline_lacks():
    """Baseline 004 of a water product defines 57 flags and 003 only 53.

    A default list naming ``CLOUD_SHADOW`` is correct for one and impossible for
    the other, so the default skips what is absent and reports it. Refusing the
    product would be wrong; silently ignoring it would hide a stale table.
    """
    table = [{"name": "WATER", "mask": 2, "value": 2}, {"name": "CLOUD", "mask": 4, "value": 4}]
    flags = np.array([[2, 4, 6]], dtype=np.uint64)

    selected, missing = flag_selection(flags, table, ["CLOUD", "CLOUD_SHADOW"], allow_missing=True)
    assert missing == ["CLOUD_SHADOW"]
    assert selected.tolist() == [[False, True, True]]


def test_a_requested_mask_is_strict_about_a_flag_that_does_not_exist():
    """A typo in a request must not quietly become a mask that does nothing."""
    table = [{"name": "WATER", "mask": 2, "value": 2}]
    flags = np.zeros((2, 2), dtype=np.uint64)
    with pytest.raises(ValueError, match="not defined in this product"):
        flag_selection(flags, table, ["WATTER"])


# --------------------------------------------------------------------------- #
# Keep and reject
# --------------------------------------------------------------------------- #
def test_a_water_product_keeps_water_and_rejects_the_failure_conditions():
    """The hard tier is about validity, not contamination.

    A pixel that is invalid, over land, or whose retrieval reported failure has
    no value worth keeping in any output. A pixel merely beside a cloud does,
    which is why the cloud-margin flags sit in the browse tier instead.
    """
    entry = S3_PRODUCTS["chl"]
    assert entry["mask_flags"] == ("WATER",)
    for name in ("INVALID", "LAND", "CLOUD", "AC_FAIL", "OCNN_FAIL"):
        assert name in entry["reject_flags"], name
    for name in ("CLOUD_MARGIN", "CLOUD_AMBIGUOUS", "CLOUD_SHADOW"):
        assert name in entry["browse_reject_flags"], name
        assert name not in entry["reject_flags"], name


def test_a_land_product_keeps_land():
    assert S3_PRODUCTS["gifapar"]["mask_flags"] == ("LAND",)
    assert "HISOLZEN" in S3_PRODUCTS["gifapar"]["reject_flags"]
    assert "CLOUD_MARGIN" in S3_PRODUCTS["gifapar"]["browse_reject_flags"]


def test_keep_and_reject_are_separate_questions():
    """Not expressible as one list: a water product keeps one flag and rejects
    eleven, and neither half implies the other."""
    from pysent.s3 import _apply_flag_mask

    table = [{"name": "WATER", "mask": 1, "value": 1}, {"name": "CLOUD", "mask": 2, "value": 2}]
    flags = np.array([[0, 1, 2, 3]], dtype=np.uint64)  # none, water, cloud, water+cloud
    values = np.ones((1, 4), dtype=np.float32)

    kept, _ = _apply_flag_mask(values=values, flags=flags, table=table,
                               keep=["WATER"], reject=["CLOUD"])
    assert kept.tolist() == [[False, True, False, False]]


def test_rejecting_nothing_keeps_every_water_pixel(s3_olci_window, tmp_path):
    strict = process_sentinel_s3_safe(
        input_dataset=str(s3_olci_window), output_dir=tmp_path / "strict",
        products={"chl": {}}, processing_options={"styled": False, "thumbnail": False},
    )[0]
    loose = process_sentinel_s3_safe(
        input_dataset=str(s3_olci_window), output_dir=tmp_path / "loose",
        products={"chl": {"reject_flags": "none"}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    assert strict["valid_fraction"] < loose["valid_fraction"]
    assert strict["reject_flags"] and loose["reject_flags"] == []


def test_keeping_land_on_a_marine_window_keeps_far_less_than_keeping_water(s3_olci_window, tmp_path):
    """The window was chosen for water, so a land mask should barely match.

    Not asserted as empty: this is a coastal frame and it does contain some
    land, which is itself worth knowing - a "water product over land is empty"
    rule holds for a frame, not for every window of one.
    """
    water = process_sentinel_s3_safe(
        input_dataset=str(s3_olci_window), output_dir=tmp_path / "water",
        products={"chl": {"reject_flags": "none"}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    land = process_sentinel_s3_safe(
        input_dataset=str(s3_olci_window), output_dir=tmp_path / "land",
        products={"chl": {"mask_flags": ["LAND"], "reject_flags": "none"}},
        processing_options={"styled": False, "thumbnail": False, "on_empty": "write"},
    )[0]
    assert land["valid_fraction"] < water["valid_fraction"] / 2


# --------------------------------------------------------------------------- #
# The log colour scale
# --------------------------------------------------------------------------- #
def test_a_log_scale_spreads_a_quantity_that_spans_orders_of_magnitude():
    """Chlorophyll runs from about 0.01 to 100 mg m-3. Linearly, everything
    below 1 collapses into the bottom two levels of the ramp."""
    values = np.array([[0.01, 0.1, 1.0, 10.0]], dtype=np.float32)
    linear = styled_bytes(values, (0.01, 50.0))
    logarithmic = styled_bytes(values, (0.01, 50.0), log_scale=True)

    assert linear[0, 0] == linear[0, 1] == 1  # 0.01 and 0.1 indistinguishable
    assert len(set(logarithmic[0].tolist())) == 4  # all four separable
    assert list(logarithmic[0]) == sorted(logarithmic[0])


def test_a_log_scale_needs_a_positive_lower_bound():
    values = np.array([[1.0]], dtype=np.float32)
    with pytest.raises(ValueError, match="positive lower bound"):
        styled_bytes(values, (0.0, 10.0), log_scale=True)
    with pytest.raises(ValueError, match="positive lower bound"):
        normalize_sentinel_s3_product_map({"chl": {"value_range": (0.0, 10.0)}})


def test_a_value_at_or_below_zero_lands_at_the_bottom_of_a_log_ramp():
    """It has no logarithm, and dropping it would turn a real measurement into
    missing data."""
    values = np.array([[-1.0, 0.0, 0.01]], dtype=np.float32)
    styled = styled_bytes(values, (0.01, 50.0), log_scale=True)
    assert styled.tolist() == [[1, 1, 1]]


def test_the_style_hint_and_the_tags_say_which_scale_was_used(s3_olci_window, tmp_path):
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_olci_window), output_dir=tmp_path, products={"chl": {}}
    )[0]
    assert result["style_hint"]["scale"] == "log10"
    assert result["provenance"]["PYSENT_STYLE_SCALE"] == "log10"
    dataset = gdal.Open(result["styled_path"])
    assert dataset.GetMetadata()["PYSENT_STYLE_SCALE"] == "log10"
    dataset = None


# --------------------------------------------------------------------------- #
# End to end on the committed window
# --------------------------------------------------------------------------- #
def test_chlorophyll_is_plausible_on_a_real_water_window(s3_olci_window, tmp_path):
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_olci_window), output_dir=tmp_path, products={"chl": {}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    statistics = result["value_stats"]
    # A concentration, so strictly positive, and not the -3..2 that forgetting
    # the log10 would give.
    assert statistics["min"] > 0.0
    assert 0.01 < statistics["p50"] < 100.0
    low, high = S3_PRODUCTS["chl"]["value_range"]
    assert low <= statistics["p50"] <= high
    assert result["units"] == "mg m-3"


def test_the_swath_output_is_projected_and_carries_its_provenance(s3_olci_window, tmp_path):
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_olci_window), output_dir=tmp_path, products={"chl": {}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    dataset = gdal.Open(result["path"])
    projection = dataset.GetProjection()
    tags = dataset.GetMetadata()
    dataset = None

    assert "4326" in projection
    assert result["epsg"] == 4326
    # 300 m ground sampling expressed in degrees.
    assert result["resolution"] == pytest.approx(300.0 / 111_320.0, rel=1e-6)
    assert tags["PYSENT_MEMBER"] == "chl_nn.nc"
    assert tags["PYSENT_VARIABLE"] == "CHL_NN"
    assert tags["PYSENT_BASELINE"] == "004"
    assert tags["PYSENT_GEOLOC_STEP"] == "1"
    assert tags["PYSENT_MASK_FLAGS"] == "WATER"
    assert "LAND" in tags["PYSENT_REJECT_FLAGS"]
    assert "CLOUD_MARGIN" in tags["PYSENT_BROWSE_REJECT_FLAGS"]


def test_the_result_reports_which_member_and_variable_were_used(s3_olci_window, tmp_path):
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_olci_window), output_dir=tmp_path, products={"chl": {}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    assert result["member"] == "chl_nn.nc"
    assert result["variable"] == "CHL_NN"
    assert result["unavailable_flags"] == []
    assert result["geoloc_step"] == 1


def test_every_olci_type_names_products_that_exist():
    for product_type, names in S3_TYPE_PRODUCTS.items():
        for name in names:
            assert name in S3_PRODUCTS, f"{product_type} names unknown product {name}"
    for name, entry in S3_PRODUCTS.items():
        if entry["kind"] == "swath":
            assert entry["candidates"], name
            assert entry["flag_member"] and entry["flag_variable"], name
