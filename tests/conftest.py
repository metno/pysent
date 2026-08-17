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
