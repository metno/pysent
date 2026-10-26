"""Detection and routing for all four Sentinel families.

The titles below are real ones from the NBS catalogue (identifiers shortened
where they carry nothing a detector looks at). They are the point of this file:
the bug it guards against was invisible to synthetic names, because it came
from a token the real Sentinel-3 naming convention puts in every title.
"""
import random
import uuid

import pytest

from pysent.errors import UnsupportedProductError
from pysent.profiles import (
    SENTINEL_PRODUCT_TYPES,
    detect_nbs_sentinel_platform,
    detect_nbs_sentinel_product,
    detect_nbs_sentinel_product_for_dataset,
    resolve_sentinel_pipeline,
    supported_sentinel_product_types,
)

# Real titles, one per family and per product type that the plan mentions.
S1_GRD = "S1A_IW_GRDH_1SDV_20260411T155118_20260411T155143_012345_01ABCD_1234.SAFE"
S1_SLC = "S1A_IW_SLC__1SDV_20260411T155118_20260411T155143_012345_01ABCD_1234.SAFE"
S2_L1C = "S2A_MSIL1C_20220319T110701_N0400_R137_T32VNR_20220319T131953.SAFE"
S2_L2A = "S2B_MSIL2A_20220319T110701_N0400_R137_T32VNR_20220319T131953.SAFE"
# The two Sentinel-3 processing-centre tokens that broke the old detector.
S3_WFR_PS1 = (
    "S3A_OL_2_WFR____20260101T101530_20260101T101830_20260102T193423_0179_001_001"
    "_1440_PS1_O_NT_002.SEN3"
)
S3_LST_PS2 = (
    "S3B_SL_2_LST____20260101T101530_20260101T101830_20260102T193423_0179_001_001"
    "_1440_PS2_O_NT_004.SEN3"
)
S3_VG1 = (
    "S3A_SY_2_VG1____20260101T000000_20260101T235959_20260103T120000"
    "_EUROPE____________PS1_O_NT_002.SEN3"
)
S3_EFR = (
    "S3A_OL_1_EFR____20260101T101530_20260101T101830_20260102T193423_0179_001_001"
    "_1440_PS1_O_NT_002.SEN3"
)
S5P_NO2_OFFL = "S5P_OFFL_L2__NO2____20260101T010101_20260101T024231_31234_03_020600_20260103T030303.nc"
S5P_CO_NRTI = "S5P_NRTI_L2__CO_____20260101T010101_20260101T024231_31234_03_020600_20260103T030303.nc"
S5P_O3TCL_RPRO = "S5P_RPRO_L2__O3_TCL_20260101T010101_20260101T024231_31234_03_020600_20260103T030303.nc"
S5P_AERAI_OFFL = "S5P_OFFL_L2__AER_AI_20260101T010101_20260101T024231_31234_03_020600_20260103T030303.nc"


# --------------------------------------------------------------------------- #
# The regression: Sentinel-3 titles must not be read as Sentinel-1 or -2
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("title", [S3_WFR_PS1, S3_LST_PS2, S3_VG1, S3_EFR])
def test_sentinel3_titles_are_not_mistaken_for_s1_or_s2(title):
    """The A5 finding, as a test.

    Sentinel-3 titles carry a processing-centre token - ``_PS1_``, ``_PS2_`` -
    and an unanchored ``S[12][A-Z]?`` matches the ``S1``/``S2`` inside it. On
    ``main`` this classifies 193 of 630 sampled Sentinel-3 records (31 %) as
    Sentinel-1 or Sentinel-2, and since the service ran anything that was not
    Sentinel-2 through the Sentinel-1 pipeline, those records were processed
    as SAR.
    """
    assert detect_nbs_sentinel_platform(title)["family"] == "S3"


def test_sentinel5p_titles_are_recognised_at_all():
    """On ``main`` every Sentinel-5P record is ``unknown`` (1,860 of 1,860 sampled)."""
    assert detect_nbs_sentinel_platform(S5P_NO2_OFFL) == {"family": "S5P", "platform": "S5P"}


def test_an_unrecognised_product_is_refused_not_routed_to_s1():
    """The fall-through that made the misroute harmful: not S2, therefore S1."""
    with pytest.raises(UnsupportedProductError, match="could not tell which Sentinel family"):
        resolve_sentinel_pipeline("some-unrelated-name-2026.nc")


# --------------------------------------------------------------------------- #
# Family, platform, product type and stream, from real names
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "title, family, platform, product_type, stream",
    [
        (S1_GRD, "S1", "S1A", "GRDH", ""),
        (S1_SLC, "S1", "S1A", "SLC", ""),
        (S2_L1C, "S2", "S2A", "MSIL1C", ""),
        (S2_L2A, "S2", "S2B", "MSIL2A", ""),
        (S3_WFR_PS1, "S3", "S3A", "OL_2_WFR", ""),
        (S3_LST_PS2, "S3", "S3B", "SL_2_LST", ""),
        (S3_VG1, "S3", "S3A", "SY_2_VG1", ""),
        (S3_EFR, "S3", "S3A", "OL_1_EFR", ""),
        (S5P_NO2_OFFL, "S5P", "S5P", "L2__NO2___", "OFFL"),
        (S5P_CO_NRTI, "S5P", "S5P", "L2__CO____", "NRTI"),
        (S5P_O3TCL_RPRO, "S5P", "S5P", "L2__O3_TCL", "RPRO"),
        (S5P_AERAI_OFFL, "S5P", "S5P", "L2__AER_AI", "OFFL"),
    ],
)
def test_detect_product_from_real_titles(title, family, platform, product_type, stream):
    assert detect_nbs_sentinel_product(title) == {
        "family": family,
        "platform": platform,
        "product_type": product_type,
        "stream": stream,
    }


@pytest.mark.parametrize(
    "path, family, product_type",
    [
        # The archive abbreviates the type in a directory name, and a path may
        # carry no filename at all.
        ("/nbsArchive/2026/09/22/TR-L2-NO2/", "S5P", "L2__NO2___"),
        ("/nbsArchive/2026/09/22/TR-L2-AER_AI/", "S5P", "L2__AER_AI"),
        ("/nbsArchive/2026/09/22/OL-L2-WFR/", "S3", "OL_2_WFR"),
        ("/nbsArchive/2026/09/22/SY-L2-VG1/", "S3", "SY_2_VG1"),
    ],
)
def test_detect_product_from_archive_path_alone(path, family, product_type):
    detected = detect_nbs_sentinel_product(path)
    assert (detected["family"], detected["product_type"]) == (family, product_type)


def test_the_title_wins_over_the_archive_path():
    """Both spellings are present in a real reference; the title is authoritative."""
    detected = detect_nbs_sentinel_product(f"/nbsArchive/2026/09/22/TR-L2-NO2/{S5P_NO2_OFFL}")
    assert detected["product_type"] == "L2__NO2___"
    assert detected["stream"] == "OFFL"


def test_detect_product_for_dataset_is_duck_typed():
    class Info:
        identifier = "e7c4f1a0-0000-0000-0000-000000000000"
        title = S3_WFR_PS1
        source_dataset = ""
        opendap_url = ""
        filename = ""

    detected = detect_nbs_sentinel_product_for_dataset(Info())
    assert (detected["family"], detected["product_type"]) == ("S3", "OL_2_WFR")


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("title, pipeline", [(S1_GRD, "s1"), (S2_L1C, "s2")])
def test_supported_products_route_to_their_pipeline(title, pipeline):
    assert resolve_sentinel_pipeline(title) == pipeline


@pytest.mark.parametrize(
    "title, expected",
    [
        (S1_SLC, "single-look complex"),
        (S2_L2A, "surface reflectance"),
        (S3_EFR, "radiance only"),
        (S5P_O3TCL_RPRO, "gridded product"),
    ],
)
def test_out_of_scope_products_are_refused_with_the_reason(title, expected):
    with pytest.raises(UnsupportedProductError, match=expected):
        resolve_sentinel_pipeline(title)


@pytest.mark.parametrize("title", [S3_WFR_PS1, S3_VG1, S5P_NO2_OFFL, S5P_CO_NRTI])
def test_planned_products_refuse_until_their_phase_lands(title):
    """Recognised, routed in the table, but no pipeline exists yet.

    This must refuse rather than return a pipeline name, or a caller would be
    handed a module that cannot read the product. Each phase flips its own rows
    of ``SENTINEL_PRODUCT_TYPES`` from ``planned`` to ``ready``.
    """
    with pytest.raises(UnsupportedProductError, match="planned .* but not implemented"):
        resolve_sentinel_pipeline(title)


def test_refusals_say_what_is_supported():
    with pytest.raises(UnsupportedProductError, match="S2 MSIL1C"):
        resolve_sentinel_pipeline(S3_WFR_PS1)


def test_resolve_accepts_a_detection_dict_and_an_object():
    class Info:
        identifier = ""
        title = S1_GRD
        source_dataset = ""
        opendap_url = ""
        filename = ""

    assert resolve_sentinel_pipeline(detect_nbs_sentinel_product(S1_GRD)) == "s1"
    assert resolve_sentinel_pipeline(Info()) == "s1"


def test_a_platform_only_dict_is_refused_for_the_right_reason():
    """A caller passing the older platform-only result must not be routed blindly."""
    with pytest.raises(UnsupportedProductError, match="not its product type"):
        resolve_sentinel_pipeline(detect_nbs_sentinel_platform(S1_GRD))


def test_every_table_entry_is_well_formed():
    for (family, product_type), (pipeline, status, note) in SENTINEL_PRODUCT_TYPES.items():
        assert family in {"S1", "S2", "S3", "S5P"}
        assert pipeline in {"s1", "s2", "s3", "s5p"}
        assert status in {"ready", "planned", "unsupported"}
        # Anything refused has to explain itself; a ready product needs no note.
        assert bool(note) == (status != "ready"), (family, product_type)


def test_only_s1_grd_and_s2_l1c_are_ready_in_this_release():
    assert sorted(supported_sentinel_product_types()) == ["S1 GRD", "S1 GRDH", "S1 GRDM", "S2 MSIL1C"]


# --------------------------------------------------------------------------- #
# What must never match
# --------------------------------------------------------------------------- #
def test_uuids_and_tile_ids_never_look_like_a_platform():
    """Identifiers are UUIDs and Sentinel-2 tile ids; neither may match.

    A UUID is hex, so it cannot contain an ``S`` - but a detector is fed the
    identifier first, and an earlier version of this pattern could match across
    a boundary, so this is worth holding down.
    """
    rng = random.Random(20261026)
    for _ in range(2000):
        assert detect_nbs_sentinel_platform(str(uuid.UUID(int=rng.getrandbits(128))))["family"] == "unknown"
    for _ in range(2000):
        tile = "T{:02d}{}{}{}".format(
            rng.randint(1, 60),
            rng.choice("CDEFGHJKLMNPQRSTUVWX"),
            rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ"),
            rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ"),
        )
        assert detect_nbs_sentinel_platform(tile)["family"] == "unknown"


@pytest.mark.parametrize(
    "text",
    [
        "PS1_O_NT_002",      # the Sentinel-3 processing centre, the actual bug
        "PS2_O_NR_001",
        "MS1A_something",     # a letter before the token
        "S10_product",        # a digit after it
        "S1AB_product",       # a letter after the unit letter
        "S4_mission",         # a mission pysent knows nothing about
        "S5_not_5p",
    ],
)
def test_tokens_inside_other_words_do_not_match(text):
    assert detect_nbs_sentinel_platform(text)["family"] == "unknown"


@pytest.mark.parametrize(
    "text, platform",
    [
        ("S1A_IW", "S1A"),            # underscore is a boundary
        ("/data/S3B/file.nc", "S3B"),  # slash is
        ("product.S2A.zip", "S2A"),    # dot is
        ("scene-S1C-grd", "S1C"),      # dash is
        ("S5P", "S5P"),                # the whole string
        ("a S2 tile", "S2"),           # space, and no unit letter
    ],
)
def test_token_boundaries(text, platform):
    assert detect_nbs_sentinel_platform(text)["platform"] == platform
