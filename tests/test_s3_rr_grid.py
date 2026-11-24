"""OLCI reduced-resolution types and the ``bbox`` crop.

A reduced-resolution file (``OL_2_WRR``, ``OL_2_LRR``, ``OL_1_ERR``) is not a
frame but a 44-minute daylight orbit segment at 1.2 km. Gridded at the
full-resolution 300 m it came to 3.5 G cells; at its own 1.2 km it is still
217 M, so a whole segment only fits once it is cropped. These tests pin both
halves: the grid follows the type, and the crop keeps exactly the source pixels
that reach into the box.
"""
import zipfile

import numpy as np
import pytest

pytest.importorskip("osgeo")

from osgeo import gdal  # noqa: E402

from pysent._swath import (  # noqa: E402
    METRES_PER_DEGREE,
    bbox_window,
    inside_bbox,
    normalise_bbox,
)
from pysent.errors import EmptySceneError  # noqa: E402
from pysent.s3 import (  # noqa: E402
    OLCI_GROUND_SAMPLING_M,
    OLCI_RR_GROUND_SAMPLING_M,
    _ground_sampling_m,
    process_sentinel_s3_safe,
)

# The test window (a real WFR frame, 320 x 320) spans lat 54.59..55.52 and
# lon 17.84..19.37. This box takes its western half.
WEST_HALF = (17.0, 54.0, 18.6, 56.0)
FAR_AWAY = (-30.0, -10.0, -20.0, 0.0)
QUIET = {"styled": False, "thumbnail": False}


# --------------------------------------------------------------------------- #
# The box itself
# --------------------------------------------------------------------------- #
def test_a_bbox_is_four_numbers_from_a_sequence_or_a_string():
    assert normalise_bbox([-30, 50, 60, 85]) == (-30.0, 50.0, 60.0, 85.0)
    assert normalise_bbox("-30, 50,60,85") == (-30.0, 50.0, 60.0, 85.0)
    assert normalise_bbox(None) is None
    assert normalise_bbox("") is None


def test_a_box_whose_west_edge_is_east_of_its_east_edge_crosses_the_antimeridian():
    assert normalise_bbox((170, 60, -170, 70)) == (170.0, 60.0, -170.0, 70.0)
    lat = np.full((1, 4), 65.0)
    lon = np.array([[175.0, -175.0, 0.0, 160.0]])
    assert inside_bbox(lat, lon, (170.0, 60.0, -170.0, 70.0)).tolist() == [[True, True, False, False]]


@pytest.mark.parametrize("value, message", [
    ((1, 2, 3), "four numbers"),
    ("a,b,c,d", "four numbers"),
    ((0, 0, float("nan"), 1), "four numbers"),
    ((-190, 0, 10, 1), "-180..180"),
    ((0, 10, 1, 5), "lat0 below lat1"),
    ((0, -95, 1, 5), "-90..90"),
    ((5, 0, 5, 1), "no width"),
])
def test_a_bad_bbox_is_refused_with_what_is_wrong(value, message):
    with pytest.raises(ValueError, match=message):
        normalise_bbox(value)


def test_the_window_is_the_block_of_rows_and_columns_reaching_into_the_box():
    lat, lon = np.meshgrid(np.linspace(60, 50, 11), np.linspace(0, 10, 11), indexing="ij")
    rows, columns = bbox_window(lat, lon, (2.5, 52.5, 4.5, 55.5))
    # lat 55, 54, 53 are rows 5-7; lon 3, 4 are columns 3-4.
    assert (rows.start, rows.stop, columns.start, columns.stop) == (5, 8, 3, 5)


def test_a_fill_coordinate_is_never_inside():
    lat = np.array([[np.nan, 55.0]])
    lon = np.array([[5.0, np.nan]])
    assert bbox_window(lat, lon, (0.0, 50.0, 10.0, 60.0)) is None


# --------------------------------------------------------------------------- #
# The grid follows the type
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("product_type, metres", [
    ("OL_2_WRR___", OLCI_RR_GROUND_SAMPLING_M),
    ("OL_2_LRR___", OLCI_RR_GROUND_SAMPLING_M),
    ("OL_1_ERR___", OLCI_RR_GROUND_SAMPLING_M),
    ("OL_2_WFR___", OLCI_GROUND_SAMPLING_M),
    ("OL_2_LFR___", OLCI_GROUND_SAMPLING_M),
    ("OL_1_EFR___", OLCI_GROUND_SAMPLING_M),
])
def test_each_olci_type_has_its_own_ground_sampling(product_type, metres):
    assert _ground_sampling_m({}, {"productType": product_type}) == metres


def test_a_product_that_names_its_own_sampling_keeps_it():
    assert _ground_sampling_m({"ground_sampling_m": 1000.0}, {"productType": "OL_2_WRR___"}) == 1000.0


def _as_type(source, target, product_type):
    """The test window with its manifest saying it is another OLCI type."""
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as copy:
        for item in original.infolist():
            data = original.read(item.filename)
            if item.filename.endswith("xfdumanifest.xml"):
                data = data.replace(b"OL_2_WFR___", product_type.encode())
            copy.writestr(item, data)
    return target


def test_a_reduced_resolution_segment_is_gridded_at_1200_m(s3_olci_window, tmp_path):
    segment = _as_type(s3_olci_window, tmp_path / "wrr.zip", "OL_2_WRR___")
    result = process_sentinel_s3_safe(
        input_dataset=str(segment), output_dir=tmp_path / "out", products={"chl": {}},
        processing_options=QUIET,
    )[0]
    assert result["resolution"] == pytest.approx(1200.0 / METRES_PER_DEGREE, rel=1e-6)


def test_target_resolution_still_overrides_the_type(s3_olci_window, tmp_path):
    segment = _as_type(s3_olci_window, tmp_path / "wrr.zip", "OL_2_WRR___")
    result = process_sentinel_s3_safe(
        input_dataset=str(segment), output_dir=tmp_path / "out", products={"chl": {}},
        processing_options={**QUIET, "target_resolution": 0.01},
    )[0]
    assert result["resolution"] == pytest.approx(0.01)


# --------------------------------------------------------------------------- #
# The crop
# --------------------------------------------------------------------------- #
def _read(path):
    dataset = gdal.Open(path)
    values = dataset.GetRasterBand(1).ReadAsArray()
    transform = dataset.GetGeoTransform()
    tags = dataset.GetMetadata()
    return values, transform, tags


def test_a_bbox_crops_the_grid_and_says_so(s3_olci_window, tmp_path):
    whole = process_sentinel_s3_safe(
        input_dataset=str(s3_olci_window), output_dir=tmp_path / "whole", products={"chl": {}},
        processing_options=QUIET,
    )[0]
    cropped = process_sentinel_s3_safe(
        input_dataset=str(s3_olci_window), output_dir=tmp_path / "cropped", products={"chl": {}},
        processing_options={**QUIET, "bbox": list(WEST_HALF)},
    )[0]

    assert cropped["width"] < whole["width"]
    assert cropped["resolution"] == whole["resolution"]
    assert cropped["bbox"] == list(WEST_HALF)
    rows0, rows1, columns0, columns1 = cropped["source_window"]
    assert (rows0, rows1) == (0, 320) and 0 < columns1 - columns0 < 320
    _values, _transform, tags = _read(cropped["path"])
    assert tags["PYSENT_BBOX"] == "17,54,18.6,56"
    assert cropped["provenance"]["PYSENT_BBOX"] == "17,54,18.6,56"

    assert whole["bbox"] is None and whole["source_window"] is None
    _values, _transform, tags = _read(whole["path"])
    assert "PYSENT_BBOX" not in tags


def _whole_and_cropped(source, tmp_path, bbox):
    whole = process_sentinel_s3_safe(
        input_dataset=str(source), output_dir=tmp_path / "whole", products={"chl": {}},
        processing_options=QUIET,
    )[0]
    cropped = process_sentinel_s3_safe(
        input_dataset=str(source), output_dir=tmp_path / "cropped", products={"chl": {}},
        processing_options={**QUIET, "bbox": list(bbox)},
    )[0]
    big, big_transform, _ = _read(whole["path"])
    small, small_transform, _ = _read(cropped["path"])
    step = big_transform[1]
    column = round((small_transform[0] - big_transform[0]) / step)
    row = round((big_transform[3] - small_transform[3]) / step)
    # Both grids are snapped to whole multiples of the resolution, so the
    # cropped one sits on the whole one's cell edges.
    assert column >= 0 and row >= 0
    overlap = big[row:row + small.shape[0], column:column + small.shape[1]]
    assert overlap.shape == small.shape
    return overlap, small


def test_a_box_around_the_whole_frame_changes_no_pixel(s3_olci_window, tmp_path):
    overlap, small = _whole_and_cropped(s3_olci_window, tmp_path, (10.0, 50.0, 30.0, 60.0))
    assert np.array_equal(np.isfinite(overlap), np.isfinite(small))
    finite = np.isfinite(small)
    assert np.array_equal(overlap[finite], small[finite])


def test_the_cropped_pixels_are_the_whole_frames_pixels(s3_olci_window, tmp_path):
    """The crop removes source pixels and changes none of the rest.

    Measured: 6 of 34,421 cells differ, every one beside the cut, where the
    nearest-neighbour warp has lost the neighbours on the far side. A cell away
    from the cut is the whole frame's own.
    """
    overlap, small = _whole_and_cropped(s3_olci_window, tmp_path, WEST_HALF)
    both = np.isfinite(overlap) & np.isfinite(small)
    assert both.sum() > 30_000
    differs = both & (overlap != small)
    assert differs.sum() <= both.sum() // 1000
    # Cells the whole frame fills and the crop does not: the far side of the cut.
    lost = np.isfinite(overlap) & ~np.isfinite(small)
    near_cut = np.zeros_like(lost)
    for dy in range(-3, 4):
        for dx in range(-3, 4):
            near_cut |= np.roll(np.roll(lost, dy, axis=0), dx, axis=1)
    assert not (differs & ~near_cut).any()


def test_a_bbox_with_nothing_inside_is_an_empty_scene(s3_olci_window, tmp_path):
    with pytest.raises(EmptySceneError, match="inside the bbox"):
        process_sentinel_s3_safe(
            input_dataset=str(s3_olci_window), output_dir=tmp_path, products={"chl": {}},
            processing_options={**QUIET, "bbox": list(FAR_AWAY)},
        )


def test_written_empty_it_lands_on_the_boxs_own_grid(s3_olci_window, tmp_path):
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_olci_window), output_dir=tmp_path, products={"chl": {}},
        processing_options={**QUIET, "bbox": list(FAR_AWAY), "on_empty": "write"},
    )[0]
    values, _transform, _tags = _read(result["path"])
    assert not np.isfinite(values).any()
    west, south, east, north = result["bounds"]
    assert west == pytest.approx(-30.0, abs=0.01) and east == pytest.approx(-20.0, abs=0.01)
    assert south == pytest.approx(-10.0, abs=0.01) and north == pytest.approx(0.0, abs=0.01)
    assert result["source_window"] is None


def test_over_the_cap_without_a_bbox_the_refusal_names_bbox(s3_olci_window, tmp_path):
    with pytest.raises(ValueError, match="pass a bbox"):
        process_sentinel_s3_safe(
            input_dataset=str(s3_olci_window), output_dir=tmp_path, products={"chl": {}},
            processing_options={**QUIET, "max_output_pixels": 1000},
        )


def test_over_the_cap_with_a_bbox_it_asks_for_a_smaller_one(s3_olci_window, tmp_path):
    with pytest.raises(ValueError, match="smaller bbox"):
        process_sentinel_s3_safe(
            input_dataset=str(s3_olci_window), output_dir=tmp_path, products={"chl": {}},
            processing_options={**QUIET, "max_output_pixels": 1000, "bbox": list(WEST_HALF)},
        )


def test_a_synergy_tile_refuses_a_bbox_rather_than_ignoring_it(s3_synergy_window, tmp_path):
    with pytest.raises(ValueError, match="does not take one"):
        process_sentinel_s3_safe(
            input_dataset=str(s3_synergy_window), output_dir=tmp_path, products={"ndvi": {}},
            processing_options={**QUIET, "bbox": [0, 0, 1, 1]},
        )


def test_a_bad_bbox_is_refused_before_anything_is_opened(tmp_path):
    with pytest.raises(ValueError, match="four numbers"):
        process_sentinel_s3_safe(
            input_dataset=str(tmp_path / "missing.zip"), output_dir=tmp_path, products={"chl": {}},
            processing_options={"bbox": [1, 2]},
        )
