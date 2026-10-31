"""OLCI Level-1 true colour: the reflectance conversion and the composite.

Most of this phase cannot be tested from a committed fixture. A Level-1 product
needs the radiance band, a 21 x 3700 solar-flux table, a per-pixel detector
index, a tie-point solar-zenith grid and a quality word, at a frame size of
19.9 megapixels - a window of it large enough to be meaningful is not a
committed artifact. So the arithmetic is tested on synthetic arrays, the
composite on real numbers, and the end-to-end path behind the archive fixture
that the other real-file tests use.
"""
import numpy as np
import pytest

pytest.importorskip("osgeo")

from osgeo import gdal  # noqa: E402

from pysent._olci_l1 import BAND_WAVELENGTHS, radiance_member  # noqa: E402
from pysent._output import rgb_bytes  # noqa: E402
from pysent.s3 import (  # noqa: E402
    S3_PRESETS,
    S3_PRODUCTS,
    S3_TYPE_PRODUCTS,
    _resolve_processing_settings,
    normalize_sentinel_s3_product_map,
)


# --------------------------------------------------------------------------- #
# The composite
# --------------------------------------------------------------------------- #
def test_gamma_below_one_lifts_the_dark_end():
    """Without it the sea is black and the land nearly black under white cloud.

    Measured on a real frame: reflectance over open water sits at 0.04-0.06
    while cloud reaches 0.5 and above, so a linear ramp spends almost all of
    its levels on cloud.
    """
    band = np.array([[0.0, 0.06, 0.3, 0.6]], dtype=np.float32)
    linear = rgb_bytes([band] * 3, (0.0, 0.6), gamma=1.0)[0, :, 0]
    lifted = rgb_bytes([band] * 3, (0.0, 0.6), gamma=0.6)[0, :, 0]

    assert linear[1] < lifted[1], "gamma below one should brighten a dark pixel"
    # Water at 0.06 goes from the bottom tenth of the ramp to about a quarter.
    assert linear[1] < 40 and lifted[1] > 55
    # The ends are unmoved.
    assert linear[0] == lifted[0] == 1
    assert linear[3] == lifted[3] == 255


def test_reflectance_above_the_range_clips_to_white():
    """Which is the right answer for cloud, including the cloud whose
    top-of-atmosphere reflectance genuinely exceeds one."""
    band = np.array([[0.6, 1.0, 1.7]], dtype=np.float32)
    out = rgb_bytes([band] * 3, (0.0, 0.6), gamma=0.6)
    assert out[0, :, 0].tolist() == [255, 255, 255]


def test_a_missing_pixel_is_zero_in_every_channel():
    band = np.array([[np.nan, 0.3]], dtype=np.float32)
    out = rgb_bytes([band] * 3, (0.0, 0.6))
    assert out[0, 0].tolist() == [0, 0, 0]
    assert all(value > 0 for value in out[0, 1])


def test_the_three_channels_are_independent():
    """A composite of three different bands must not collapse to grey."""
    red = np.full((1, 1), 0.5, dtype=np.float32)
    green = np.full((1, 1), 0.2, dtype=np.float32)
    blue = np.full((1, 1), 0.05, dtype=np.float32)
    out = rgb_bytes([red, green, blue], (0.0, 0.6), gamma=0.6)[0, 0]
    assert out[0] > out[1] > out[2]


@pytest.mark.parametrize(
    "bands, value_range, gamma, message",
    [
        ([np.zeros((2, 2), dtype=np.float32)] * 2, (0.0, 1.0), 1.0, "needs three bands"),
        ([np.zeros((2, 2), dtype=np.float32)] * 3, (1.0, 0.0), 1.0, "low < high"),
        ([np.zeros((2, 2), dtype=np.float32)] * 3, (0.0, 1.0), 0.0, "gamma must be positive"),
    ],
)
def test_a_bad_composite_request_is_refused(bands, value_range, gamma, message):
    with pytest.raises(ValueError, match=message):
        rgb_bytes(bands, value_range, gamma=gamma)


# --------------------------------------------------------------------------- #
# The product
# --------------------------------------------------------------------------- #
def test_true_colour_names_three_real_bands():
    entry = S3_PRODUCTS["truecolor"]
    assert entry["kind"] == "rgb"
    assert entry["bands"] == (8, 6, 4)
    # Descending wavelength: red, green, blue.
    wavelengths = [BAND_WAVELENGTHS[band] for band in entry["bands"]]
    assert wavelengths == sorted(wavelengths, reverse=True)
    assert wavelengths == [665, 560, 490]


def test_the_radiance_member_follows_the_band_number():
    assert radiance_member(8) == ("Oa08_radiance.nc", "Oa08_radiance")
    assert radiance_member(21) == ("Oa21_radiance.nc", "Oa21_radiance")
    with pytest.raises(ValueError, match="band must be 1 to 21"):
        radiance_member(22)


def test_the_level_one_types_route_to_true_colour():
    assert S3_TYPE_PRODUCTS["OL_1_EFR"] == ("truecolor",)
    assert S3_TYPE_PRODUCTS["OL_1_ERR"] == ("truecolor",)
    from pysent.profiles import supported_sentinel_product_types

    assert "S3 OL_1_EFR" in supported_sentinel_product_types()


def test_the_range_and_gamma_are_overridable():
    options = normalize_sentinel_s3_product_map(
        {"truecolor": {"value_range": (0.0, 0.4), "gamma": 0.8}}
    )["truecolor"]
    assert options["value_range"] == (0.0, 0.4)
    assert options["gamma"] == 0.8


# --------------------------------------------------------------------------- #
# A quicklook has to coarsen the grid, not only the read
# --------------------------------------------------------------------------- #
def test_the_quicklook_preset_coarsens_the_output_grid():
    """Two different things get coarser and only one is obvious.

    ``decimate`` reads fewer source pixels. But on a swath the output grid
    comes from the product's ground sampling, so decimating the read alone
    leaves a raster of exactly the same size, only blurrier - and for three
    bands that raster is over the size cap. ``grid_multiplier`` is what makes
    the output smaller.
    """
    assert S3_PRESETS["quicklook"]["grid_multiplier"] > 1
    assert S3_PRESETS["quicklook"]["decimate"] > 1
    assert _resolve_processing_settings({})["grid_multiplier"] == 1.0
    assert _resolve_processing_settings({"preset": "quicklook"})["grid_multiplier"] == 8.0


def test_a_grid_multiplier_below_one_is_refused():
    with pytest.raises(ValueError, match="grid_multiplier must be 1 or more"):
        _resolve_processing_settings({"grid_multiplier": 0.5})


# --------------------------------------------------------------------------- #
# Against a real Level-1 product, when one is available
# --------------------------------------------------------------------------- #
def test_true_colour_from_a_real_frame(s5p_archive, tmp_path):
    """The whole path, on the archive fixture the other real-file tests use.

    Checks the two things that matter and cannot be checked synthetically: that
    the reflectance lands in the physically expected range over water, and that
    every value above one is in a pixel the product itself calls bright.
    """
    from pysent.s3 import process_sentinel_s3_safe

    candidates = sorted(s5p_archive.glob("*OL_1_EFR*.zip"))
    if not candidates:
        pytest.skip("no OL_1_EFR product in the archive directory")

    result = process_sentinel_s3_safe(
        input_dataset=str(candidates[0]), output_dir=tmp_path, products={"truecolor": {}},
        processing_options={"preset": "quicklook", "gdal_cachemax_mb": 256},
    )[0]

    assert result["bands"] == ["Oa08", "Oa06", "Oa04"]
    assert result["wavelengths_nm"] == [665, 560, 490]
    statistics = result["value_stats"]
    # Top-of-atmosphere reflectance: never negative, and a scene with any cloud
    # in it has a median well above a dark ocean's 0.05.
    assert statistics["min"] >= 0.0
    assert 0.0 < statistics["p50"] < 1.0
    # The frame carries cloud, and the product says so.
    assert result["bright_fraction"] > 0.0

    styled = gdal.Open(result["styled_path"])
    assert styled.RasterCount == 4, "three colour bands and an alpha band"
    assert styled.GetRasterBand(1).DataType == gdal.GDT_Byte
    styled = None

    floats = gdal.Open(result["path"])
    assert floats.RasterCount == 3
    assert "toa_reflectance" in floats.GetRasterBand(1).GetDescription()
    floats = None
    # The provenance has to say the reflectance was derived here, not measured.
    assert "derived" in result["provenance"]["PYSENT_VARIABLE"]


# --------------------------------------------------------------------------- #
# The solar flux table's orientation (review-study item R1)
# --------------------------------------------------------------------------- #
def _instrument_member(path, *, rows=21, detectors=8):
    """A minimal ``instrument_data.nc`` with its rows in **file order**.

    Row 0 is band 1 at 400 nm and row 20 is band 21 at 1020 nm, exactly as a
    real product stores them. GDAL's netCDF driver hands such a table back
    bottom-up, which is the whole point of the test: whatever the driver does
    to the row order, band 4 must be divided by band 4's flux.

    The flux of band *n* is ``1000 + n``, so a wrong row is obvious in the
    value rather than only in a ratio.
    """
    from pysent._olci_l1 import BAND_WAVELENGTHS

    wavelengths = np.array(
        [[float(BAND_WAVELENGTHS[band]) for _ in range(detectors)] for band in range(1, rows + 1)],
        dtype=np.float32,
    )
    flux = np.array(
        [[1000.0 + band for _ in range(detectors)] for band in range(1, rows + 1)], dtype=np.float32
    )
    detector = np.tile(np.arange(detectors, dtype=np.int16), (3, 1))

    dataset = gdal.GetDriverByName("netCDF").CreateMultiDimensional(str(path))
    root = dataset.GetRootGroup()
    band_dimension = root.CreateDimension("bands", None, None, rows)
    detector_dimension = root.CreateDimension("detectors", None, None, detectors)
    row_dimension = root.CreateDimension("rows", None, None, detector.shape[0])
    float32 = gdal.ExtendedDataType.Create(gdal.GDT_Float32)
    for name, data in (("lambda0", wavelengths), ("solar_flux", flux)):
        array = root.CreateMDArray(name, [band_dimension, detector_dimension], float32)
        array.Write(data.tobytes())
    index = root.CreateMDArray(
        "detector_index", [row_dimension, detector_dimension],
        gdal.ExtendedDataType.Create(gdal.GDT_Int16),
    )
    index.Write(detector.tobytes())
    dataset = None
    return detector.shape


def test_the_flux_row_is_matched_by_wavelength_not_by_band_number(tmp_path):
    """Regression: the flux table comes back bottom-up, so ``band - 1`` is band ``22 - band``.

    Written as the pictures' own arithmetic rather than as a driver detail: on
    ``main`` the blue band was divided by the flux of band 18, which made it
    2.10x too bright and gave every true-colour picture a blue cast.
    """
    from pysent._olci_l1 import flux_row_for_band

    member = tmp_path / "instrument_data.nc"
    _instrument_member(member)

    dataset = gdal.Open(f'NETCDF:"{member}":lambda0')
    wavelengths = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    dataset = gdal.Open(f'NETCDF:"{member}":solar_flux')
    table = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None

    # Every band finds the flux that belongs to it, whichever way the driver
    # ordered the rows.
    for band in (4, 6, 8, 1, 21):
        row = flux_row_for_band(wavelengths, band)
        assert table[row][0] == pytest.approx(1000.0 + band), band

    # And the old arithmetic really is wrong on this file, which is what makes
    # this a regression test and not a tautology.
    assert flux_row_for_band(wavelengths, 4) != 4 - 1
    assert table[4 - 1][0] == pytest.approx(1000.0 + 18)


def test_a_product_whose_wavelengths_do_not_match_is_refused(tmp_path):
    """A table that cannot be matched to the bands must say so, not guess a row."""
    from pysent._olci_l1 import flux_row_for_band

    wavelengths = np.full((21, 4), 300.0, dtype=np.float32)
    with pytest.raises(ValueError, match="within 12 nm of band 4"):
        flux_row_for_band(wavelengths, 4)


def _instrument_product(path, tmp_path):
    """A stub SEN3 zip holding only ``instrument_data.nc``.

    Enough for the flux lookup, which reads that member and nothing else.
    """
    import zipfile

    member = tmp_path / "instrument_data.nc"
    shape = _instrument_member(member)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.write(member, "STUB.SEN3/instrument_data.nc")
        archive.writestr("STUB.SEN3/xfdumanifest.xml", b"<xfdu:XFDU/>")
    return shape


def test_each_band_is_divided_by_its_own_flux(tmp_path):
    """Regression, through the function the pipeline actually calls.

    On ``main`` this returns band 18's flux for band 4, because the row was
    taken as ``band - 1`` from a table the driver hands back bottom-up.
    """
    from pysent._olci_l1 import solar_flux_per_pixel
    from pysent._sen3 import sen3_root

    product = tmp_path / "stub.zip"
    shape = _instrument_product(product, tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    for band in (4, 6, 8):
        flux = solar_flux_per_pixel(sen3_root(str(product)), scratch, band, shape)
        assert flux.shape == shape
        assert np.allclose(flux, 1000.0 + band), f"band {band} got {np.unique(flux)}"
