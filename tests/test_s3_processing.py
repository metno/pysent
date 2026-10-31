"""Sentinel-3 Synergy processing: the archive reader, flags, grid and outputs.

Everything here runs against ``tests/data/s3_synergy_window.zip``, a 384x384
window cut from a real daily VG1 tile over southern Norway. It keeps the
``.SEN3`` layout, the member names, the byte packing and the whole flag table,
so the parts that are easy to get quietly wrong - the packing, the mixed flag
conventions, the assigned CRS - are tested against real structure rather than
against a mock.
"""
import zipfile
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("osgeo")

from osgeo import gdal  # noqa: E402

from pysent._sen3 import (  # noqa: E402
    check_members_stored,
    extract_member,
    flag_selection,
    open_sen3_variable,
    read_flag_table,
    read_packed,
    read_sen3_manifest,
    sen3_members,
    sen3_root,
)
from pysent.errors import EmptySceneError  # noqa: E402
from pysent.s3 import (  # noqa: E402
    REJECT_REASONS,
    S3_PRODUCTS,
    S3_TYPE_PRODUCTS,
    build_sentinel_s3_output_filename,
    default_sentinel_s3_products,
    normalize_sentinel_s3_product_map,
    process_sentinel_s3_safe,
)


# --------------------------------------------------------------------------- #
# Finding and reading the archive
# --------------------------------------------------------------------------- #
def test_the_product_directory_is_found_inside_the_zip(s3_synergy_window):
    root = sen3_root(str(s3_synergy_window))
    assert root.startswith("/vsizip/")
    assert root.upper().endswith(".SEN3")
    assert "NDVI.nc" in sen3_members(root)


def test_a_remote_archive_takes_the_vsicurl_path():
    """A URL ends in .zip too, so the order of these two tests matters.

    Checked without network: only the path that would be opened is asserted.
    An earlier version put the .zip test first and built ``/vsizip/https://...``,
    which fails with a missing-manifest error that looks like bad data.
    """
    remote = sen3_root("https://example.invalid/archive/S3B_SY_2_VG1____x.zip")
    assert remote.startswith("/vsizip//vsicurl/https://")


def test_an_unpacked_directory_is_taken_as_is():
    assert sen3_root("/data/S3B_SY_2_VG1____x.SEN3") == "/data/S3B_SY_2_VG1____x.SEN3"
    assert sen3_root("/data/S3B_SY_2_VG1____x.SEN3/") == "/data/S3B_SY_2_VG1____x.SEN3"


def test_every_member_is_stored_so_one_variable_can_be_range_read(s3_synergy_window):
    """The property the whole remote path depends on.

    Every member of every real product inspected is STORED, which is why a
    single 13 MB variable can be lifted out of a 143 MB archive by reading only
    its own bytes. The fixture is built the same way so this path is exercised.
    """
    assert check_members_stored(str(s3_synergy_window)) == []
    with zipfile.ZipFile(s3_synergy_window) as archive:
        assert all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())


def test_a_compressed_member_is_reported_rather_than_rejected(tmp_path):
    """Compression is not an error - it only makes a remote read expensive."""
    path = tmp_path / "deflated.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("X.SEN3/NDVI.nc", b"not really netcdf")
    assert check_members_stored(str(path)) == ["X.SEN3/NDVI.nc"]


def test_extracting_a_member_lets_the_netcdf_driver_open_it(s3_synergy_window, tmp_path):
    """Why the reader copies a member out before opening it.

    GDAL's netCDF driver cannot open a file inside a zip without Linux
    ``userfaultfd``. Whether that syscall is available is a property of the
    *host*, not of the data: a container under Docker's default seccomp profile
    is refused, a plain CI runner is not. Production workers are containers, so
    the copy is necessary - and because every member is STORED it is a byte
    range read rather than an inflate.

    What is asserted here is only that the copy works, since that has to hold
    everywhere. Whether the in-place open happens to succeed is reported for
    information and deliberately not asserted: an earlier version of this test
    asserted it must fail, passed in a container and failed in CI.
    """
    root = sen3_root(str(s3_synergy_window))
    local = extract_member(root, "NDVI.nc", tmp_path)
    assert local.endswith("NDVI.nc")
    assert Path(local).exists()

    dataset = open_sen3_variable(local, "NDVI")
    assert dataset.RasterXSize == 384
    # The packing survives the copy, which is the other half of the point: the
    # HDF5 driver can open the member in place but loses scale and nodata.
    band = dataset.GetRasterBand(1)
    assert band.GetScale() == pytest.approx(0.004, rel=1e-3)
    assert band.GetNoDataValue() == 255
    dataset = None


def test_a_missing_member_names_what_the_product_has(s3_synergy_window, tmp_path):
    root = sen3_root(str(s3_synergy_window))
    with pytest.raises(ValueError, match="is not in this product"):
        extract_member(root, "chlorophyll.nc", tmp_path)


# --------------------------------------------------------------------------- #
# The manifest
# --------------------------------------------------------------------------- #
def test_the_manifest_gives_type_baseline_times_and_footprint(s3_synergy_window):
    manifest = read_sen3_manifest(sen3_root(str(s3_synergy_window)))
    assert manifest["productType"].startswith("SY_2_VG1")
    assert manifest["baseline"] == "002"
    assert manifest["time"]["start"].startswith("2025-07-20T00:00")
    assert manifest["time"]["end"].startswith("2025-07-20T23:59")

    footprint = manifest["footprint"]
    assert footprint["type"] == "Polygon"
    ring = footprint["coordinates"][0]
    assert ring[0] == ring[-1]
    # Longitude first, as GeoJSON wants, though the manifest writes it the
    # other way round.
    for longitude, latitude in ring:
        assert -180.0 <= longitude <= 180.0
        assert 55.0 <= latitude <= 70.0


def test_the_baseline_comes_from_the_manifest_not_the_name():
    """A 2023 product labelled ``_NT_003`` carried the older variable names, so
    guessing the baseline from the filename reads variables that are not there.
    """
    from pysent._sen3 import _baseline_of

    assert _baseline_of({"baselineCollection": "4"}, "/x/S3A_OL_2_WFR_..._002.SEN3") == "004"
    # Only with nothing in the manifest does the name get a say.
    assert _baseline_of({"productName": "S3A_OL_2_WFR____x_003.SEN3"}, "/x") == "003"
    assert _baseline_of({}, "/x/S3A_OL_2_WFR____x_004.SEN3") == "004"


def test_a_product_without_a_manifest_is_refused(tmp_path):
    path = tmp_path / "nomanifest.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("X.SEN3/NDVI.nc", b"stub")
    with pytest.raises(ValueError, match="does not look like a SEN3 product"):
        read_sen3_manifest(sen3_root(str(path)))


# --------------------------------------------------------------------------- #
# Packing and flags
# --------------------------------------------------------------------------- #
def test_the_byte_packing_is_applied(s3_synergy_window, tmp_path):
    """NDVI is stored as ``raw * 0.004 - 0.08`` in a byte, with 255 as fill."""
    root = sen3_root(str(s3_synergy_window))
    dataset = open_sen3_variable(extract_member(root, "NDVI.nc", tmp_path), "NDVI")
    band = dataset.GetRasterBand(1)
    raw = band.ReadAsArray()
    values = read_packed(band)
    dataset = None

    assert values.dtype == np.float32
    finite = np.isfinite(values)
    assert np.allclose(values[finite], raw[finite] * 0.004 - 0.08, atol=1e-6)
    # Fill became NaN rather than 0.92, which is what 255 would scale to.
    assert np.all(~finite == (raw == 255))
    assert -1.0 <= np.nanmin(values) <= np.nanmax(values) <= 1.0


def test_decimating_reads_a_smaller_array(s3_synergy_window, tmp_path):
    root = sen3_root(str(s3_synergy_window))
    dataset = open_sen3_variable(extract_member(root, "NDVI.nc", tmp_path), "NDVI")
    band = dataset.GetRasterBand(1)
    full = read_packed(band)
    quarter = read_packed(band, decimate=4)
    dataset = None
    assert quarter.shape == (full.shape[0] // 4, full.shape[1] // 4)


def test_the_log10_unpacking_undoes_a_log_packed_variable():
    """The OLCI water products store log10 of the value.

    Forgetting the final power of ten maps chlorophyll onto about -3..2 instead
    of 0.001..100 - a plausible looking picture of the wrong quantity. Phases 4
    and 5 depend on this, so it is held down here.
    """
    from pysent._sen3 import read_packed as unpack

    class Band:
        XSize, YSize = 2, 1

        def ReadAsArray(self, buf_xsize=None, buf_ysize=None):
            return np.array([[0, 100]], dtype=np.uint8)

        def GetNoDataValue(self):
            return 255.0

        def GetScale(self):
            return 0.01574803

        def GetOffset(self):
            return -2.0

    values = unpack(Band(), log10=True)
    assert values[0, 0] == pytest.approx(10 ** -2.0, rel=1e-5)
    assert values[0, 1] == pytest.approx(10 ** (100 * 0.01574803 - 2.0), rel=1e-5)
    assert values.min() > 0  # a concentration, never negative


def test_the_flag_table_is_read_from_the_file(s3_synergy_window, tmp_path):
    """Sentinel-3 mixes two CF conventions in one variable.

    Single bits have ``mask == value`` (``land`` is bit 8). A small enumeration
    shares a mask: the status word puts clear, unfilled, uncertain and cloud in
    the low two bits as values 0-3 of mask 3. Both have to come back, because a
    caller selects by name.
    """
    root = sen3_root(str(s3_synergy_window))
    dataset = open_sen3_variable(extract_member(root, "sm.nc", tmp_path), "SM")
    table = read_flag_table(dataset.GetRasterBand(1))
    dataset = None

    rows = {row["name"]: row for row in table}
    assert rows["land"] == {"name": "land", "mask": 8, "value": 8}
    assert rows["clear"] == {"name": "clear", "mask": 3, "value": 0}
    assert rows["cloud"] == {"name": "cloud", "mask": 3, "value": 3}
    assert rows["uncertain"] == {"name": "uncertain", "mask": 3, "value": 2}


def test_an_enumerated_flag_is_not_confused_with_a_bit():
    """``clear`` is value 0 of mask 3. Treated as a bit test it would match
    nothing, and the mask would silently keep everything."""
    table = [
        {"name": "clear", "mask": 3, "value": 0},
        {"name": "cloud", "mask": 3, "value": 3},
        {"name": "land", "mask": 8, "value": 8},
    ]
    flags = np.array([[0, 1, 2, 3, 8, 11]], dtype=np.uint16)
    assert flag_selection(flags, table, ["clear"])[0].tolist() == [[True, False, False, False, True, False]]
    assert flag_selection(flags, table, ["cloud"])[0].tolist() == [[False, False, False, True, False, True]]
    assert flag_selection(flags, table, ["land"])[0].tolist() == [[False, False, False, False, True, True]]


def test_an_unknown_flag_name_is_refused_rather_than_ignored():
    """A mask that does nothing looks exactly like unusually good data."""
    table = [{"name": "clear", "mask": 3, "value": 0}]
    flags = np.zeros((2, 2), dtype=np.uint16)
    with pytest.raises(ValueError, match="not defined in this product"):
        flag_selection(flags, table, ["cloud_shadow"])


def test_a_variable_with_no_flag_metadata_gives_an_empty_table():
    class Band:
        def GetMetadata(self):
            return {"units": "1"}

    assert read_flag_table(Band()) == []


# --------------------------------------------------------------------------- #
# The grid
# --------------------------------------------------------------------------- #
def test_the_output_carries_an_assigned_wgs84_crs(s3_synergy_window, tmp_path):
    """The files declare an unnamed datum, so the CRS is assigned, not copied."""
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window),
        output_dir=tmp_path,
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    dataset = gdal.Open(result["path"])
    projection = dataset.GetProjection()
    dataset = None
    assert "4326" in projection
    assert "unnamed" not in projection.lower()
    assert result["epsg"] == 4326


def test_the_native_grid_is_one_hundred_and_twelfth_of_a_degree(s3_synergy_window, tmp_path):
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window),
        output_dir=tmp_path,
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    assert result["resolution"] == pytest.approx(1.0 / 112.0, rel=1e-6)


def test_decimating_coarsens_the_grid_but_keeps_the_origin(s3_synergy_window, tmp_path):
    full = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window), output_dir=tmp_path / "full",
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    coarse = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window), output_dir=tmp_path / "coarse",
        processing_options={"decimate": 4, "styled": False, "thumbnail": False},
    )[0]
    assert coarse["resolution"] == pytest.approx(full["resolution"] * 4, rel=1e-6)
    assert coarse["width"] == full["width"] // 4
    assert coarse["transform"][0] == pytest.approx(full["transform"][0])
    assert coarse["transform"][3] == pytest.approx(full["transform"][3])


def test_the_quicklook_preset_decimates(s3_synergy_window, tmp_path):
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window), output_dir=tmp_path,
        processing_options={"preset": "quicklook", "styled": False, "thumbnail": False},
    )[0]
    assert result["decimate"] == 8
    assert result["width"] == 384 // 8


def test_an_output_too_large_for_the_queue_is_refused(s3_synergy_window, tmp_path):
    with pytest.raises(ValueError, match="over the .* limit"):
        process_sentinel_s3_safe(
            input_dataset=str(s3_synergy_window), output_dir=tmp_path,
            processing_options={"max_output_pixels": 1000},
        )


# --------------------------------------------------------------------------- #
# The products
# --------------------------------------------------------------------------- #
def test_ndvi_is_plausible_on_a_real_window(s3_synergy_window, tmp_path):
    """Southern Norway in July: mostly forest, so a high median NDVI."""
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window), output_dir=tmp_path,
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    statistics = result["value_stats"]
    assert -0.08 <= statistics["min"] <= statistics["max"] <= 1.0
    assert 0.3 < statistics["p50"] < 0.95
    low, high = S3_PRODUCTS["ndvi"]["value_range"]
    assert low <= statistics["p50"] <= high


def test_only_clear_pixels_survive_the_default_mask(s3_synergy_window, tmp_path):
    """Keeping more than "clear" has to keep more pixels, or the mask is inert."""
    strict = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window), output_dir=tmp_path / "strict",
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    unmasked = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window), output_dir=tmp_path / "none",
        products={"ndvi": {"mask_flags": "none"}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    assert strict["valid_fraction"] < unmasked["valid_fraction"]
    assert strict["mask_flags"] == ["clear"]
    assert unmasked["mask_flags"] == []


def test_the_result_reports_the_flags_the_product_defines(s3_synergy_window, tmp_path):
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window), output_dir=tmp_path,
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    assert "cloud" in result["available_flags"]
    assert "land" in result["available_flags"]


def test_three_files_are_written_and_the_float_one_keeps_physical_values(s3_synergy_window, tmp_path):
    result = process_sentinel_s3_safe(input_dataset=str(s3_synergy_window), output_dir=tmp_path)[0]
    for key in ("path", "styled_path", "thumbnail_path"):
        assert key in result

    dataset = gdal.Open(result["path"])
    band = dataset.GetRasterBand(1)
    assert band.DataType == gdal.GDT_Float32
    assert np.isnan(band.GetNoDataValue())
    dataset = None

    styled = gdal.Open(result["styled_path"])
    styled_band = styled.GetRasterBand(1)
    assert styled_band.DataType == gdal.GDT_Byte
    assert styled_band.GetRasterColorTable() is not None
    styled = None


# --------------------------------------------------------------------------- #
# Why a pixel is empty
# --------------------------------------------------------------------------- #
def test_the_reject_reason_band_explains_each_empty_pixel(s3_synergy_window, tmp_path):
    """So a point query can answer "masked: cloud" rather than "no data"."""
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window), output_dir=tmp_path,
        processing_options={"reject_reason": True, "styled": False, "thumbnail": False},
    )[0]
    dataset = gdal.Open(result["path"])
    assert dataset.RasterCount == 2
    values = dataset.GetRasterBand(1).ReadAsArray()
    codes = dataset.GetRasterBand(2).ReadAsArray().astype(int)
    assert dataset.GetRasterBand(2).GetDescription() == "reject_reason"
    dataset = None

    # Every valid pixel is coded valid, and no empty pixel is.
    assert np.all(codes[np.isfinite(values)] == REJECT_REASONS["valid"])
    assert np.all(codes[~np.isfinite(values)] != REJECT_REASONS["valid"])
    # The window has both cloud and fill in it.
    assert set(np.unique(codes)) & {REJECT_REASONS["cloud"], REJECT_REASONS["fill"]}
    assert result["query_source"]["reject_reason_band"] == 2


def test_without_the_option_there_is_one_band_and_no_reason_map(s3_synergy_window, tmp_path):
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window), output_dir=tmp_path,
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    dataset = gdal.Open(result["path"])
    assert dataset.RasterCount == 1
    dataset = None
    assert result["query_source"]["reject_reason_band"] is None


def test_a_tile_with_nothing_left_after_masking_raises(s3_synergy_window, tmp_path):
    """Normal for a winter tile, so it gets the empty-scene error."""
    with pytest.raises(EmptySceneError, match="nothing to map"):
        process_sentinel_s3_safe(
            input_dataset=str(s3_synergy_window), output_dir=tmp_path,
            # "unfilled" marks a pixel that holds no value, so NDVI is fill
            # there by definition and nothing can survive this mask. Certain by
            # meaning rather than by what this window happens to contain.
            products={"ndvi": {"mask_flags": ["unfilled"]}},
        )


def test_an_empty_tile_can_be_written_instead_of_raising(s3_synergy_window, tmp_path):
    results = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window), output_dir=tmp_path,
        products={"ndvi": {"mask_flags": ["unfilled"]}},
        processing_options={"on_empty": "write", "styled": False, "thumbnail": False},
    )
    assert results[0]["valid_fraction"] == 0.0


# --------------------------------------------------------------------------- #
# The request, provenance and names
# --------------------------------------------------------------------------- #
def test_the_defaults_come_from_the_product_table():
    normalized = normalize_sentinel_s3_product_map(None)
    assert normalized["ndvi"]["mask_flags"] == ["clear"]
    assert normalized["ndvi"]["value_range"] == (-0.1, 0.9)
    assert normalized["ndvi"]["colormap"] == "brown_green"


@pytest.mark.parametrize(
    "products, message",
    [
        ({"chlorophyll": {}}, "not a Sentinel-3 product"),
        ({"ndvi": {"decimate": 0}}, "1 or more"),
        ({"ndvi": {"decimate": "half"}}, "whole number"),
        ({"ndvi": {"value_range": (1.0, 0.0)}}, "low < high"),
        ({"ndvi": {"colormap": "chartreuse"}}, "not a colormap"),
        ({"ndvi": {"mask_flags": 7}}, "mask_flags must be"),
        ({"ndvi": {"nonsense": 1}}, "unknown option"),
        ({"ndvi": []}, "must be a mapping"),
        ("ndvi", "must be a mapping"),
    ],
)
def test_a_bad_request_fails_before_anything_is_opened(products, message):
    with pytest.raises(ValueError, match=message):
        normalize_sentinel_s3_product_map(products)


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("S3B_SY_2_VG1____20250720T000000_x_EUROPE____________ESA_O_NT_002.zip", ("ndvi",)),
        ("S3A_SY_2_V10____20250621T000000_x_EUROPE____________PS1_O_NT_002.zip", ("ndvi",)),
        ("unrecognisable.zip", ("ndvi",)),
    ],
)
def test_a_product_says_which_derived_products_it_supports(filename, expected):
    assert default_sentinel_s3_products(filename) == expected


def test_every_ready_type_has_products_and_every_product_has_a_type():
    from pysent.profiles import SENTINEL_PRODUCT_TYPES

    ready = {
        product_type
        for (family, product_type), (_, status, _) in SENTINEL_PRODUCT_TYPES.items()
        if family == "S3" and status == "ready"
    }
    assert ready == set(S3_TYPE_PRODUCTS)
    named = {name for names in S3_TYPE_PRODUCTS.values() for name in names}
    assert named == set(S3_PRODUCTS)


def test_the_provenance_records_the_mask_and_the_baseline(s3_synergy_window, tmp_path):
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window), output_dir=tmp_path,
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    stamped = result["provenance"]
    assert stamped["PYSENT_MASK_FLAGS"] == "clear"
    assert stamped["PYSENT_BASELINE"] == "002"
    assert stamped["PYSENT_PRODUCT_TYPE"].startswith("SY_2_VG1")
    assert stamped["PYSENT_DECIMATE"] == "1"

    dataset = gdal.Open(result["path"])
    on_disk = dataset.GetMetadata()
    dataset = None
    assert on_disk["PYSENT_MASK_FLAGS"] == "clear"
    assert "PYSENT_VERSION" in on_disk


def test_the_output_name_is_derived_from_the_product_name():
    name = build_sentinel_s3_output_filename(
        "/archive/S3B_SY_2_VG1____20250720T000000_x_EUROPE____________ESA_O_NT_002.zip", "ndvi"
    )
    assert name == "S3B_SY_2_VG1____20250720T000000_x_EUROPE____________ESA_O_NT_002_ndvi.tif"


def test_a_re_run_reproduces_the_pixels_and_the_tags(s3_synergy_window, tmp_path):
    options = {"styled": False, "thumbnail": False}
    first = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window), output_dir=tmp_path / "a", processing_options=options
    )[0]
    second = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window), output_dir=tmp_path / "b", processing_options=options
    )[0]
    one, two = gdal.Open(first["path"]), gdal.Open(second["path"])
    left = one.GetRasterBand(1).ReadAsArray()
    right = two.GetRasterBand(1).ReadAsArray()
    tags_one, tags_two = one.GetMetadata(), two.GetMetadata()
    one = two = None
    assert np.array_equal(left, right, equal_nan=True)
    assert tags_one == tags_two


# --------------------------------------------------------------------------- #
# Options that were accepted and then ignored (review-study items R4, R6)
# --------------------------------------------------------------------------- #
def test_a_requested_browse_mask_reaches_the_product():
    """Regression: ``browse_reject_flags`` was accepted and silently dropped.

    It is in ``_PRODUCT_OPTIONS``, so a request carrying it validated, but the
    normalized product took its value only from the defaults. On ``main`` all
    three of these come back as the default cloud margin.
    """
    default = normalize_sentinel_s3_product_map({"chl": {}})["chl"]["browse_reject_flags"]
    assert default, "this test needs a product whose browse mask is not already empty"

    off = normalize_sentinel_s3_product_map({"chl": {"browse_reject_flags": []}})["chl"]
    assert list(off["browse_reject_flags"]) == []

    none = normalize_sentinel_s3_product_map({"chl": {"browse_reject_flags": "none"}})["chl"]
    assert list(none["browse_reject_flags"]) == []

    picked = normalize_sentinel_s3_product_map(
        {"chl": {"browse_reject_flags": ["CLOUD_AMBIGUOUS"]}}
    )["chl"]
    assert list(picked["browse_reject_flags"]) == ["CLOUD_AMBIGUOUS"]

    # And "default" still means the table's own choice.
    kept = normalize_sentinel_s3_product_map({"chl": {"browse_reject_flags": "default"}})["chl"]
    assert list(kept["browse_reject_flags"]) == list(default)


def test_a_bad_browse_mask_request_is_refused():
    with pytest.raises(ValueError, match="browse_reject_flags must be"):
        normalize_sentinel_s3_product_map({"chl": {"browse_reject_flags": 7}})


def test_asking_for_a_browse_mask_makes_the_flag_names_strict():
    """A name the caller typed is strict; a default one may be absent in an
    older baseline and is then skipped and reported.
    """
    asked = normalize_sentinel_s3_product_map({"chl": {"browse_reject_flags": ["CLOUD"]}})["chl"]
    assert asked["flags_are_default"] is False
