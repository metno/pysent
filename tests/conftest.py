"""Shared fixtures, including the committed real-Sentinel test data.

The artifacts under ``tests/data/`` are small windows cut from genuine recent
Sentinel-1 and Sentinel-2 scenes over Norway (see ``scripts/make_test_data.py``
and ``tests/data/manifest.json`` for their provenance). They let the stretch and
statistics paths be exercised against real radiometry - speckle, partial swaths,
bright outliers - which synthetic arrays do not reproduce.

Tests that need them skip cleanly when the directory is absent, so a checkout
without the artifacts still runs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

DATA_DIR = Path(__file__).parent / "data"
MANIFEST = DATA_DIR / "manifest.json"


@pytest.fixture(scope="session")
def data_manifest() -> dict:
    """Provenance manifest for the committed Sentinel artifacts."""
    if not MANIFEST.exists():
        pytest.skip("tests/data/manifest.json missing - run scripts/make_test_data.py")
    return json.loads(MANIFEST.read_text())


def _artifact(manifest: dict, platform: str, key: str) -> Path:
    try:
        name = manifest["products"][platform]["artifacts"][key]["path"]
    except KeyError:
        pytest.skip(f"no {platform}/{key} artifact in manifest")
    path = DATA_DIR / name
    if not path.exists():
        pytest.skip(f"{path.name} missing - run scripts/make_test_data.py")
    return path


@pytest.fixture(scope="session")
def s1_vv(data_manifest) -> Path:
    """512x512 UInt16 window of real Sentinel-1 VV amplitude."""
    return _artifact(data_manifest, "S1", "Amplitude_VV")


@pytest.fixture(scope="session")
def s1_vh(data_manifest) -> Path:
    """512x512 UInt16 window of real Sentinel-1 VH amplitude."""
    return _artifact(data_manifest, "S1", "Amplitude_VH")


@pytest.fixture(scope="session")
def s2_rgb(data_manifest) -> Path:
    """512x512 UInt16 3-band (B4/B3/B2) window of a real Sentinel-2 scene."""
    return _artifact(data_manifest, "S2", "rgb")


@pytest.fixture(scope="session")
def s5p_no2_window() -> Path:
    """60 scanlines of a real Sentinel-5P NO2 granule, as a tiny NetCDF.

    Keeps the product's group layout and packing - ``/PRODUCT`` arrays, the
    byte-packed ``qa_value`` with its 0.01 scale, the real fill values - so the
    masking and unpacking paths see what a real file gives them.
    """
    path = DATA_DIR / "s5p_no2_window.nc"
    if not path.exists():
        pytest.skip("s5p_no2_window.nc missing - run scripts/make_test_data.py --platform s5p")
    return path


@pytest.fixture(scope="session")
def s5p_archive() -> Path:
    """A directory of full-size real Sentinel-5P granules, one per product type.

    These are too large to commit (4-600 MB each), so the tests that need them
    skip unless ``PYSENT_S5P_ARCHIVE`` points at a directory holding them. Fetch
    them with the titles in ``PLANNING/TODO_PLANNING_s5p_s3_pipelines.md`` B.7.
    """
    import os

    location = os.environ.get("PYSENT_S5P_ARCHIVE")
    if not location:
        pytest.skip("set PYSENT_S5P_ARCHIVE to a directory of real S5P granules")
    path = Path(location)
    if not path.is_dir() or not any(path.glob("S5P_*.nc")):
        pytest.skip(f"{path} holds no S5P_*.nc granules")
    return path


@pytest.fixture(scope="session")
def s3_synergy_window() -> Path:
    """A 384x384 window of a real Synergy VG1 tile, as a small STORED zip.

    Keeps the ``.SEN3`` layout, the member names, the byte packing, the full
    flag table and a manifest, so the reader's archive path, flag selection and
    unpacking are all exercised without the 143 MB product.
    """
    path = DATA_DIR / "s3_synergy_window.zip"
    if not path.exists():
        pytest.skip("s3_synergy_window.zip missing - run scripts/make_test_data.py --platform s3")
    return path


@pytest.fixture(scope="session")
def s3_olci_window() -> Path:
    """A 320x320 water window of a real OLCI WFR frame, as a small STORED zip.

    Keeps the Int32 micro-degree geolocation, the log10-packed science variable
    and the 64-bit quality word with its full flag table, so the swath warp,
    the unpacking and the keep/reject mask are all exercised offline.
    """
    path = DATA_DIR / "s3_olci_window.zip"
    if not path.exists():
        pytest.skip("s3_olci_window.zip missing - run scripts/make_test_data.py --platform s3")
    return path


@pytest.fixture(scope="session")
def s3_slstr_lst_window() -> Path:
    """A 256x256 window of a real SLSTR land-surface-temperature frame.

    Carries all three members the product needs - the Int16 temperature in
    kelvin with its exception word, the Int32 micro-degree geolocation, and the
    16-flag confidence word - so the two-source mask and the kelvin-to-celsius
    conversion are exercised offline.
    """
    path = DATA_DIR / "s3_slstr_lst_window.zip"
    if not path.exists():
        pytest.skip("s3_slstr_lst_window.zip missing - run scripts/make_test_data.py --platform s3")
    return path


@pytest.fixture(scope="session")
def s3_slstr_sst_window() -> Path:
    """A 256x256 window of a real SLSTR sea-surface-temperature frame.

    The member keeps its generated GHRSST name, so the pattern lookup that
    tells the two-view retrieval from the nadir-only one is exercised too.
    """
    path = DATA_DIR / "s3_slstr_sst_window.zip"
    if not path.exists():
        pytest.skip("s3_slstr_sst_window.zip missing - run scripts/make_test_data.py --platform s3")
    return path
