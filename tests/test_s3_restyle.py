"""The two-audience split, and re-styling a product without re-warping.

The Float32 product answers to a coverage service and the styled copy to a map
service, and they do not have to hide the same pixels. Everything here is about
keeping that split honest: the float keeps what it can explain, the browse image
shows only what is clean, and changing a colour decision costs a re-render
rather than a reprocess.
"""
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("osgeo")

from osgeo import gdal  # noqa: E402

from pysent._output import _matches_codes  # noqa: E402
from pysent.s3 import (  # noqa: E402
    BROWSE_HIDDEN_REASONS,
    REJECT_REASONS,
    process_sentinel_s3_safe,
    restyle_sentinel_s3_product,
)


@pytest.fixture(scope="module")
def processed(s3_olci_window, tmp_path_factory):
    directory = tmp_path_factory.mktemp("processed")
    return process_sentinel_s3_safe(
        input_dataset=str(s3_olci_window), output_dir=directory, products={"chl": {}}
    )[0]


# --------------------------------------------------------------------------- #
# The split
# --------------------------------------------------------------------------- #
def test_the_float_product_keeps_more_than_the_browse_image_shows(processed):
    """The point of the split, measured.

    On this window the float carries 38.1 % of cells and the browse image draws
    28.1 % - the extra ten points are pixels beside, possibly in, or shadowed by
    cloud. A coverage user gets them with a reason attached; a map user does not
    get cloud edges in the picture.
    """
    float_dataset = gdal.Open(processed["path"])
    values = float_dataset.GetRasterBand(1).ReadAsArray()
    float_dataset = None
    styled_dataset = gdal.Open(processed["styled_path"])
    styled = styled_dataset.GetRasterBand(1).ReadAsArray()
    styled_dataset = None

    in_float = float(np.isfinite(values).mean())
    drawn = float((styled > 0).mean())
    assert in_float > drawn, "the float should keep pixels the browse image hides"
    assert drawn > 0.1, "the browse image should still show something"


def test_the_hidden_pixels_are_exactly_the_marginal_ones(processed):
    float_dataset = gdal.Open(processed["path"])
    values = float_dataset.GetRasterBand(1).ReadAsArray()
    codes = float_dataset.GetRasterBand(2).ReadAsArray()
    float_dataset = None
    styled_dataset = gdal.Open(processed["styled_path"])
    styled = styled_dataset.GetRasterBand(1).ReadAsArray()
    styled_dataset = None

    marginal = _matches_codes(codes, BROWSE_HIDDEN_REASONS)
    # Every marginal pixel has a value in the float and none in the browse image.
    assert np.all(np.isfinite(values[marginal]))
    assert np.all(styled[marginal] == 0)
    # Nothing else was dropped from the browse image.
    clean = np.isfinite(values) & ~marginal
    assert np.all(styled[clean] >= 1)


def test_the_reason_band_names_why_each_pixel_is_hidden(processed):
    dataset = gdal.Open(processed["path"])
    assert dataset.RasterCount == 2
    codes = dataset.GetRasterBand(2).ReadAsArray()
    dataset = None

    present = {
        name for name, code in REJECT_REASONS.items()
        if _matches_codes(codes, (code,)).any()
    }
    # A cloud buffer is the big one on a real frame; it should be there.
    assert "cloud_margin" in present
    assert "valid" in present
    assert processed["query_source"]["reject_reason_band"] == 2
    assert set(processed["browse_hidden_reasons"]) <= set(REJECT_REASONS)


def test_a_reason_band_outside_the_swath_is_not_mistaken_for_a_code():
    """The reason band rides through the same warp as the values, so cells
    outside the swath hold NaN rather than a code. Rounding that to an integer
    is undefined, and an unguarded cast turns it into a real reason."""
    codes = np.array([[np.nan, 0.0, 9.0, np.inf]], dtype=np.float32)
    matched = _matches_codes(codes, (REJECT_REASONS["cloud_margin"],))
    assert matched.tolist() == [[False, False, True, False]]


def test_a_product_with_no_marginal_tier_has_no_split(s3_synergy_window, tmp_path):
    """The Synergy status word has no marginal tier - its classes are the whole
    decision - so its float and browse image agree."""
    result = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window), output_dir=tmp_path, products={"ndvi": {}}
    )[0]
    assert result["browse_hidden_reasons"] == []


# --------------------------------------------------------------------------- #
# Re-styling without re-warping
# --------------------------------------------------------------------------- #
def test_restyling_changes_the_picture_and_leaves_the_data_alone(processed, tmp_path):
    before = gdal.Open(processed["path"])
    original = before.GetRasterBand(1).ReadAsArray()
    before = None
    first = gdal.Open(processed["styled_path"])
    first_pixels = first.GetRasterBand(1).ReadAsArray()
    first = None

    written = restyle_sentinel_s3_product(
        product_path=processed["path"], output_dir=tmp_path,
        value_range=(0.1, 30.0), colormap="magma",
    )
    second = gdal.Open(written["styled_path"])
    second_pixels = second.GetRasterBand(1).ReadAsArray()
    second = None
    after = gdal.Open(processed["path"])
    unchanged = after.GetRasterBand(1).ReadAsArray()
    after = None

    assert not np.array_equal(first_pixels, second_pixels), "a new range should render differently"
    assert np.array_equal(original, unchanged, equal_nan=True), "the float product must not be touched"
    assert written["value_range"] == [0.1, 30.0]
    assert written["colormap"] == "magma"


def test_restyling_reads_what_it_is_not_told_from_the_products_own_tags(processed, tmp_path):
    """So a caller can change one thing and keep the rest."""
    written = restyle_sentinel_s3_product(
        product_path=processed["path"], output_dir=tmp_path, colormap="grey"
    )
    assert written["colormap"] == "grey"
    # Range and scale came back from the file.
    assert written["value_range"] == list(processed["style_hint"]["value_range"])
    assert written["scale"] == processed["style_hint"]["scale"]


def test_restyling_still_hides_what_the_browse_image_hides(processed, tmp_path):
    written = restyle_sentinel_s3_product(
        product_path=processed["path"], output_dir=tmp_path, colormap="grey"
    )
    assert written["browse_hidden"] is True

    dataset = gdal.Open(processed["path"])
    codes = dataset.GetRasterBand(2).ReadAsArray()
    dataset = None
    styled = gdal.Open(written["styled_path"])
    pixels = styled.GetRasterBand(1).ReadAsArray()
    styled = None
    assert np.all(pixels[_matches_codes(codes, BROWSE_HIDDEN_REASONS)] == 0)


def test_restyling_records_the_style_it_applied(processed, tmp_path):
    written = restyle_sentinel_s3_product(
        product_path=processed["path"], output_dir=tmp_path,
        value_range=(1.0, 40.0), colormap="viridis", log_scale=False,
    )
    dataset = gdal.Open(written["styled_path"])
    tags = dataset.GetMetadata()
    dataset = None
    # The styled writer formats the range itself, with repr rather than %g.
    # The float product's own tag uses %g ("1,40"), so the two spellings differ -
    # a wart worth tidying once the Sentinel-5P freeze lifts, since changing it
    # now would alter a tag on products already rendered for the review study.
    assert tags["PYSENT_STYLE_RANGE"] == "1.0,40.0"
    assert tags["PYSENT_STYLE_SCALE"] == "linear"
    assert tags["PYSENT_COLORMAP"] == "viridis"
    # The provenance of the data it came from survives.
    assert tags["PYSENT_VARIABLE"] == "CHL_NN"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"value_range": (5.0, 1.0)}, "low < high"),
        ({"colormap": "chartreuse"}, "not a colormap"),
        ({"value_range": (0.0, 10.0), "log_scale": True}, "positive lower bound"),
    ],
)
def test_a_bad_restyle_request_is_refused(processed, tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        restyle_sentinel_s3_product(product_path=processed["path"], output_dir=tmp_path, **kwargs)


def test_restyling_something_that_is_not_there_says_so(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        restyle_sentinel_s3_product(product_path=tmp_path / "nothing.tif")


def test_restyling_a_raster_without_a_style_tag_needs_a_range(tmp_path):
    """A plain GeoTIFF carries no PYSENT_STYLE_RANGE, so there is nothing to
    read back and guessing would be worse than asking."""
    path = tmp_path / "plain.tif"
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), 4, 4, 1, gdal.GDT_Float32)
    dataset.GetRasterBand(1).WriteArray(np.ones((4, 4), dtype=np.float32))
    dataset = None
    with pytest.raises(ValueError, match="no usable PYSENT_STYLE_RANGE"):
        restyle_sentinel_s3_product(product_path=path, output_dir=tmp_path)

    assert restyle_sentinel_s3_product(
        product_path=path, output_dir=tmp_path, value_range=(0.0, 2.0), thumbnail=False
    )["value_range"] == [0.0, 2.0]


def test_restyling_is_much_cheaper_than_processing(processed, tmp_path):
    """The claim the split rests on: a colour decision is reversible.

    Not timed - a timing assertion in CI is a flake - but the work is bounded by
    reading one raster back, with no archive access, no unpacking and no warp.
    """
    import time

    start = time.monotonic()
    restyle_sentinel_s3_product(product_path=processed["path"], output_dir=tmp_path, colormap="magma")
    assert time.monotonic() - start < 30.0


# --------------------------------------------------------------------------- #
# The warp's temporary directory
# --------------------------------------------------------------------------- #
def test_the_warp_points_gdal_at_a_writable_temporary_directory(tmp_path):
    """GDAL's geolocation warp writes hole-filling work files to CPL_TMPDIR, or
    to the current directory when it is unset. In a read-only container neither
    is writable and the hole fill fails silently, with one ERROR 4 in the log.
    """
    from pysent._swath import warp_swath

    seen = {}
    real = gdal.Warp

    def spy(*args, **kwargs):
        seen["tmpdir"] = gdal.GetConfigOption("CPL_TMPDIR")
        return real(*args, **kwargs)

    lat = np.repeat(np.linspace(60.0, 61.0, 8)[:, None], 8, axis=1)
    lon = np.repeat(np.linspace(10.0, 11.0, 8)[None, :], 8, axis=0)
    gdal.Warp = spy
    try:
        warp_swath(
            values=np.ones((8, 8), dtype=np.float32), lat=lat, lon=lon,
            output_path=str(tmp_path / "out.tif"), epsg=4326, resolution=0.1,
            resampling="near", scratch=tmp_path,
        )
    finally:
        gdal.Warp = real

    assert seen["tmpdir"] == str(tmp_path)
    # And it is restored afterwards, so one call cannot change another's setting.
    assert gdal.GetConfigOption("CPL_TMPDIR") != str(tmp_path)


# --------------------------------------------------------------------------- #
# A picture product is restyled as a picture (review-study item R5)
# --------------------------------------------------------------------------- #
def _rgb_float_product(path, *, gamma="0.6"):
    """A three-band true-colour float, as the pipeline writes one."""
    from osgeo import osr

    rows, columns = 8, 10
    bands = [
        np.linspace(0.0, 0.5, rows * columns, dtype=np.float32).reshape(rows, columns),
        np.linspace(0.1, 0.6, rows * columns, dtype=np.float32).reshape(rows, columns),
        np.linspace(0.2, 0.7, rows * columns, dtype=np.float32).reshape(rows, columns),
    ]
    dataset = gdal.GetDriverByName("GTiff").Create(str(path), columns, rows, 3, gdal.GDT_Float32)
    reference = osr.SpatialReference()
    reference.ImportFromEPSG(4326)
    dataset.SetProjection(reference.ExportToWkt())
    dataset.SetGeoTransform((10.0, 0.01, 0.0, 60.0, 0.0, -0.01))
    for index, (band, label) in enumerate(
        zip(bands, ("Oa08_665nm_toa_reflectance", "Oa06_560nm_toa_reflectance",
                    "Oa04_490nm_toa_reflectance"))
    ):
        raster = dataset.GetRasterBand(index + 1)
        raster.SetNoDataValue(float("nan"))
        raster.WriteArray(band)
        raster.SetDescription(label)
    dataset.SetMetadata({
        "PYSENT_PRODUCT": "truecolor",
        "PYSENT_STYLE_RANGE": "0,0.6",
        "PYSENT_STYLE_GAMMA": gamma,
        "PYSENT_STYLE_SCALE": "linear",
    })
    dataset.FlushCache()
    dataset = None
    return path


def test_a_truecolor_float_is_restyled_as_rgb(tmp_path):
    """Regression: ``band_count >= 2`` read green reflectance as the reason band.

    On ``main`` this wrote a single-band paletted copy of a three-band picture,
    with pixels hidden wherever the rounded green value matched a reason code.
    """
    product = _rgb_float_product(tmp_path / "truecolor.tif")
    result = restyle_sentinel_s3_product(product_path=str(product), output_dir=tmp_path / "out")

    styled = gdal.Open(result["styled_path"])
    # Three colour bands plus alpha, and no palette. On main this is one
    # paletted band, which is the defect.
    assert styled.RasterCount == 4
    assert styled.GetRasterBand(1).GetRasterColorTable() is None
    tags = styled.GetMetadata()
    styled = None

    assert tags["PYSENT_STYLE_GAMMA"] == "0.6"
    assert result["rgb"] is True
    assert result["gamma"] == pytest.approx(0.6)
    assert result["browse_hidden"] is False
    assert Path(result["thumbnail_path"]).exists()


def test_the_gamma_can_be_changed_without_reprocessing(tmp_path):
    product = _rgb_float_product(tmp_path / "truecolor.tif")
    dark = restyle_sentinel_s3_product(
        product_path=str(product), output_dir=tmp_path / "dark", gamma=1.0
    )
    light = restyle_sentinel_s3_product(
        product_path=str(product), output_dir=tmp_path / "light", gamma=0.4
    )

    def green(path):
        dataset = gdal.Open(path)
        array = dataset.GetRasterBand(2).ReadAsArray()
        dataset = None
        return array.mean()

    # A gamma below one lifts the dark end, so the same data renders brighter.
    assert green(light["styled_path"]) > green(dark["styled_path"])


def test_the_ramp_options_are_refused_for_a_picture(tmp_path):
    product = _rgb_float_product(tmp_path / "truecolor.tif")
    with pytest.raises(ValueError, match="does not apply"):
        restyle_sentinel_s3_product(product_path=str(product), colormap="viridis")
    with pytest.raises(ValueError, match="does not apply"):
        restyle_sentinel_s3_product(product_path=str(product), log_scale=True)


def test_gamma_is_refused_for_a_ramped_product(s3_synergy_window, tmp_path):
    product = process_sentinel_s3_safe(
        input_dataset=str(s3_synergy_window),
        output_dir=tmp_path / "ndvi",
        products={"ndvi": {}},
        processing_options={"styled": False, "thumbnail": False, "decimate": 8},
    )[0]
    with pytest.raises(ValueError, match="gamma"):
        restyle_sentinel_s3_product(product_path=product["path"], gamma=0.5)
