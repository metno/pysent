"""Multi-granule composites: the job shape.

The granules here are synthetic, and deliberately so: a composite's arithmetic
is about which cell a value lands in and how many values are behind it, so a
test needs to *know* the answer. The one test that uses a real granule is the
one that matters most - that a composite of a single input is the
single-granule product, which is what keeps the two from drifting apart.
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("osgeo")

from osgeo import gdal  # noqa: E402

from pysent.composite import (  # noqa: E402
    COUNT_BAND,
    build_composite_output_filename,
    composite_input_digest,
    normalize_composite_region,
    process_sentinel_composite,
    region_transform,
)
from pysent.errors import EmptySceneError  # noqa: E402

NO2 = "/PRODUCT/nitrogendioxide_tropospheric_column"

# A region of whole 0.05 degree cells, small enough to assert cell by cell.
REGION = {"bbox": [10.0, 60.0, 11.0, 61.0], "epsg": 4326, "res": 0.05}
NO_EXTRAS = {"styled": False, "thumbnail": False}


# --------------------------------------------------------------------------- #
# Synthetic granules
# --------------------------------------------------------------------------- #
def _write_granule(
    path,
    *,
    lat_range=(60.1, 60.9),
    lon_range=(10.1, 10.9),
    value=1e-5,
    qa=100,
    shape=(24, 24),
    start="2026-10-09T10:00:00Z",
    end="2026-10-09T10:05:00Z",
):
    """A Sentinel-5P-shaped granule: ``/PRODUCT`` arrays and a byte-packed quality.

    Keeps the parts the pipeline reads - the group layout, the 0.01 scale on
    ``qa_value``, the coverage attributes - and nothing else.
    """
    rows, columns = shape
    lat = np.repeat(np.linspace(lat_range[0], lat_range[1], rows)[:, None], columns, axis=1)
    lon = np.repeat(np.linspace(lon_range[0], lon_range[1], columns)[None, :], rows, axis=0)
    values = np.full(shape, value, dtype=np.float32) if np.isscalar(value) else np.asarray(value, dtype=np.float32)
    quality = np.full(shape, qa, dtype=np.uint8) if np.isscalar(qa) else np.asarray(qa, dtype=np.uint8)

    path = str(path)
    driver = gdal.GetDriverByName("netCDF")
    dataset = driver.CreateMultiDimensional(path)
    root = dataset.GetRootGroup()
    for name, text in (("time_coverage_start", start), ("time_coverage_end", end)):
        if text:
            attribute = root.CreateAttribute(name, [], gdal.ExtendedDataType.CreateString())
            attribute.Write(text)
    group = root.CreateGroup("PRODUCT")
    scanline = group.CreateDimension("scanline", None, None, rows)
    ground_pixel = group.CreateDimension("ground_pixel", None, None, columns)

    for name, data, gdal_type in (
        ("latitude", lat.astype(np.float32), gdal.GDT_Float32),
        ("longitude", lon.astype(np.float32), gdal.GDT_Float32),
        ("nitrogendioxide_tropospheric_column", values, gdal.GDT_Float32),
        ("qa_value", quality, gdal.GDT_Byte),
    ):
        array = group.CreateMDArray(
            name, [scanline, ground_pixel], gdal.ExtendedDataType.Create(gdal_type)
        )
        if name == "qa_value":
            # Declared before any data is written: netCDF refuses to define the
            # packing of a variable that already holds values.
            array.SetScale(0.01)
        array.Write(data.tobytes())
    dataset = None
    return path


def _granule(tmp_path, index, **kwargs):
    """A granule under a realistic product name, so detection routes it.

    Its coverage attributes follow the index, like the name does, so a test
    can tell which granule set the window.
    """
    name = f"S5P_NRTI_L2__NO2____2026100{index}T100000_2026100{index}T100500_00001_01_020400_20261009T120000.nc"
    kwargs.setdefault("start", f"2026-10-0{index}T10:00:00Z")
    kwargs.setdefault("end", f"2026-10-0{index}T10:05:00Z")
    return _write_granule(tmp_path / name, **kwargs)


def _read(path, band=1):
    dataset = gdal.Open(str(path))
    array = dataset.GetRasterBand(band).ReadAsArray()
    dataset = None
    return array


# --------------------------------------------------------------------------- #
# The region is the caller's, and it has to be a grid
# --------------------------------------------------------------------------- #
def test_a_composite_without_a_region_is_refused():
    """Q3: a composite has no single input to take a grid from."""
    with pytest.raises(ValueError, match="explicit region"):
        normalize_composite_region(None)


@pytest.mark.parametrize(
    ("region", "message"),
    [
        ({"bbox": [10, 60, 11, 61], "epsg": 4326}, "missing 'res'"),
        ({"bbox": [10, 60, 11, 61], "res": 0.05}, "missing 'epsg'"),
        ({"epsg": 4326, "res": 0.05}, "missing 'bbox'"),
        ({"bbox": [10, 60, 11], "epsg": 4326, "res": 0.05}, "four numbers"),
        ({"bbox": [11, 60, 10, 61], "epsg": 4326, "res": 0.05}, "west < east"),
        ({"bbox": [10, 61, 11, 60], "epsg": 4326, "res": 0.05}, "south < north"),
        ({"bbox": [10, 60, 11, 61], "epsg": 4326, "res": 0}, "res must be positive"),
        ({"bbox": [10, 60, 200, 61], "epsg": 4326, "res": 0.05}, "longitudes"),
        ({"bbox": [10, 60, 11, 90], "epsg": 4326, "res": 0.05}, "reaching a pole"),
        ({"bbox": [10, 60, 11, 61], "epsg": 4326, "res": 0.07}, "not a whole"),
        ({"bbox": [10, 60, 11, 61], "epsg": 4326, "res": 0.05, "crs": "x"}, "unknown key"),
    ],
)
def test_a_bad_region_fails_before_anything_is_opened(region, message):
    with pytest.raises(ValueError, match=message):
        normalize_composite_region(region)


def test_a_region_that_is_not_a_whole_number_of_cells_names_the_nearest_ones():
    """Snapping silently would shift one composite half a cell against another."""
    with pytest.raises(ValueError) as caught:
        normalize_composite_region({"bbox": [10.0, 60.0, 11.0, 61.0], "epsg": 4326, "res": 0.3})
    message = str(caught.value)
    assert "0.9" in message and "1.2" in message


def test_the_region_becomes_a_north_up_grid():
    region = normalize_composite_region(REGION)
    assert (region["width"], region["height"], region["cells"]) == (20, 20, 400)
    assert region_transform(region) == (10.0, 0.05, 0.0, 61.0, 0.0, -0.05)


def test_a_region_too_large_for_the_context_is_refused(tmp_path):
    with pytest.raises(ValueError, match="over the"):
        process_sentinel_composite(
            input_datasets=[_granule(tmp_path, 1)],
            output_dir=tmp_path / "out",
            region={"bbox": [-180.0, -80.0, 180.0, 80.0], "epsg": 4326, "res": 0.001},
            processing_options={"context": "interactive", **NO_EXTRAS},
        )


def test_the_grid_cannot_be_overridden_per_product_or_per_request(tmp_path):
    """Two composites on different grids cannot be compared, so neither is allowed."""
    with pytest.raises(ValueError, match="does not apply to a composite"):
        process_sentinel_composite(
            input_datasets=[_granule(tmp_path, 1)],
            output_dir=tmp_path / "out",
            region=REGION,
            processing_options={"target_resolution": 0.1, **NO_EXTRAS},
        )
    with pytest.raises(ValueError, match="per-product grid does not apply"):
        process_sentinel_composite(
            input_datasets=[_granule(tmp_path, 1)],
            output_dir=tmp_path / "out",
            region=REGION,
            products={"no2": {"grid": {"res": 0.1}}},
            processing_options=NO_EXTRAS,
        )


# --------------------------------------------------------------------------- #
# The arithmetic
# --------------------------------------------------------------------------- #
def test_the_composite_is_the_mean_and_the_second_band_counts_the_observations(tmp_path):
    """Three granules over the same ground: the mean of 1, 2 and 3 is 2."""
    inputs = [
        _granule(tmp_path, index, value=value * 1e-5)
        for index, value in ((1, 1.0), (2, 2.0), (3, 3.0))
    ]
    result = process_sentinel_composite(
        input_datasets=inputs,
        output_dir=tmp_path / "out",
        region=REGION,
        processing_options=NO_EXTRAS,
    )[0]

    values = _read(result["path"])
    counts = _read(result["path"], COUNT_BAND)
    filled = np.isfinite(values)
    assert filled.any()
    assert np.allclose(values[filled], 2e-5, rtol=1e-5)
    assert set(np.unique(counts[filled]).tolist()) == {3.0}
    assert result["observations_per_cell"] == {"mean": 3.0, "max": 3, "single_share": 0.0}
    assert len(result["inputs_used"]) == 3
    assert result["inputs_skipped"] == []


def test_a_cell_seen_once_is_reported_as_seen_once(tmp_path):
    """A third of a real daily composite's cells are a single observation."""
    inputs = [
        _granule(tmp_path, 1, value=1e-5, lon_range=(10.05, 10.45)),
        _granule(tmp_path, 2, value=3e-5, lon_range=(10.55, 10.95)),
    ]
    result = process_sentinel_composite(
        input_datasets=inputs,
        output_dir=tmp_path / "out",
        region=REGION,
        processing_options=NO_EXTRAS,
    )[0]

    counts = _read(result["path"], COUNT_BAND)
    values = _read(result["path"])
    filled = np.isfinite(values)
    # The two granules do not overlap, so every filled cell holds exactly one
    # observation and its own granule's value - no blending between them.
    assert set(np.unique(counts[filled]).tolist()) == {1.0}
    assert result["observations_per_cell"]["single_share"] == 1.0
    assert np.isclose(np.nanmin(values), 1e-5, rtol=1e-5)
    assert np.isclose(np.nanmax(values), 3e-5, rtol=1e-5)


def test_coverage_says_how_much_of_the_region_is_empty(tmp_path):
    """"Daily mean" overstates a product that covers 80 % of its region."""
    result = process_sentinel_composite(
        input_datasets=[_granule(tmp_path, 1, lat_range=(60.05, 60.45))],
        output_dir=tmp_path / "out",
        region=REGION,
        processing_options=NO_EXTRAS,
    )[0]
    # Under half the region, and coverage is the share of cells with a value.
    assert 0.1 < result["coverage"] < 0.6
    assert result["coverage"] == pytest.approx(result["valid_fraction"], abs=1e-9)


def test_a_one_input_composite_is_the_single_granule_product(s5p_no2_window, tmp_path):
    """Risk 4: a composite must not re-implement the masking or the gridding.

    The composite reads and masks a granule in its own module, so the only
    thing standing between it and a quiet divergence from ``pysent.s5p`` is
    this comparison on a real granule.
    """
    from pysent.s5p import process_sentinel_s5p_netcdf

    single = process_sentinel_s5p_netcdf(
        input_dataset=str(s5p_no2_window),
        output_dir=tmp_path / "single",
        products={"no2": {}},
        processing_options=NO_EXTRAS,
    )[0]

    region = {
        "bbox": single["bounds"],
        "epsg": single["epsg"],
        "res": single["resolution"],
    }
    composite = process_sentinel_composite(
        input_datasets=[str(s5p_no2_window)],
        output_dir=tmp_path / "composite",
        region=region,
        products={"no2": {}},
        processing_options=NO_EXTRAS,
    )[0]

    assert (composite["width"], composite["height"]) == (single["width"], single["height"])
    one = _read(single["path"])
    many = _read(composite["path"])
    assert np.array_equal(np.isfinite(one), np.isfinite(many))
    assert np.allclose(one[np.isfinite(one)], many[np.isfinite(many)], rtol=1e-6, atol=0.0)
    # And every cell of it is a single observation, which is what it is.
    counts = _read(composite["path"], COUNT_BAND)
    assert set(np.unique(counts[np.isfinite(many)]).tolist()) == {1.0}


def test_the_weighted_mean_matches_the_plain_one_when_the_quality_is_uniform(tmp_path):
    """A6: at the recommended threshold 96 % of the weights are exactly 1.00."""
    inputs = [
        _granule(tmp_path, 1, value=1e-5, qa=100),
        _granule(tmp_path, 2, value=3e-5, qa=100),
    ]
    plain, weighted = (
        process_sentinel_composite(
            input_datasets=inputs,
            output_dir=tmp_path / method,
            region=REGION,
            method=method,
            processing_options=NO_EXTRAS,
        )[0]
        for method in ("mean", "weighted_mean")
    )
    one, two = _read(plain["path"]), _read(weighted["path"])
    assert np.allclose(one[np.isfinite(one)], two[np.isfinite(two)], rtol=1e-6)
    assert weighted["provenance"]["PYSENT_COMPOSITE_METHOD"] == "weighted_mean"


def test_the_weighted_mean_leans_towards_the_better_pixel(tmp_path):
    """Where the quality does vary, the weighting has to move the answer."""
    inputs = [
        _granule(tmp_path, 1, value=1e-5, qa=100),
        _granule(tmp_path, 2, value=3e-5, qa=60),
    ]
    plain, weighted = (
        process_sentinel_composite(
            input_datasets=inputs,
            output_dir=tmp_path / method,
            region=REGION,
            method=method,
            products={"no2": {"qa_min": 0.5}},
            processing_options=NO_EXTRAS,
        )[0]
        for method in ("mean", "weighted_mean")
    )
    # Plain: (1 + 3) / 2 = 2. Weighted: (1*1.0 + 3*0.6) / 1.6 = 1.75.
    assert np.isclose(np.nanmean(_read(plain["path"])), 2e-5, rtol=1e-3)
    assert np.isclose(np.nanmean(_read(weighted["path"])), 1.75e-5, rtol=1e-3)


# --------------------------------------------------------------------------- #
# A useless input is normal
# --------------------------------------------------------------------------- #
def test_an_input_that_contributes_nothing_is_reported_not_raised(tmp_path):
    """A3: 4 of 8 real inputs added nothing to the region."""
    inputs = [
        _granule(tmp_path, 1, value=2e-5),
        _granule(tmp_path, 2, lat_range=(20.0, 21.0), lon_range=(100.0, 101.0)),
        _granule(tmp_path, 3, qa=10),  # nothing above the default threshold
        str(tmp_path / "S5P_NRTI_L2__NO2____20261004T100000_stub.nc"),
    ]
    open(inputs[-1], "wb").close()  # a zero-byte stub, a third of the NRTI archive

    result = process_sentinel_composite(
        input_datasets=inputs,
        output_dir=tmp_path / "out",
        region=REGION,
        processing_options=NO_EXTRAS,
    )[0]

    assert [entry["input"] for entry in result["inputs_used"]] == [
        inputs[0].rsplit("/", 1)[-1]
    ]
    reasons = {entry["reason"] for entry in result["inputs_skipped"]}
    assert reasons == {"no_overlap", "no_pixels_above_qa_min", "unreadable"}
    assert len(result["inputs"]) == 4
    assert result["provenance"]["PYSENT_COMPOSITE_INPUT_COUNT"] == "4"
    assert result["provenance"]["PYSENT_COMPOSITE_INPUTS_CONTRIBUTING"] == "1"


def test_a_composite_with_nothing_in_it_raises_and_says_why(tmp_path):
    inputs = [
        _granule(tmp_path, 1, lat_range=(20.0, 21.0), lon_range=(100.0, 101.0)),
        _granule(tmp_path, 2, qa=10),
    ]
    with pytest.raises(EmptySceneError) as caught:
        process_sentinel_composite(
            input_datasets=inputs,
            output_dir=tmp_path / "out",
            region=REGION,
            processing_options=NO_EXTRAS,
        )
    message = str(caught.value)
    assert "no_overlap" in message and "no_pixels_above_qa_min" in message


def test_no_inputs_at_all_is_a_request_error(tmp_path):
    with pytest.raises(ValueError, match="at least one input"):
        process_sentinel_composite(
            input_datasets=[], output_dir=tmp_path, region=REGION
        )


def test_an_unsupported_platform_says_which_phase_covers_it(tmp_path, s3_synergy_window):
    with pytest.raises(ValueError, match="Phase 3"):
        process_sentinel_composite(
            input_datasets=[str(s3_synergy_window)],
            output_dir=tmp_path / "out",
            region=REGION,
            products={"no2": {}},
            processing_options=NO_EXTRAS,
        )


# --------------------------------------------------------------------------- #
# Identity and provenance
# --------------------------------------------------------------------------- #
def test_the_digest_is_over_the_names_and_ignores_their_order_and_location():
    first = composite_input_digest(["/a/one.nc", "/a/two.nc"])
    assert first == composite_input_digest(["/b/two.nc", "/b/one.nc?token=secret"])
    assert first != composite_input_digest(["/a/one.nc", "/a/three.nc"])
    assert first.startswith("sha256:")


def test_the_output_name_carries_the_product_the_threshold_and_the_key():
    name = build_composite_output_filename(product="no2", qa_min=0.75, key="abc123abc123")
    assert name == "composite_no2_qa75_abc123abc123.tif"


def test_the_key_changes_with_anything_that_changes_the_pixels(tmp_path):
    inputs = [_granule(tmp_path, 1), _granule(tmp_path, 2)]
    keys = {}
    for label, products, method in (
        ("default", {"no2": {}}, "mean"),
        ("threshold", {"no2": {"qa_min": 0.5}}, "mean"),
        ("method", {"no2": {}}, "weighted_mean"),
    ):
        keys[label] = process_sentinel_composite(
            input_datasets=inputs,
            output_dir=tmp_path / label,
            region=REGION,
            products=products,
            method=method,
            processing_options=NO_EXTRAS,
        )[0]["composite_key"]
    assert len(set(keys.values())) == 3

    # The same request twice is the same key, and the input order does not count.
    again = process_sentinel_composite(
        input_datasets=list(reversed(inputs)),
        output_dir=tmp_path / "again",
        region=REGION,
        processing_options=NO_EXTRAS,
    )[0]["composite_key"]
    assert again == keys["default"]


def test_the_provenance_is_one_fact_per_tag(tmp_path):
    """Q4: a JSON-LD/PROV-O mapping is planned, so nothing here is composed.

    The bounding box is four tags rather than one string, the times are ISO
    8601 UTC, and the input list is in the result rather than the file.
    """
    result = process_sentinel_composite(
        input_datasets=[_granule(tmp_path, 1), _granule(tmp_path, 2)],
        output_dir=tmp_path / "out",
        region=REGION,
        processing_options=NO_EXTRAS,
    )[0]

    tags = _tags(result["path"])
    assert tags["PYSENT_COMPOSITE_METHOD"] == "mean"
    assert tags["PYSENT_COMPOSITE_INPUT_COUNT"] == "2"
    assert tags["PYSENT_COMPOSITE_INPUT_DIGEST"] == result["input_digest"]
    assert tags["PYSENT_COMPOSITE_INPUT_DIGEST"].startswith("sha256:")
    assert tags["PYSENT_COMPOSITE_KEY"] == result["composite_key"]
    assert tags["PYSENT_COMPOSITE_COUNT_BAND"] == str(COUNT_BAND)
    assert (
        tags["PYSENT_COMPOSITE_REGION_WEST"],
        tags["PYSENT_COMPOSITE_REGION_SOUTH"],
        tags["PYSENT_COMPOSITE_REGION_EAST"],
        tags["PYSENT_COMPOSITE_REGION_NORTH"],
    ) == ("10", "60", "11", "61")
    assert tags["PYSENT_COMPOSITE_TIME_START"] == "2026-10-01T10:00:00Z"
    assert tags["PYSENT_COMPOSITE_TIME_END"] == "2026-10-02T10:05:00Z"
    assert tags["PYSENT_QA_MIN"] == "0.75"
    # No tag holds a list of inputs; that is what the digest and the result are for.
    assert not any(".nc" in value for value in tags.values())
    assert [name for name in result["inputs"]] and all(
        name.endswith(".nc") for name in result["inputs"]
    )


def test_the_window_comes_from_the_granules_not_from_the_request(tmp_path):
    """A granule that contributed nothing still counts towards the window."""
    inputs = [
        _granule(tmp_path, 1, start="2026-10-09T23:30:00Z", end="2026-10-10T00:05:00Z"),
        _granule(
            tmp_path,
            2,
            lat_range=(20.0, 21.0),
            lon_range=(100.0, 101.0),
            start="2026-10-09T01:00:00Z",
            end="2026-10-09T01:05:00Z",
        ),
    ]
    result = process_sentinel_composite(
        input_datasets=inputs,
        output_dir=tmp_path / "out",
        region=REGION,
        processing_options=NO_EXTRAS,
    )[0]
    assert result["time"] == {"start": "2026-10-09T01:00:00Z", "end": "2026-10-10T00:05:00Z"}


# --------------------------------------------------------------------------- #
# The three files
# --------------------------------------------------------------------------- #
def test_three_files_are_written_and_the_styled_one_is_paletted(tmp_path):
    from pathlib import Path

    result = process_sentinel_composite(
        input_datasets=[_granule(tmp_path, 1)],
        output_dir=tmp_path / "out",
        region=REGION,
    )[0]

    for key in ("path", "styled_path", "thumbnail_path"):
        assert Path(result[key]).exists(), key

    product = gdal.Open(result["path"])
    assert product.RasterCount == 2
    assert product.GetRasterBand(1).GetDescription() == "Tropospheric NO₂ column"
    assert product.GetRasterBand(COUNT_BAND).GetDescription() == "observations"
    values = product.GetRasterBand(1).ReadAsArray()
    counts = product.GetRasterBand(COUNT_BAND).ReadAsArray()
    # A GeoTIFF carries one no-data value for the whole file, so the count band
    # cannot have its own: it inherits band 1's NaN. That is harmless because a
    # count is never NaN - zero observations is written as a zero, and it is
    # exactly where the composite itself is NaN - but it does mean a reader
    # must not treat "has no-data" as "may be absent" on this band.
    assert np.isnan(product.GetRasterBand(COUNT_BAND).GetNoDataValue())
    product = None
    assert np.isfinite(counts).all()
    assert np.array_equal(np.isfinite(values), counts > 0)

    styled = gdal.Open(result["styled_path"])
    assert styled.RasterCount == 1
    assert styled.GetRasterBand(1).GetRasterColorTable() is not None
    assert styled.GetRasterBand(1).GetNoDataValue() == 0
    styled = None


def test_a_failed_run_leaves_no_file_under_the_final_name(tmp_path, monkeypatch):
    """The product is written through ``atomic_output`` like every other one."""
    import pysent.composite as composite

    def explode(**kwargs):
        raise RuntimeError("no disk")

    monkeypatch.setattr(composite, "finish_float_product", explode)
    with pytest.raises(RuntimeError, match="no disk"):
        process_sentinel_composite(
            input_datasets=[_granule(tmp_path, 1)],
            output_dir=tmp_path / "out",
            region=REGION,
            processing_options=NO_EXTRAS,
        )
    assert not list((tmp_path / "out").glob("composite_*.tif"))


def test_a_bad_method_is_refused_before_anything_is_opened(tmp_path):
    with pytest.raises(ValueError, match="method must be one of"):
        process_sentinel_composite(
            input_datasets=["missing.nc"],
            output_dir=tmp_path,
            region=REGION,
            method="median",
        )


def _tags(path):
    dataset = gdal.Open(str(path))
    tags = dict(dataset.GetMetadata())
    dataset = None
    return tags
