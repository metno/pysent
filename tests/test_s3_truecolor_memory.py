"""Truecolor without frame-sized temporaries: the same bits, fewer frames.

``toa_reflectance`` used to build a new frame per operation of
``pi * L / (F0 * cos(theta))`` (about seven on a full-resolution frame), and the
band loop kept a masked copy of every band beside it. Both now work in the
arrays they already hold, a strip of rows at a time, with the same float32
operations on the same operands in the same order. The rule is the one every
memory change here follows: **the same bits**. So the arithmetic is compared
bit for bit with the whole-frame expression it replaces, on synthetic frames
that carry every edge the expression has (missing radiance, a pixel with no
detector, the sun at or below the horizon, a missing zenith), and with strips
that do not divide the frame.
"""
import gc
import weakref

import numpy as np
import pytest

pytest.importorskip("osgeo")

from pysent import _olci_l1, _sen3  # noqa: E402
from pysent._olci_l1 import BAND_WAVELENGTHS, flux_row_for_band  # noqa: E402

SHAPE = (37, 23)
DETECTORS = 9


class _Band:
    def __init__(self, data):
        self.data = data
        self.YSize, self.XSize = data.shape

    def ReadAsArray(self, *args, **kwargs):  # noqa: N802 - GDAL's name
        return self.data.copy()


class _Dataset:
    def __init__(self, data):
        self.band = _Band(data)

    def GetRasterBand(self, index):  # noqa: N802 - GDAL's name
        return self.band


def _scene(table_dtype):
    """Radiance, flux table, wavelengths, detector index and zenith with every edge in them."""
    generator = np.random.default_rng(7)
    radiance = generator.uniform(0.0, 300.0, SHAPE).astype(np.float32)
    radiance[generator.random(SHAPE) < 0.05] = np.nan
    wavelengths = np.array(
        [[BAND_WAVELENGTHS[band] + generator.uniform(-1, 1) for _ in range(DETECTORS)]
         for band in range(21, 0, -1)],  # bottom-up, as the driver hands it over
        dtype=np.float32,
    )
    table = generator.uniform(900.0, 1900.0, (21, DETECTORS)).astype(table_dtype)
    detector = generator.integers(-1, DETECTORS + 2, SHAPE).astype(np.int16)
    # Up to 100 degrees: past 90 the cosine is negative, at 90 about zero.
    zenith = generator.uniform(0.0, 100.0, SHAPE).astype(np.float32)
    zenith[0, :4] = (90.0, 89.999, 0.0, 95.0)
    zenith[generator.random(SHAPE) < 0.03] = np.nan
    return radiance, table, wavelengths, detector, zenith


def _whole_frame(radiance, table, wavelengths, detector, zenith, band):
    """The expressions as they stood before, one new frame per operation."""
    row = table[flux_row_for_band(wavelengths, band)]
    inside = (detector >= 0) & (detector < row.size)
    flux = np.where(inside, row[np.clip(detector, 0, row.size - 1)], np.nan).astype(np.float32)
    with np.errstate(invalid="ignore", divide="ignore"):
        cosine = np.cos(np.radians(zenith.astype(np.float32)))
        reflectance = (np.pi * radiance) / (flux * cosine)
        reflectance = np.where(cosine > 0, reflectance, np.nan)
    return flux, reflectance.astype(np.float32)


def _serve(monkeypatch, radiance, table, wavelengths, detector):
    variables = {"solar_flux": table, "lambda0": wavelengths, "detector_index": detector,
                 "Oa08_radiance": radiance}
    monkeypatch.setattr(_olci_l1, "extract_member", lambda root, member, scratch: member)
    monkeypatch.setattr(_olci_l1, "open_sen3_variable", lambda local, variable: _Dataset(variables[variable]))
    monkeypatch.setattr(_olci_l1, "read_packed", lambda band, **kwargs: band.data.copy())


def _bits(array):
    return np.ascontiguousarray(array).view(np.uint32)


@pytest.mark.parametrize("table_dtype", [np.float32, np.float64])
def test_the_flux_is_bitwise_the_whole_frame_lookup(tmp_path, monkeypatch, table_dtype):
    radiance, table, wavelengths, detector, zenith = _scene(table_dtype)
    _serve(monkeypatch, radiance, table, wavelengths, detector)
    expected, _ = _whole_frame(radiance, table, wavelengths, detector, zenith, 8)

    flux = _olci_l1.solar_flux_per_pixel("root", tmp_path, 8, SHAPE)

    assert flux.dtype == np.float32
    assert np.array_equal(_bits(flux), _bits(expected))
    assert np.isnan(flux[(detector < 0) | (detector >= DETECTORS)]).all()


@pytest.mark.parametrize("strip_cells", [1 << 20, 5 * SHAPE[1] + 3, 1])
@pytest.mark.parametrize("table_dtype", [np.float32, np.float64])
def test_the_reflectance_is_bitwise_the_whole_frame_expression(tmp_path, monkeypatch, table_dtype, strip_cells):
    """Whole frame, strips that do not divide it, and a row at a time: the same bits."""
    radiance, table, wavelengths, detector, zenith = _scene(table_dtype)
    _serve(monkeypatch, radiance, table, wavelengths, detector)
    monkeypatch.setattr(_sen3, "_STRIP_CELLS", strip_cells)
    _, expected = _whole_frame(radiance, table, wavelengths, detector, zenith, 8)
    shared = zenith.copy()

    reflectance = _olci_l1.toa_reflectance("root", 8, tmp_path, solar_zenith_degrees=shared)

    assert reflectance.dtype == np.float32
    assert np.array_equal(_bits(reflectance), _bits(expected))
    # The bands of a scene share one zenith, so it must come back as it went in.
    assert np.array_equal(_bits(shared), _bits(zenith))
    # And the edges are what they were: no sun, no detector, no radiance - no value.
    with np.errstate(invalid="ignore"):
        below = ~(np.cos(np.radians(zenith)) > 0)
    assert np.isnan(reflectance[below]).all()
    assert np.isnan(reflectance[np.isnan(radiance)]).all()


# --------------------------------------------------------------------------- #
# End to end, on a real frame
# --------------------------------------------------------------------------- #
def _old_toa_reflectance(root, band, scratch, *, decimate=1, solar_zenith_degrees=None,
                         window=None, image_shape=None):
    """``toa_reflectance`` as it stood: every operation a new frame."""
    member, variable = _olci_l1.radiance_member(band)
    dataset = _olci_l1.open_sen3_variable(_olci_l1.extract_member(root, member, scratch), variable)
    source = dataset.GetRasterBand(1)
    full_shape = (max(1, source.YSize // max(1, int(decimate))),
                  max(1, source.XSize // max(1, int(decimate))))
    radiance = _olci_l1.read_packed(source, decimate=decimate, log10=False, window=window)
    source = dataset = None
    row, detector, outside = _olci_l1._flux_lookup(root, scratch, band, full_shape, window)
    flux = row[detector]
    flux[outside] = np.nan
    if solar_zenith_degrees is None:
        solar_zenith_degrees = _olci_l1.solar_zenith(root, scratch, full_shape, window)
    with np.errstate(invalid="ignore", divide="ignore"):
        cosine = np.cos(np.radians(solar_zenith_degrees.astype(np.float32)))
        reflectance = (np.pi * radiance) / (flux * cosine)
        reflectance = np.where(cosine > 0, reflectance, np.nan)
    return reflectance.astype(np.float32)


def _files(directory):
    return {path.name: path for path in directory.iterdir() if path.is_file()}


def test_true_colour_writes_the_same_bytes_as_the_whole_frame_arithmetic(olci_l1_archive, tmp_path, monkeypatch):
    """Needs a real Level-1 frame (``PYSENT_OLCI_ARCHIVE``): no window of one is committed."""
    import filecmp

    from pysent.s3 import process_sentinel_s3_safe

    source = sorted(olci_l1_archive.glob("*OL_1_E*R*.zip"))[0]
    options = {"preset": "quicklook", "gdal_cachemax_mb": 256}

    def run(name):
        out = tmp_path / name
        process_sentinel_s3_safe(input_dataset=str(source), output_dir=out, products={"truecolor": {}},
                                 processing_options={**options, "work_dir": str(tmp_path / f"{name}-work")})
        return _files(out)

    now = run("now")
    with monkeypatch.context() as patch:
        patch.setattr(_olci_l1, "toa_reflectance", _old_toa_reflectance)
        before = run("before")
    assert sorted(now) == sorted(before) and now
    for name in now:
        assert filecmp.cmp(now[name], before[name], shallow=False), name


def test_each_truecolor_band_is_freed_by_its_warp(olci_l1_archive, tmp_path, monkeypatch):
    """The layers are handed over: once a warp returns, nothing holds its band.

    And the last warp takes the geolocation as well, so nothing of the swath is
    alive once the warps are done.
    """
    import pysent.s3 as s3

    source = sorted(olci_l1_archive.glob("*OL_1_E*R*.zip"))[0]
    real = s3.warp_swath
    seen = []

    def spy(**kwargs):
        swath = kwargs["swath"]
        values = weakref.ref(swath.values)
        lat, lon = weakref.ref(swath.lat), weakref.ref(swath.lon)
        result = real(**kwargs)
        gc.collect()
        seen.append((values() is None, lat() is None, lon() is None))
        return result

    monkeypatch.setattr(s3, "warp_swath", spy)
    s3.process_sentinel_s3_safe(
        input_dataset=str(source), output_dir=tmp_path / "out", products={"truecolor": {}},
        processing_options={"preset": "quicklook", "gdal_cachemax_mb": 256,
                            "styled": False, "thumbnail": False},
    )
    assert [freed for freed, _, _ in seen] == [True, True, True]
    assert seen[-1][1:] == (True, True)
    assert seen[0][1:] == (False, False), "the geolocation is kept for the next band's warp"
