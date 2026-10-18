"""A product says how it was made (issue #14).

Given only the GeoTIFF, it must be possible to tell which warp transform, stretch and speckle
settings produced it, and which pysent wrote it. Before this, pysent wrote no provenance at all,
and ``use_tps`` did not even reach the return value. The settings are stamped as ``PYSENT_*``
items in the default GDAL metadata domain, which plain ``gdalinfo`` shows, and the same mapping
is returned as ``provenance`` beside each product.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
gdal = pytest.importorskip("osgeo.gdal")

import pysent  # noqa: E402
from pysent import _runtime, s1, s2  # noqa: E402

from test_bulk_robustness import _write_raster, fake_s2_warp, run_s1_safe, run_s2  # noqa: E402,F401


def _stamped(path: str | Path) -> dict[str, str]:
    dataset = gdal.Open(str(path))
    try:
        return {k: v for k, v in (dataset.GetMetadata() or {}).items() if k.startswith(("PYSENT_", "TIFFTAG_SOFTWARE"))}
    finally:
        dataset = None


def test_an_s2_product_records_its_stretch_grid_and_version(tmp_path, fake_s2_warp):
    fake_s2_warp()
    result = run_s2(tmp_path / "out")[0]
    stamped = _stamped(result["path"])
    assert stamped == result["provenance"], "the file and the result say the same thing"
    assert stamped["PYSENT_VERSION"] == pysent.__version__
    assert stamped["TIFFTAG_SOFTWARE"] == f"pysent {pysent.__version__}"
    assert stamped["PYSENT_STRETCH_METHOD"] == "percentile"
    assert stamped["PYSENT_STRETCH_PERCENTILES"] == "0.5,99.5"
    assert stamped["PYSENT_STRETCH_GAMMA"] == repr(float(s2.S2_STRETCH_GAMMA))
    assert stamped["PYSENT_TARGET_EPSG"] == str(result["target_epsg"])
    assert stamped["PYSENT_PRESET"] == "full"
    assert not any(k.startswith(("PYSENT_WARP", "PYSENT_SPECKLE")) for k in stamped), "S1-only settings stay out"


def test_an_unstretched_s2_product_says_so(tmp_path, fake_s2_warp):
    fake_s2_warp()
    result = run_s2(tmp_path / "out", histogram_stretch=False)[0]
    stamped = _stamped(result["path"])
    assert stamped["PYSENT_STRETCH_METHOD"] == "none"
    assert "PYSENT_STRETCH_PERCENTILES" not in stamped and "PYSENT_STRETCH_GAMMA" not in stamped
    assert stamped == result["provenance"]


def test_the_quicklook_preset_is_recorded(tmp_path, fake_s2_warp):
    fake_s2_warp()
    result = run_s2(tmp_path / "out", preset="quicklook")[0]
    assert _stamped(result["path"])["PYSENT_PRESET"] == "quicklook"
    assert _stamped(result["path"])["PYSENT_TARGET_RESOLUTION"] == "60.0"


@pytest.mark.parametrize("use_tps, transform", [(True, "thin-plate-spline"), (False, "polynomial-gcp")])
def test_an_s1_safe_product_records_its_warp_transform(tmp_path, monkeypatch, use_tps, transform):
    """The warp choice moves pixels (1.54 px RMS at 40 m) and used to leave no trace anywhere."""
    result = run_s1_safe(tmp_path / "out", monkeypatch, [], use_tps=use_tps)[0]
    stamped = _stamped(result["path"])
    assert stamped["PYSENT_WARP_TRANSFORM"] == transform
    assert result["provenance"]["PYSENT_WARP_TRANSFORM"] == transform
    assert stamped == result["provenance"]
    assert stamped["PYSENT_STRETCH_METHOD"] == "db"
    assert stamped["PYSENT_SPECKLE_FILTER"] == "none"
    assert "PYSENT_SPECKLE_WINDOW" not in stamped
    assert stamped["PYSENT_TARGET_EPSG"] == "EPSG:32661" and stamped["PYSENT_TARGET_RESOLUTION"] == "40.0"


def test_an_s1_netcdf_product_has_no_warp_transform_to_record(tmp_path, monkeypatch):
    def fake_warp(input_dataset, variable, warped_path, **kwargs):
        _write_raster(Path(warped_path), np.random.default_rng(0).random((64, 64), dtype=np.float32) + 1)

    monkeypatch.setattr(s1, "_warp_sentinel_s1_amplitude", fake_warp)
    result = s1.process_sentinel_s1_netcdf(
        input_dataset="unused.nc", output_dir=tmp_path / "out", output_names={"Amplitude_VV": "vv.tif"},
    )[0]
    stamped = _stamped(result["path"])
    assert "PYSENT_WARP_TRANSFORM" not in stamped
    assert stamped["PYSENT_VERSION"] == pysent.__version__


def test_the_lee_filter_records_its_window_and_the_looks_it_used(tmp_path, monkeypatch):
    result = run_s1_safe(tmp_path / "out", monkeypatch, [], speckle_filter="lee", speckle_window=7)[0]
    stamped = _stamped(result["path"])
    assert stamped["PYSENT_SPECKLE_FILTER"] == "lee"
    assert stamped["PYSENT_SPECKLE_WINDOW"] == "7"
    assert stamped["PYSENT_SPECKLE_LOOKS"] == repr(result["speckle"]["looks"])


def test_the_stamp_survives_a_byte_copy(tmp_path, monkeypatch):
    """A consuming service moves products into place with shutil.copy2."""
    result = run_s1_safe(tmp_path / "out", monkeypatch, [])[0]
    copy = tmp_path / "copied.tif"
    shutil.copy2(result["path"], copy)
    assert _stamped(copy) == result["provenance"]


def test_the_file_is_stamped_before_it_appears_under_its_final_name(tmp_path, monkeypatch, fake_s2_warp):
    """Stamping happens inside atomic_output: no moment where the final file lacks it."""
    seen: list[dict[str, str]] = []
    real_replace = _runtime.os.replace

    def replace(src, dst):
        seen.append(_stamped(src))
        return real_replace(src, dst)

    monkeypatch.setattr(_runtime.os, "replace", replace)
    fake_s2_warp()
    run_s2(tmp_path / "s2")
    run_s1_safe(tmp_path / "s1", monkeypatch, [])
    assert len(seen) == 2 and all(s.get("PYSENT_VERSION") == pysent.__version__ for s in seen)


def test_provenance_formats_values_and_leaves_out_what_does_not_apply():
    stamped = _runtime.provenance(stretch_percentiles=(1.0, 99.0), speckle_window=None, use_x=True, target_resolution=40.0)
    assert stamped["PYSENT_STRETCH_PERCENTILES"] == "1.0,99.0"
    assert stamped["PYSENT_USE_X"] == "true"
    assert stamped["PYSENT_TARGET_RESOLUTION"] == "40.0"
    assert "PYSENT_SPECKLE_WINDOW" not in stamped
