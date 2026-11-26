"""The correction in row strips and inside a window gives the whole frame's values.

Every step of the correction is per pixel, so cutting the frame into strips, or
correcting only a bbox's window, must change no value: these compare the bits
(``.view`` of an unsigned type, so a NaN and a signed zero count) of every
output against a single call on the whole frame. The readers are replaced by
cuts of synthetic whole-frame fields - the readers' own windows are checked in
``test_s3_read_window.py`` - so what is tested here is the strip logic itself.
"""
from __future__ import annotations

import numpy as np
import pytest

from pysent import _olci_correction
from pysent._aerosol_retrieval import RETRIEVAL_BANDS
from pysent._atmosphere import atmosphere_terms
from pysent._olci_correction import correct_frame, empty_correction

NAME = "S3A_OL_1_EFR____20260615T095911_20260615T100211_synthetic.SEN3"
SHAPE = (23, 9)
BANDS = [8, 6, 4]


def _frame():
    """Geometry, columns, masks and TOA bands of a small frame with water, land and haze."""
    rows, columns = np.mgrid[0 : SHAPE[0], 0 : SHAPE[1]].astype(np.float64)
    atmosphere = {
        "solar_zenith_deg": (60.0 + rows * 1.2 + columns * 0.1).astype(np.float32),
        "view_zenith_deg": (2.0 + columns * 4.0).astype(np.float32),
        "relative_azimuth_deg": 40.0 + rows * 3.0 + columns * 2.0,
        "pressure_hpa": 1013.0 - columns * 3.0 - rows * 0.5,
        "ozone_kg_m2": (0.006 + rows * 1e-5).astype(np.float32),
        "water_kg_m2": (5.0 + columns * 0.3).astype(np.float32),
    }
    geometry = {key: np.asarray(value, dtype=np.float64) for key, value in atmosphere.items()}
    land = (columns > 5) | (rows < 2)
    bright = (rows == 7) & (columns < 3)
    rejected = (rows == 11) & (columns == 4)
    random = np.random.default_rng(7)
    depth = 0.05 + 0.5 * random.random(SHAPE)  # some past the table's top
    angstrom = 0.2 + 1.2 * random.random(SHAPE)
    toa = {}
    for band in dict.fromkeys([*BANDS, *RETRIEVAL_BANDS]):
        terms = atmosphere_terms(
            "S3A", band, optical_depth_550=depth, angstrom=angstrom, **geometry
        )
        surface = np.where(land, 0.12 + 0.01 * band, 0.002 * random.random(SHAPE))
        surface = np.where(bright, 0.8, surface)
        # The coupled form a surface under an atmosphere gives, so the water is
        # dark enough for the retrieval to invert and the land is not.
        trans = terms["transmittance_down"] * terms["transmittance_up"]
        albedo = terms["spherical_albedo"]
        value = terms["gas_transmittance"] * (
            terms["path_reflectance"] + trans * surface / (1.0 - albedo * surface)
        )
        value[(rows == 3) & (columns == 1)] = np.nan  # a missing input
        toa[band] = value.astype(np.float32)
    return atmosphere, toa, land, bright, rejected


@pytest.fixture
def frame(monkeypatch):
    atmosphere, toa, land, bright, rejected = _frame()
    calls = []

    def fake_atmosphere(root, scratch, shape, *, decimate, window=None):
        assert tuple(shape) == SHAPE
        calls.append(("atmosphere", window))
        cut = window or (slice(None), slice(None))
        return {key: values[cut].copy() for key, values in atmosphere.items()}

    def fake_toa(root, band, scratch, *, decimate=1, solar_zenith_degrees=None, window=None, image_shape=None):
        assert tuple(image_shape) == SHAPE
        calls.append(("toa", window))
        cut = window or (slice(None), slice(None))
        values = toa[band][cut].copy()
        assert solar_zenith_degrees.shape == values.shape
        return values

    monkeypatch.setattr(_olci_correction, "frame_atmosphere", fake_atmosphere)
    monkeypatch.setattr(_olci_correction, "toa_reflectance", fake_toa)
    return {"land": land, "bright": bright, "rejected": rejected,
            "zenith": atmosphere["solar_zenith_deg"], "calls": calls}


def _run(frame, monkeypatch, *, read, compute, window=None, **request):
    monkeypatch.setattr(_olci_correction, "READ_STRIP_CELLS", read)
    monkeypatch.setattr(_olci_correction, "COMPUTE_STRIP_CELLS", compute)
    cut = window or (slice(None), slice(None))
    return correct_frame(
        NAME, "root", None, bands=BANDS, shape=SHAPE, decimate=1,
        solar_zenith_for_toa=frame["zenith"][cut],
        land=frame["land"][cut], bright=frame["bright"][cut], rejected=frame["rejected"][cut],
        window=window, **request,
    )


def _bits(array):
    array = np.ascontiguousarray(array)
    if array.dtype == bool:
        return array.view(np.uint8)
    return array.view(f"u{array.dtype.itemsize}")


def _arrays(result):
    """Every array of a correction's result, by a name that says where it is."""
    out = {f"surface {band}": values for band, values in result["surfaces"].items()}
    out.update({f"uncertainty {band}": values for band, values in result["uncertainties"].items()})
    out["quality"] = result["quality"]
    loading = result["loading"]
    if loading is not None:
        for key in ("optical_depth_550", "angstrom", "source", "haze_beyond_table"):
            out[f"loading {key}"] = loading[key]
        out.update({f"refused {name}": values for name, values in loading["refused"].items()})
    return out


def _assert_same(first, second, cut=(slice(None), slice(None))):
    a, b = _arrays(first), _arrays(second)
    assert a.keys() == b.keys()
    for name in a:
        assert a[name].dtype == b[name].dtype, name
        assert np.array_equal(_bits(a[name]), _bits(b[name][cut])), name


REQUESTS = [
    {"tier": "full", "aerosol": "retrieve"},
    {"tier": "full", "aerosol": "climatological"},
    {"tier": "full", "aerosol": 0.2},
    {"tier": "full", "aerosol": (0.5, 1.1)},
    {"tier": "rayleigh"},
    {"tier": "gas"},
]


@pytest.mark.parametrize("request_", REQUESTS, ids=lambda r: "-".join(map(str, r.values())))
@pytest.mark.parametrize("read, compute", [(40, 11), (9, 1), (100, 100), (19, 9)])
def test_strips_give_the_whole_frames_bits(frame, monkeypatch, request_, read, compute):
    whole = _run(frame, monkeypatch, read=10**9, compute=10**9, **request_)
    strips = _run(frame, monkeypatch, read=read, compute=compute, **request_)
    _assert_same(strips, whole)
    assert strips["provenance"] == whole["provenance"]
    if request_.get("aerosol") == "retrieve":
        # The synthetic frame does exercise the retrieval and its refusals.
        assert (whole["loading"]["source"] == 0).any() and (whole["loading"]["source"] == 2).any()
        assert any(values.any() for values in whole["loading"]["refused"].values())
    if request_.get("aerosol") == (0.5, 1.1):
        assert whole["loading"]["haze_beyond_table"].any()


def test_a_strip_reads_only_its_rows(frame, monkeypatch):
    _run(frame, monkeypatch, read=4 * SHAPE[1], compute=SHAPE[1], tier="full", aerosol="retrieve")
    windows = [window for kind, window in frame["calls"] if kind == "atmosphere"]
    assert [(rows.start, rows.stop) for rows, _ in windows] == [(0, 4), (4, 8), (8, 12), (12, 16), (16, 20), (20, 23)]
    # Each read strip reads each band once: the three corrected and band 17 (band 8 is both).
    assert sum(kind == "toa" for kind, _ in frame["calls"]) == 6 * 4


@pytest.mark.parametrize("request_", REQUESTS[:3], ids=lambda r: "-".join(map(str, r.values())))
def test_a_window_is_the_whole_frame_cut(frame, monkeypatch, request_):
    window = (slice(5, 17), slice(2, 8))
    whole = _run(frame, monkeypatch, read=10**9, compute=10**9, **request_)
    inside = _run(frame, monkeypatch, read=20, compute=7, window=window, **request_)
    _assert_same(inside, whole, window)
    for kind, read in frame["calls"][-6:]:
        assert read[1] == window[1] and 5 <= read[0].start < read[0].stop <= 17
    # The provenance medians are the window's own (the plan's Q13), the rest is the same.
    loading = whole["loading"]
    expected = dict(whole["provenance"])
    expected["aerosol_optical_depth_550_median"] = (
        f"{float(np.nanmedian(loading['optical_depth_550'][window])):.4f}"
    )
    expected["aerosol_angstrom_median"] = f"{float(np.nanmedian(loading['angstrom'][window])):.3f}"
    assert inside["provenance"] == expected


@pytest.mark.parametrize("request_", REQUESTS, ids=lambda r: "-".join(map(str, r.values())))
def test_an_empty_window_is_what_a_corrected_frame_cut_to_nothing_was(frame, monkeypatch, request_):
    """``main`` corrected the whole frame, then cut it to the empty mesh with ``_SwathCrop.nested``."""
    from pysent.s3 import _SwathCrop

    rows, columns = np.mgrid[0 : SHAPE[0], 0 : SHAPE[1]].astype(np.float32)
    crop = _SwathCrop(60.0 + rows * 0.01, 5.0 + columns * 0.01, product="surface_reflectance",
                      settings={"bbox": (100.0, -10.0, 110.0, 0.0), "on_empty": "write"})
    assert crop.outside
    cut = crop.nested(_run(frame, monkeypatch, read=10**9, compute=10**9, **request_))
    empty = empty_correction(NAME, bands=BANDS, shape=crop.lat.shape, **request_)
    _assert_same(empty, cut)
    assert empty["loading"] is None or (
        empty["loading"]["source_names"] == cut["loading"]["source_names"]
        and empty["loading"]["table_ceiling"] == cut["loading"]["table_ceiling"]
    )
    expected = dict(cut["provenance"])
    if "aerosol_optical_depth_550_median" in expected:
        expected["aerosol_optical_depth_550_median"] = expected["aerosol_angstrom_median"] = "none"
    assert empty["provenance"] == expected


def test_the_full_tier_without_a_source_is_refused_before_anything_is_read(frame, monkeypatch):
    with pytest.raises(ValueError, match="needs an aerosol source"):
        _run(frame, monkeypatch, read=10**9, compute=10**9, tier="full")
    with pytest.raises(ValueError, match="needs an aerosol source"):
        empty_correction(NAME, bands=BANDS, shape=(2, 2), tier="full")
    assert frame["calls"] == []
