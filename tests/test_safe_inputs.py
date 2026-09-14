"""Resolving the file GDAL opens from a zipped or unpacked SAFE product.

Regression tests for https://github.com/metno/pysent/issues/1: a ``.zip`` was
passed to GDAL as ``/vsizip/<zip>``, which is a directory and not a dataset, so
Sentinel-2 processing failed with "Unable to open Sentinel-2 SAFE dataset". The
products here are empty skeletons - only the layout matters for resolution.
"""
import zipfile

import pytest

pytest.importorskip("rasterio")
pytest.importorskip("osgeo")

from pysent.s1 import _resolve_safe_manifest_path  # noqa: E402
from pysent.s2 import _resolve_sentinel_s2_dataset_ref  # noqa: E402

S2_NAME = "S2C_MSIL2A_20260810T092031_N0512_R093_T35VPE_20260810T124910"
S1_NAME = "S1D_IW_GRDH_1SDV_20260810T052300_20260810T052325_004059_007650_5857"


def _write_zip(path, safe_name, members):
    with zipfile.ZipFile(path, "w") as archive:
        for member in members:
            archive.writestr(f"{safe_name}.SAFE/{member}", b"")
    return path


@pytest.fixture
def s2_members():
    return ["manifest.safe", "MTD_MSIL2A.xml", "GRANULE/L2A_T35VPE/MTD_TL.xml"]


def test_s2_zip_resolves_to_metadata_xml(tmp_path, s2_members):
    zip_path = _write_zip(tmp_path / f"{S2_NAME}.zip", S2_NAME, s2_members)
    assert _resolve_sentinel_s2_dataset_ref(str(zip_path)) == (
        f"/vsizip/{zip_path}/{S2_NAME}.SAFE/MTD_MSIL2A.xml"
    )


def test_s2_vsizip_prefixed_zip_resolves_to_metadata_xml(tmp_path, s2_members):
    zip_path = _write_zip(tmp_path / f"{S2_NAME}.zip", S2_NAME, s2_members)
    assert _resolve_sentinel_s2_dataset_ref(f"/vsizip/{zip_path}") == (
        f"/vsizip/{zip_path}/{S2_NAME}.SAFE/MTD_MSIL2A.xml"
    )


def test_s2_renamed_zip_finds_the_safe_directory_inside(tmp_path, s2_members):
    zip_path = _write_zip(tmp_path / "input_dataset.zip", S2_NAME, s2_members)
    assert _resolve_sentinel_s2_dataset_ref(str(zip_path)) == (
        f"/vsizip/{zip_path}/{S2_NAME}.SAFE/MTD_MSIL2A.xml"
    )


def test_s2_l1c_zip_resolves_to_l1c_metadata(tmp_path):
    name = S2_NAME.replace("MSIL2A", "MSIL1C")
    zip_path = _write_zip(tmp_path / f"{name}.zip", name, ["manifest.safe", "MTD_MSIL1C.xml"])
    assert _resolve_sentinel_s2_dataset_ref(str(zip_path)).endswith(f"{name}.SAFE/MTD_MSIL1C.xml")


def test_s2_safe_directory_resolves_to_metadata_xml(tmp_path):
    safe_dir = tmp_path / f"{S2_NAME}.SAFE"
    safe_dir.mkdir()
    (safe_dir / "manifest.safe").touch()
    (safe_dir / "MTD_MSIL2A.xml").touch()
    assert _resolve_sentinel_s2_dataset_ref(f"{safe_dir}/") == f"{safe_dir}/MTD_MSIL2A.xml"


def test_s2_metadata_xml_is_used_as_given():
    ref = f"/vsizip//data/{S2_NAME}.zip/{S2_NAME}.SAFE/MTD_MSIL2A.xml"
    assert _resolve_sentinel_s2_dataset_ref(ref) == ref


def test_s2_zip_without_metadata_raises_clearly(tmp_path):
    zip_path = _write_zip(tmp_path / f"{S2_NAME}.zip", S2_NAME, ["manifest.safe"])
    with pytest.raises(RuntimeError, match="MTD_MSIL"):
        _resolve_sentinel_s2_dataset_ref(str(zip_path))


def test_s1_zip_resolves_to_manifest(tmp_path):
    zip_path = _write_zip(tmp_path / f"{S1_NAME}.zip", S1_NAME, ["manifest.safe"])
    assert _resolve_safe_manifest_path(str(zip_path)) == (
        f"/vsizip/{zip_path}/{S1_NAME}.SAFE/manifest.safe"
    )


def test_s1_renamed_zip_finds_the_safe_directory_inside(tmp_path):
    zip_path = _write_zip(tmp_path / "input_dataset.zip", S1_NAME, ["manifest.safe"])
    assert _resolve_safe_manifest_path(f"/vsizip/{zip_path}") == (
        f"/vsizip/{zip_path}/{S1_NAME}.SAFE/manifest.safe"
    )


def test_s1_safe_directory_resolves_to_manifest(tmp_path):
    safe_dir = tmp_path / f"{S1_NAME}.SAFE"
    assert _resolve_safe_manifest_path(f"{safe_dir}/") == f"{safe_dir}/manifest.safe"
