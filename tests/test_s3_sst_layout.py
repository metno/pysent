"""Sea surface temperature in both SL_2_WST layouts.

Products before about June 2026 (GHRSST v02.0) hold one orbit-long GHRSST file
with no view in its name; later ones hold the ``_DUAL_``/``_NADIR_`` pair. With
no view asked for, the older layout is read as view ``best`` and the result
says so and why; an asked-for view is never changed.

The older layout is built from the committed frame by renaming its member to
the single-file shape, so the pixels are the same and only the layout differs.
"""
import zipfile

import numpy as np
import pytest

pytest.importorskip("osgeo")

from osgeo import gdal  # noqa: E402

from pysent._sen3 import resolve_variable, sen3_root, single_ghrsst_member  # noqa: E402
from pysent.s3 import (  # noqa: E402
    VIEW_BY_LAYOUT_REASON,
    normalize_sentinel_s3_product_map,
    process_sentinel_s3_safe,
)

FAST = {"styled": False, "thumbnail": False}
SINGLE_NAME = "20241219162421-MAR-L2P_GHRSST-SSTskin-SLSTRB-20241220222222-v02.0-fv01.0.nc"


@pytest.fixture()
def single_file_product(s3_slstr_sst_window, tmp_path):
    """The committed frame repacked in the pre-2026-06 layout."""
    path = tmp_path / "S3B_SL_2_WST____20241219T162421_single.zip"
    with zipfile.ZipFile(s3_slstr_sst_window) as source, zipfile.ZipFile(
        path, "w", compression=zipfile.ZIP_STORED
    ) as target:
        for info in source.infolist():
            name = info.filename
            if "_DUAL_" in name:
                name = f"{name.split('/')[0]}/{SINGLE_NAME}"
            target.writestr(name, source.read(info.filename))
    return path


def _read(path):
    dataset = gdal.Open(path)
    values = dataset.GetRasterBand(1).ReadAsArray()
    tags = dataset.GetMetadata()
    dataset = None
    return values, tags


# --------------------------------------------------------------------------- #
# Recognising the layout
# --------------------------------------------------------------------------- #
def test_one_ghrsst_file_without_a_view_is_the_single_file_layout():
    assert single_ghrsst_member(["X.SEN3/xfdumanifest.xml", f"X.SEN3/{SINGLE_NAME}"]) == (
        f"X.SEN3/{SINGLE_NAME}"
    )


@pytest.mark.parametrize("members", [
    ["a-L2P_GHRSST-NTC_DUAL_x.nc", "a-L2P_GHRSST-NTC_NADIR_x.nc"],  # the pair
    ["a-L2P_GHRSST-NTC_DUAL_x.nc"],                                 # one view of the pair
    ["xfdumanifest.xml", "LST_in.nc"],                              # not a GHRSST product
    ["a-L2P_GHRSST-x.nc", "b-L2P_GHRSST-y.nc"],                     # no layout seen so far
])
def test_anything_else_is_not(members):
    assert single_ghrsst_member(members) is None


# --------------------------------------------------------------------------- #
# No view asked for
# --------------------------------------------------------------------------- #
def test_the_single_file_layout_is_read_as_best_and_says_why(single_file_product, tmp_path):
    result = process_sentinel_s3_safe(
        input_dataset=str(single_file_product), output_dir=tmp_path, products={"sst": {}},
        processing_options=FAST,
    )[0]
    assert result["view"] == "best"
    assert result["view_reason"] == VIEW_BY_LAYOUT_REASON
    assert result["member"].endswith(SINGLE_NAME)
    assert result["provenance"]["PYSENT_VIEW"] == "best"
    assert result["provenance"]["PYSENT_VIEW_REASON"] == VIEW_BY_LAYOUT_REASON
    _values, tags = _read(result["path"])
    assert tags["PYSENT_VIEW"] == "best"
    assert tags["PYSENT_VIEW_REASON"] == VIEW_BY_LAYOUT_REASON


def test_the_default_product_list_gets_the_same_pick(single_file_product, tmp_path):
    """The service's ingest asks for no products at all: SL_2_WST's default."""
    result = process_sentinel_s3_safe(
        input_dataset=str(single_file_product), output_dir=tmp_path, processing_options=FAST,
    )[0]
    assert (result["product"], result["view"]) == ("sst", "best")


def test_best_on_the_single_file_is_the_same_pixels_as_asking_for_it(single_file_product, tmp_path):
    """Picked by layout, the read is exactly the read of an explicit view='best';
    only the provenance says who chose."""
    picked = process_sentinel_s3_safe(
        input_dataset=str(single_file_product), output_dir=tmp_path / "picked",
        products={"sst": {}}, processing_options=FAST,
    )[0]
    asked = process_sentinel_s3_safe(
        input_dataset=str(single_file_product), output_dir=tmp_path / "asked",
        products={"sst": {"view": "best"}}, processing_options=FAST,
    )[0]
    assert asked["view_reason"] == "requested"
    assert "PYSENT_VIEW" not in asked["provenance"]
    picked_values, _ = _read(picked["path"])
    asked_values, _ = _read(asked["path"])
    np.testing.assert_array_equal(picked_values, asked_values)


def test_the_callers_other_options_survive_the_pick(single_file_product, tmp_path):
    result = process_sentinel_s3_safe(
        input_dataset=str(single_file_product), output_dir=tmp_path,
        products={"SST": {"value_range": (0.0, 10.0), "unit": "kelvin"}},
        processing_options=FAST,
    )[0]
    assert result["view"] == "best"
    assert result["style_hint"]["value_range"] == [0.0, 10.0]
    # Kelvin, not celsius: sea water is above 250 K and nowhere near 250 C.
    assert result["value_stats"]["p01"] > 250.0


def test_the_pair_layout_keeps_its_default_and_its_tags(s3_slstr_sst_window, tmp_path):
    """A product read the way it always was keeps exactly the tags it had."""
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_slstr_sst_window), output_dir=tmp_path, products={"sst": {}},
        processing_options=FAST,
    )[0]
    assert (result["view"], result["view_reason"]) == ("dual", "default")
    assert "_DUAL_" in result["member"]
    _values, tags = _read(result["path"])
    assert "PYSENT_VIEW" not in tags and "PYSENT_VIEW_REASON" not in tags


# --------------------------------------------------------------------------- #
# A view asked for is never changed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("view", ["dual", "nadir"])
def test_an_asked_for_view_on_the_single_file_still_fails_with_the_hint(
    single_file_product, tmp_path, view
):
    with pytest.raises(ValueError, match=r"single GHRSST file.*view='best'"):
        process_sentinel_s3_safe(
            input_dataset=str(single_file_product), output_dir=tmp_path,
            products={"sst": {"view": view}}, processing_options=FAST,
        )


def test_resolve_variable_names_the_layout_too(single_file_product, tmp_path):
    """The miss the service met came from here, without a hint."""
    root = sen3_root(str(single_file_product))
    with pytest.raises(ValueError, match=r"none of .*_DUAL_.*single GHRSST file.*view='best'"):
        resolve_variable(root, [("*_DUAL_*.nc", "sea_surface_temperature")], tmp_path)


def test_a_miss_on_another_product_keeps_its_old_message(s3_slstr_lst_window, tmp_path):
    root = sen3_root(str(s3_slstr_lst_window))
    with pytest.raises(ValueError, match=r"would look exactly like this\.$"):
        resolve_variable(root, [("nothing.nc", "nothing")], tmp_path)


def test_normalising_records_who_chose_the_view():
    assert normalize_sentinel_s3_product_map({"sst": {}})["sst"]["view_reason"] == "default"
    assert normalize_sentinel_s3_product_map({"sst": {"view": "NADIR"}})["sst"]["view_reason"] == (
        "requested"
    )
    assert normalize_sentinel_s3_product_map({"lst": {}})["lst"]["view_reason"] is None
