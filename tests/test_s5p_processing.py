"""Sentinel-5P Level-2 processing: masking, gridding, outputs and refusals.

The real-data cases run against ``tests/data/s5p_no2_window.nc``, 60 scanlines
cut out of a genuine NO2 granule with its packing intact. The geometry cases use
synthetic coordinate arrays, because a committed granule cannot cover a pole, an
antimeridian crossing and a whole orbit at once.
"""
import numpy as np
import pytest

pytest.importorskip("osgeo")

from osgeo import gdal  # noqa: E402

from pysent._swath import (  # noqa: E402
    EPSG_LATLON,
    EPSG_POLAR_NORTH,
    EPSG_POLAR_SOUTH,
    choose_grid,
    contains_pole,
    crosses_antimeridian,
    footprint_polygon,
    grid_bounds,
    open_root,
    read_array,
    read_raw_array,
    valid_fraction,
    value_statistics,
)
from pysent.errors import EmptySceneError  # noqa: E402
from pysent.s5p import (  # noqa: E402
    S5P_PRODUCTS,
    build_sentinel_s5p_output_filename,
    normalize_sentinel_s5p_product_map,
    process_sentinel_s5p_netcdf,
)

NO2 = S5P_PRODUCTS["no2"]["variable"]


# --------------------------------------------------------------------------- #
# Reading and unpacking
# --------------------------------------------------------------------------- #
def test_quality_values_are_unpacked_and_the_raw_bytes_are_available(s5p_no2_window):
    """The quality band is a byte of whole percent with a 0.01 scale.

    The multidimensional reader hands back raw values and exposes the scale as
    a property rather than an attribute, so a caller that misses it compares a
    0-100 byte against a 0-1 threshold and keeps everything.
    """
    root = open_root(str(s5p_no2_window))
    scaled = read_array(root, "/PRODUCT/qa_value")
    raw = read_raw_array(root, "/PRODUCT/qa_value")

    assert raw.dtype == np.uint8
    assert raw.max() > 1  # whole percent, not a fraction
    finite = np.isfinite(scaled)
    assert np.allclose(scaled[finite], raw[finite] * 0.01, atol=1e-6)
    assert np.nanmax(scaled) <= 1.0


def test_the_fill_value_becomes_nan_rather_than_a_huge_number(s5p_no2_window):
    root = open_root(str(s5p_no2_window))
    values = read_array(root, NO2)
    assert values.dtype == np.float32
    # The product's fill is 9.96921e36; nothing that large may survive.
    assert not np.any(np.abs(values[np.isfinite(values)]) > 1e30)


def test_real_no2_values_are_physically_plausible(s5p_no2_window):
    """A tropospheric NO2 column is a small number of mol per square metre.

    Negative values are genuine: they are instrument noise at low columns, and
    the decision was to keep them in the data and clip them in the style.
    """
    root = open_root(str(s5p_no2_window))
    values = read_array(root, NO2)
    statistics = value_statistics(values)
    assert 0.0 < statistics["p50"] < 1e-4
    assert statistics["min"] < 0.0
    assert statistics["p99"] < 1e-3


# --------------------------------------------------------------------------- #
# Masking
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("qa_min", [0.5, 0.75])
def test_the_mask_matches_an_independent_recomputation(s5p_no2_window, tmp_path, qa_min):
    """The output's valid pixels are exactly those an independent mask keeps.

    Computed here from the raw byte against the raw threshold, which is the
    comparison the pipeline has to make: the stored values are whole percent, so
    a float comparison against 0.75 is not the same question.
    """
    from pysent._swath import warp_swath

    root = open_root(str(s5p_no2_window))
    values = read_array(root, NO2)
    lat = read_array(root, "/PRODUCT/latitude")
    lon = read_array(root, "/PRODUCT/longitude")
    raw_quality = read_raw_array(root, "/PRODUCT/qa_value")

    result = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window),
        output_dir=tmp_path,
        products={"no2": {"qa_min": qa_min, "resampling": "near"}},
        processing_options={"styled": False, "thumbnail": False, "overview_factors": []},
    )[0]

    # Mask the source here, independently, and put it on the same grid the same
    # way. Counting valid cells instead would prove nothing: a 0.05 deg cell is
    # finer than a Sentinel-5P pixel across track, so one source pixel
    # legitimately fills several output cells.
    reference = np.where(raw_quality > round(qa_min * 100), values, np.nan).astype(np.float32)
    reference_path = tmp_path / f"reference_{qa_min}.tif"
    warp_swath(
        values=reference,
        lat=lat,
        lon=lon,
        output_path=str(reference_path),
        epsg=result["epsg"],
        resolution=result["resolution"],
        bounds=tuple(result["bounds"]),
        resampling="near",
        scratch=tmp_path,
    )

    # Hold both datasets: a band whose dataset has been collected is invalid.
    produced_dataset = gdal.Open(result["path"])
    expected_dataset = gdal.Open(str(reference_path))
    produced = produced_dataset.GetRasterBand(1).ReadAsArray()
    expected = expected_dataset.GetRasterBand(1).ReadAsArray()
    produced_dataset = expected_dataset = None
    assert np.array_equal(np.isfinite(produced), np.isfinite(expected))
    assert np.allclose(produced[np.isfinite(produced)], expected[np.isfinite(expected)], rtol=1e-6)
    assert result["qa_min"] == qa_min


def test_a_higher_threshold_keeps_strictly_fewer_pixels(s5p_no2_window, tmp_path):
    """The audit measured 80.7 % kept at 0.5 against 25.6 % at 0.75.

    The exact fractions belong to the whole granule; on a 60-scanline window
    only the ordering is guaranteed, which is the property that matters: a
    stricter threshold must not somehow keep more.
    """
    counts = {}
    for qa_min in (0.5, 0.75):
        result = process_sentinel_s5p_netcdf(
            input_dataset=str(s5p_no2_window),
            output_dir=tmp_path / f"qa{qa_min}",
            products={"no2": {"qa_min": qa_min}},
            processing_options={"styled": False, "thumbnail": False},
        )[0]
        counts[qa_min] = result["valid_fraction"]
    assert counts[0.75] < counts[0.5]


def test_masked_pixels_are_not_averaged_into_their_neighbours(tmp_path):
    """A masked pixel must stay masked through the warp.

    If the mask were applied as a zero rather than as NaN, ``average`` would mix
    it into neighbouring cells and halve real values along every mask edge.
    """
    from pysent._swath import warp_swath

    rows, columns = 20, 20
    lat = np.repeat(np.linspace(60.0, 61.0, rows)[:, None], columns, axis=1)
    lon = np.repeat(np.linspace(10.0, 11.0, columns)[None, :], rows, axis=0)
    values = np.full((rows, columns), 5.0, dtype=np.float32)
    values[:, : columns // 2] = np.nan  # mask the western half

    output = tmp_path / "masked.tif"
    warp_swath(
        values=values,
        lat=lat,
        lon=lon,
        output_path=str(output),
        epsg=EPSG_LATLON,
        resolution=0.05,
        resampling="average",
        scratch=tmp_path,
    )
    dataset = gdal.Open(str(output))
    grid = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None

    surviving = grid[np.isfinite(grid)]
    assert surviving.size > 0
    # Every value that came through is the original, not a blend with nodata.
    assert np.allclose(surviving, 5.0, atol=1e-5)


# --------------------------------------------------------------------------- #
# Grid policy
# --------------------------------------------------------------------------- #
def _swath(lat_range, lon_range, shape=(40, 40)):
    lat = np.repeat(np.linspace(*lat_range, shape[0])[:, None], shape[1], axis=1)
    lon = np.repeat(np.linspace(*lon_range, shape[1])[None, :], shape[0], axis=0)
    return lat, lon


def test_a_mid_latitude_swath_goes_on_a_latlon_grid():
    lat, lon = _swath((40.0, 65.0), (-2.0, 46.0))
    assert choose_grid(lat, lon) == (EPSG_LATLON, 0.05)


def test_a_pole_crossing_swath_goes_on_a_polar_grid():
    """In plain lat/lon this granule left 5.5 % holes and smeared pixels.

    The documented reason the policy exists at all: see the antimeridian test
    below for the failure it avoids.
    """
    lat, lon = _swath((70.0, 89.0), (-180.0, 180.0))
    epsg, resolution = choose_grid(lat, lon)
    assert epsg == EPSG_POLAR_NORTH
    assert resolution == 5000.0


def test_a_southern_polar_swath_goes_on_the_southern_grid():
    lat, lon = _swath((-89.0, -70.0), (-180.0, 180.0))
    assert choose_grid(lat, lon)[0] == EPSG_POLAR_SOUTH


def test_a_whole_orbit_stays_in_latlon_because_no_polar_grid_holds_it():
    """A polar stereographic grid cannot represent a pole-to-pole orbit.

    So the policy keeps lat/lon there and accepts the known fringe artefacts
    along the polar cap, rather than producing something worse.
    """
    lat, lon = _swath((-89.0, 89.0), (-180.0, 180.0))
    assert choose_grid(lat, lon)[0] == EPSG_LATLON


def test_an_explicit_grid_overrides_the_policy():
    lat, lon = _swath((70.0, 89.0), (-180.0, 180.0))
    assert choose_grid(lat, lon, epsg=4326, resolution=0.1) == (4326, 0.1)
    # An explicit projection still gets a sensible default resolution.
    assert choose_grid(lat, lon, epsg=32661)[1] == 5000.0


def test_an_antimeridian_crossing_is_detected_from_the_longitude_jump():
    lat, lon = _swath((60.0, 70.0), (170.0, 190.0))
    lon = ((lon + 180.0) % 360.0) - 180.0  # wrap into -180..180, as a product does
    assert crosses_antimeridian(lon)
    assert not crosses_antimeridian(_swath((60.0, 70.0), (10.0, 30.0))[1])


def test_the_pole_test_uses_both_hemispheres():
    assert contains_pole(_swath((70.0, 80.0), (0.0, 10.0))[0])
    assert contains_pole(_swath((-80.0, -70.0), (0.0, 10.0))[0])
    assert not contains_pole(_swath((40.0, 65.0), (0.0, 10.0))[0])


def test_latlon_bounds_span_the_world_when_the_swath_wraps():
    """Anything narrower would cut the swath in half at the seam."""
    lat, lon = _swath((60.0, 70.0), (170.0, 190.0))
    lon = ((lon + 180.0) % 360.0) - 180.0
    bounds = grid_bounds(lat, lon, EPSG_LATLON, 0.05)
    assert bounds[0] == -180.0 and bounds[2] == 180.0


def test_bounds_are_snapped_to_whole_cells(s5p_no2_window):
    """So the same product on the same grid always lands on the same edges."""
    root = open_root(str(s5p_no2_window))
    lat = read_array(root, "/PRODUCT/latitude")
    lon = read_array(root, "/PRODUCT/longitude")
    bounds = grid_bounds(lat, lon, EPSG_LATLON, 0.05)
    for edge in bounds:
        assert abs(edge / 0.05 - round(edge / 0.05)) < 1e-9


# --------------------------------------------------------------------------- #
# Footprint
# --------------------------------------------------------------------------- #
def test_the_footprint_is_a_closed_polygon_around_the_swath(s5p_no2_window):
    root = open_root(str(s5p_no2_window))
    lat = read_array(root, "/PRODUCT/latitude")
    lon = read_array(root, "/PRODUCT/longitude")
    footprint = footprint_polygon(lat, lon)

    assert footprint["type"] == "Polygon"
    ring = footprint["coordinates"][0]
    assert ring[0] == ring[-1]
    assert len(ring) > 4
    # It is the swath outline, not the bounding box, so it must sit inside it.
    xs = [point[0] for point in ring]
    ys = [point[1] for point in ring]
    assert min(xs) >= float(np.nanmin(lon)) - 1e-6
    assert max(ys) <= float(np.nanmax(lat)) + 1e-6


def test_the_footprint_of_a_wrapping_swath_is_split_at_the_seam():
    """Otherwise the polygon is drawn straight across the map."""
    lat, lon = _swath((60.0, 70.0), (170.0, 190.0))
    lon = ((lon + 180.0) % 360.0) - 180.0
    footprint = footprint_polygon(lat, lon)
    assert footprint["type"] == "MultiPolygon"
    for part in footprint["coordinates"]:
        for x, _ in part[0]:
            assert -180.0 <= x <= 180.0


def test_the_output_bounds_contain_the_valid_source_pixels(s5p_no2_window, tmp_path):
    root = open_root(str(s5p_no2_window))
    lat = read_array(root, "/PRODUCT/latitude")
    lon = read_array(root, "/PRODUCT/longitude")
    result = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window),
        output_dir=tmp_path,
        products={"no2": {}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    min_x, min_y, max_x, max_y = result["bounds"]
    assert min_x <= float(np.nanmin(lon)) and max_x >= float(np.nanmax(lon))
    assert min_y <= float(np.nanmin(lat)) and max_y >= float(np.nanmax(lat))


# --------------------------------------------------------------------------- #
# The three outputs
# --------------------------------------------------------------------------- #
def test_three_files_are_written_and_the_float_one_keeps_physical_units(s5p_no2_window, tmp_path):
    result = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window), output_dir=tmp_path, products={"no2": {}}
    )[0]

    float_path = tmp_path / result["output_file"]
    styled_path = tmp_path / f"{float_path.stem}_styled.tif"
    thumbnail_path = tmp_path / f"{float_path.stem}_thumb.png"
    assert float_path.exists() and styled_path.exists() and thumbnail_path.exists()

    dataset = gdal.Open(str(float_path))
    band = dataset.GetRasterBand(1)
    assert band.DataType == gdal.GDT_Float32
    assert np.isnan(band.GetNoDataValue())
    values = band.ReadAsArray()
    dataset = None
    # Physical units, not a stretched byte range.
    assert np.nanmax(values) < 1e-3


def test_the_styled_copy_is_paletted_with_zero_as_no_data(s5p_no2_window, tmp_path):
    """One paletted band rather than RGB: 1.7x smaller, and a reader can still
    recover which class a pixel fell in."""
    result = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window), output_dir=tmp_path, products={"no2": {}}
    )[0]
    dataset = gdal.Open(result["styled_path"])
    band = dataset.GetRasterBand(1)

    assert dataset.RasterCount == 1
    assert band.DataType == gdal.GDT_Byte
    assert band.GetNoDataValue() == 0
    table = band.GetRasterColorTable()
    assert table is not None
    assert table.GetColorEntry(0)[3] == 0  # no data is transparent
    assert table.GetColorEntry(255)[3] == 255

    styled = band.ReadAsArray()
    float_dataset = gdal.Open(result["path"])
    values = float_dataset.GetRasterBand(1).ReadAsArray()
    float_dataset = None
    dataset = None
    # Zero means no data and nothing else, so valid data starts at 1.
    assert np.all(styled[np.isfinite(values)] >= 1)
    assert np.all(styled[~np.isfinite(values)] == 0)


def test_the_styled_copy_has_the_same_grid_as_the_float_one(s5p_no2_window, tmp_path):
    result = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window), output_dir=tmp_path, products={"no2": {}}
    )[0]
    first, second = gdal.Open(result["path"]), gdal.Open(result["styled_path"])
    assert first.GetGeoTransform() == second.GetGeoTransform()
    assert first.GetProjection() == second.GetProjection()
    assert (first.RasterXSize, first.RasterYSize) == (second.RasterXSize, second.RasterYSize)
    first = second = None


def test_the_thumbnail_is_small_and_paletted(s5p_no2_window, tmp_path):
    result = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window), output_dir=tmp_path, products={"no2": {}}
    )[0]
    dataset = gdal.Open(result["thumbnail_path"])
    assert max(dataset.RasterXSize, dataset.RasterYSize) <= 256
    assert dataset.GetRasterBand(1).GetRasterColorTable() is not None
    dataset = None


def test_values_outside_the_colour_range_are_clipped_not_dropped():
    """A negative NO2 column is noise, not missing data: it must still be drawn."""
    from pysent.s5p import _styled_bytes

    values = np.array([[-5e-5, 0.0, 5e-5, 2e-4, np.nan]], dtype=np.float32)
    styled = _styled_bytes(values, (0.0, 1e-4))
    assert styled.tolist() == [[1, 1, 128, 255, 0]]


def test_outputs_can_be_turned_off(s5p_no2_window, tmp_path):
    result = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window),
        output_dir=tmp_path,
        products={"no2": {}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    assert "styled_path" not in result and "thumbnail_path" not in result
    assert list(tmp_path.glob("*.png")) == []


# --------------------------------------------------------------------------- #
# The result and its provenance
# --------------------------------------------------------------------------- #
def test_the_result_carries_what_a_service_needs(s5p_no2_window, tmp_path):
    result = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window), output_dir=tmp_path, products={"no2": {}}
    )[0]
    for key in (
        "path", "output_file", "epsg", "resolution", "bounds", "width", "height",
        "valid_fraction", "value_stats", "units", "qa_min", "style_hint",
        "query_source", "footprint", "provenance",
    ):
        assert key in result, key
    assert sorted(result["value_stats"]) == ["max", "min", "p01", "p50", "p99"]
    assert result["style_hint"]["value_range"] == [0.0, 1e-4]
    # The point query has to apply the same mask the map did.
    assert result["query_source"]["qa_min"] == result["qa_min"]
    assert result["query_source"]["variable"] == NO2
    assert result["query_source"]["scale"] == 0.01


def test_the_provenance_records_the_threshold_that_made_the_pixels(s5p_no2_window, tmp_path):
    result = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window),
        output_dir=tmp_path,
        products={"no2": {"qa_min": 0.6}},
    )[0]
    assert result["provenance"]["PYSENT_QA_MIN"] == "0.60"

    dataset = gdal.Open(result["path"])
    stamped = dataset.GetMetadata()
    dataset = None
    assert stamped["PYSENT_QA_MIN"] == "0.60"
    assert stamped["PYSENT_VARIABLE"] == NO2
    assert stamped["PYSENT_GRID_EPSG"] == "4326"
    assert "PYSENT_VERSION" in stamped


def test_the_file_name_carries_the_threshold(s5p_no2_window, tmp_path):
    """Two requests differing only in qa_min must not overwrite each other."""
    first = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window), output_dir=tmp_path,
        products={"no2": {"qa_min": 0.5}}, processing_options={"styled": False, "thumbnail": False},
    )[0]
    second = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window), output_dir=tmp_path,
        products={"no2": {"qa_min": 0.75}}, processing_options={"styled": False, "thumbnail": False},
    )[0]
    assert first["output_file"] != second["output_file"]
    assert "qa50" in first["output_file"] and "qa75" in second["output_file"]


def test_a_re_run_reproduces_the_pixels_and_the_tags(s5p_no2_window, tmp_path):
    """Same input and options, byte-identical result."""
    options = {"styled": False, "thumbnail": False}
    first = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window), output_dir=tmp_path / "a",
        products={"no2": {}}, processing_options=options,
    )[0]
    second = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window), output_dir=tmp_path / "b",
        products={"no2": {}}, processing_options=options,
    )[0]

    one, two = gdal.Open(first["path"]), gdal.Open(second["path"])
    left = one.GetRasterBand(1).ReadAsArray()
    right = two.GetRasterBand(1).ReadAsArray()
    tags_one, tags_two = one.GetMetadata(), two.GetMetadata()
    one = two = None

    assert np.array_equal(left, right, equal_nan=True)
    assert tags_one == tags_two


def test_a_failed_run_leaves_no_file_under_the_final_name(s5p_no2_window, tmp_path, monkeypatch):
    """A half-written map that looks finished is worse than no map."""
    import pysent.s5p as module

    def explode(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(module, "_finish_float_product", explode)
    with pytest.raises(RuntimeError, match="disk full"):
        process_sentinel_s5p_netcdf(
            input_dataset=str(s5p_no2_window), output_dir=tmp_path, products={"no2": {}}
        )
    assert list(tmp_path.glob("*.tif")) == []


# --------------------------------------------------------------------------- #
# Empty granules
# --------------------------------------------------------------------------- #
def test_a_granule_with_nothing_above_the_threshold_raises(s5p_no2_window, tmp_path):
    """Empty is the normal outcome over the winter pole, so it gets its own error."""
    with pytest.raises(EmptySceneError, match="nothing to map"):
        process_sentinel_s5p_netcdf(
            input_dataset=str(s5p_no2_window),
            output_dir=tmp_path,
            products={"no2": {"qa_min": 1.0}},  # nothing exceeds 100
        )


def test_the_empty_message_says_how_much_of_the_granule_was_fill(s5p_no2_window, tmp_path):
    with pytest.raises(EmptySceneError, match=r"% fill before masking"):
        process_sentinel_s5p_netcdf(
            input_dataset=str(s5p_no2_window), output_dir=tmp_path,
            products={"no2": {"qa_min": 1.0}},
        )


def test_an_empty_granule_can_be_written_instead_of_raising(s5p_no2_window, tmp_path):
    results = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window),
        output_dir=tmp_path,
        products={"no2": {"qa_min": 1.0}},
        processing_options={"on_empty": "write", "styled": False, "thumbnail": False},
    )
    assert results[0]["valid_fraction"] == 0.0


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #
def test_the_defaults_come_from_the_product_table():
    normalized = normalize_sentinel_s5p_product_map(None)
    assert normalized["no2"]["qa_min"] == 0.75
    assert normalized["no2"]["resampling"] == "average"
    assert normalized["no2"]["variable"] == NO2
    assert normalized["no2"]["unit"] == "native"


@pytest.mark.parametrize(
    "products, message",
    [
        ({"ozone": {}}, "not a Sentinel-5P product"),
        ({"no2": {"qa_min": 1.5}}, "between 0 and 1"),
        ({"no2": {"qa_min": "high"}}, "between 0 and 1"),
        ({"no2": {"resampling": "cubic"}}, "resampling must be one of"),
        ({"no2": {"unit": "furlongs"}}, "unit must be one of"),
        ({"no2": {"value_range": (1.0, 0.0)}}, "low < high"),
        ({"no2": {"nonsense": 1}}, "unknown option"),
        ({"no2": {"grid": {"epsg": 4326, "nope": 1}}}, "unknown key"),
        ({"no2": []}, "must be a mapping"),
        ("no2", "must be a mapping"),
    ],
)
def test_a_bad_request_fails_before_anything_is_opened(products, message):
    with pytest.raises(ValueError, match=message):
        normalize_sentinel_s5p_product_map(products)


def test_a_threshold_is_rounded_to_the_stored_precision():
    """The quality value is whole percent, so a hundredth is the finest step."""
    assert normalize_sentinel_s5p_product_map({"no2": {"qa_min": 0.7549}})["no2"]["qa_min"] == 0.75


def test_a_variable_may_be_given_bare_or_fully_qualified():
    bare = normalize_sentinel_s5p_product_map({"no2": {"variable": "nitrogendioxide_total_column"}})
    assert bare["no2"]["variable"] == "/PRODUCT/nitrogendioxide_total_column"
    full = normalize_sentinel_s5p_product_map({"no2": {"variable": "/PRODUCT/SUPPORT_DATA/x"}})
    assert full["no2"]["variable"] == "/PRODUCT/SUPPORT_DATA/x"


def test_a_variable_that_is_not_on_the_swath_grid_is_refused(s5p_no2_window, tmp_path):
    with pytest.raises(ValueError, match="not in this product"):
        process_sentinel_s5p_netcdf(
            input_dataset=str(s5p_no2_window),
            output_dir=tmp_path,
            products={"no2": {"variable": "not_a_variable"}},
        )


def test_units_are_converted_and_recorded(s5p_no2_window, tmp_path):
    native = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window), output_dir=tmp_path / "n", products={"no2": {}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    micro = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window), output_dir=tmp_path / "u",
        products={"no2": {"unit": "umol_m2"}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    assert micro["units"] == "umol m-2"
    assert micro["value_stats"]["p50"] == pytest.approx(native["value_stats"]["p50"] * 1e6, rel=1e-3)
    assert micro["provenance"]["PYSENT_UNITS"] == "umol m-2"


def test_an_output_too_large_for_an_interactive_request_is_refused(s5p_no2_window, tmp_path):
    with pytest.raises(ValueError, match="over the .* limit for a interactive request"):
        process_sentinel_s5p_netcdf(
            input_dataset=str(s5p_no2_window),
            output_dir=tmp_path,
            products={"no2": {"grid": {"res": 0.00005}}},
            processing_options={"context": "interactive"},
        )


def test_the_quicklook_preset_renders_coarser(s5p_no2_window, tmp_path):
    full = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window), output_dir=tmp_path / "f", products={"no2": {}},
        processing_options={"styled": False, "thumbnail": False},
    )[0]
    quick = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window), output_dir=tmp_path / "q", products={"no2": {}},
        processing_options={"preset": "quicklook", "styled": False, "thumbnail": False},
    )[0]
    assert quick["resolution"] > full["resolution"]
    assert quick["width"] < full["width"]


@pytest.mark.parametrize("context, expected", [("interactive", 40_000_000), ("bulk", 150_000_000)])
def test_the_pixel_cap_depends_on_the_queue(context, expected):
    """The caps come from a measurement, not a guess: peak memory is linear in
    output cells at about 39 MB per million, so 100 M cells would fill the
    interactive queue's whole 4 GB."""
    from pysent.s5p import _resolve_processing_settings

    assert _resolve_processing_settings({"context": context})["max_output_pixels"] == expected


def test_an_unknown_context_or_empty_policy_is_refused():
    from pysent.s5p import _resolve_processing_settings

    with pytest.raises(ValueError, match="context must be"):
        _resolve_processing_settings({"context": "whenever"})
    with pytest.raises(ValueError, match="on_empty must be"):
        _resolve_processing_settings({"on_empty": "shrug"})


def test_an_unknown_colormap_names_the_ones_that_exist():
    from pysent.s5p import _colour_table

    with pytest.raises(ValueError, match="viridis"):
        _colour_table("chartreuse")


def test_the_output_name_is_derived_from_the_input():
    name = build_sentinel_s5p_output_filename(
        "/archive/S5P_NRTI_L2__NO2____20261009T111407_x_46578_03_020901_y.nc", "no2", qa_min=0.75
    )
    assert name == "S5P_NRTI_L2__NO2____20261009T111407_x_46578_03_020901_y_no2_qa75.tif"


def test_mismatched_coordinate_shapes_are_refused(tmp_path):
    from pysent._swath import warp_swath

    with pytest.raises(ValueError, match="must have the same shape"):
        warp_swath(
            values=np.zeros((4, 4), dtype=np.float32),
            lat=np.zeros((4, 5)),
            lon=np.zeros((4, 5)),
            output_path=str(tmp_path / "x.tif"),
            epsg=4326,
            resolution=0.1,
            scratch=tmp_path,
        )


def test_statistics_of_an_all_nan_array_are_nan_rather_than_an_error():
    statistics = value_statistics(np.full((3, 3), np.nan, dtype=np.float32))
    assert all(np.isnan(value) for value in statistics.values())
    assert valid_fraction(np.full((3, 3), np.nan, dtype=np.float32)) == 0.0
