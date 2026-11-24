"""The Sentinel-3 and Sentinel-5P defaults are the review studies' approved ones.

Each value below is copied from a study's manifest (its decisions' ``default``
options), which the owner approved from the pictures: ``s5p-l2`` on 2026-10-30,
``s3-l2`` on 2026-11-02. A change to pysent's tables that moves one of them
changes what a service publishes, so it has to fail here and be decided again.
"""
from __future__ import annotations

import pytest

from pysent.errors import UnsupportedProductError
from pysent.profiles import REVIEWED_SENTINEL_DEFAULTS, sentinel_product_defaults
from pysent.s3 import S3_PRODUCTS, S3_TYPE_PRODUCTS
from pysent.s5p import S5P_PRODUCTS, S5P_TYPE_PRODUCTS

# product: (qa_min, value_range, colormap, variable)
S5P_APPROVED = {
    "no2": (0.75, (0.0, 1e-4), "viridis", "/PRODUCT/nitrogendioxide_tropospheric_column"),
    "co": (0.5, (0.007, 0.048), "viridis", "/PRODUCT/carbonmonoxide_total_column"),
    "hcho": (0.5, (0.0, 5e-4), "viridis", "/PRODUCT/formaldehyde_tropospheric_vertical_column"),
    "so2": (0.5, (0.0, 2.5e-3), "viridis", "/PRODUCT/sulfurdioxide_total_vertical_column"),
    "o3": (0.5, (0.08, 0.22), "viridis", "/PRODUCT/ozone_total_vertical_column"),
    "ch4": (0.5, (1690.0, 1980.0), "viridis", "/PRODUCT/methane_mixing_ratio_bias_corrected"),
    "aer_ai": (0.5, (-2.0, 2.0), "rdbu", "/PRODUCT/aerosol_index_354_388"),
    "cloud": (0.5, (0.0, 1.0), "grey", "/PRODUCT/cloud_fraction"),
    "aer_lh": (0.5, (0.0, 9000.0), "viridis", "/PRODUCT/aerosol_mid_height"),
    "o3_pr_total": (0.5, (0.10, 0.22), "viridis", "/PRODUCT/ozone_total_column"),
}

_WFR = ["INVALID", "LAND", "CLOUD", "SNOW_ICE", "SATURATED", "HIGHGLINT", "WHITECAPS", "AC_FAIL"]
_MARGINAL = ["CLOUD_AMBIGUOUS", "CLOUD_MARGIN", "CLOUD_SHADOW"]
# product: (value_range, colormap, log scale, mask_flags, reject_flags, browse_reject_flags)
S3_APPROVED = {
    "chl": ((0.01, 50.0), "viridis", True, ["WATER"], _WFR + ["OCNN_FAIL"], _MARGINAL),
    "tsm": ((0.01, 100.0), "magma", True, ["WATER"], _WFR + ["OCNN_FAIL"], _MARGINAL),
    "kd490": ((0.01, 10.0), "viridis", True, ["WATER"], _WFR, _MARGINAL),
    "gifapar": ((0.0, 1.0), "brown_green", False, ["LAND"],
                ["INVALID", "CLOUD", "SNOW_ICE", "SATURATED", "HISOLZEN", "GIFAPAR_FAIL"], _MARGINAL),
    "otci": ((0.0, 5.0), "brown_green", False, ["LAND"],
             ["INVALID", "CLOUD", "SNOW_ICE", "SATURATED", "HISOLZEN", "OTCI_FAIL"], _MARGINAL),
    "lst": ((-43.0, 33.0), "magma", False, ["land"],
            ["summary_cloud", "unfilled", "summary_pointing", "duplicate"], []),
    "sst": ((-2.0, 20.0), "magma", False, [], ["land", "ice"], []),
}

TITLES = {
    "co": "S5P_OFFL_L2__CO_____20260510T142648_20260510T160818_44424_03_020901_20260512T063937",
    "o3_pr": "S5P_NRTI_L2__O3__PR_20260510T142648_20260510T143148_44424_03_020901_20260512T063937.nc",
    "wfr": "S3B_OL_2_WFR____20260709T153000_20260709T153300_20260710T211739_0179_122_111_1260_MAR_O_NT_004",
    "lst": "S3B_SL_2_LST____20260709T153000_20260709T153300_20260710T211739_0179_122_111_1260_MAR_O_NT_004",
    "wst": "S3A_SL_2_WST____20260709T153000_20260709T153300_20260710T211739_0179_122_111_1260_MAR_O_NT_004",
    "efr": "S3A_OL_1_EFR____20260709T153000_20260709T153300_20260710T211739_0179_122_111_1260_MAR_O_NT_004",
    "vg1": "S3A_SY_2_VG1____20260709T000000_20260709T235959_20260710T211739_EUROPE____________MAR_O_NT_002",
    "s2": "S2A_MSIL2A_20261117T104331_N0511_R008_T33VUG_20261117T130021.zip",
    "s1": "S1A_IW_GRDH_1SDV_20261117T053012_20261117T053037_056789_06F0A1_ABCD.zip",
    "slc": "S1A_IW_SLC__1SDV_20261117T053012_20261117T053037_056789_06F0A1_ABCD.zip",
}


@pytest.mark.parametrize("product", sorted(S5P_APPROVED))
def test_s5p_table_holds_the_approved_defaults(product):
    qa_min, value_range, colormap, variable = S5P_APPROVED[product]
    row = S5P_PRODUCTS[product]
    assert row["qa_min"] == qa_min
    assert tuple(row["value_range"]) == pytest.approx(value_range)
    assert row["colormap"] == colormap
    assert row["variable"] == variable


@pytest.mark.parametrize("product", sorted(S3_APPROVED))
def test_s3_table_holds_the_approved_defaults(product):
    value_range, colormap, log_scale, mask, reject, browse = S3_APPROVED[product]
    row = S3_PRODUCTS[product]
    assert tuple(row["value_range"]) == pytest.approx(value_range)
    assert row["colormap"] == colormap
    assert bool(row.get("log_scale")) is log_scale
    assert list(row["mask_flags"]) == mask
    assert list(row["reject_flags"]) == reject
    assert list(row["browse_reject_flags"]) == browse


def test_s3_approved_extras():
    # Display units Celsius; SST the dual view with quality_level >= 4; truecolor 0-0.6, gamma 0.6.
    assert S3_PRODUCTS["lst"]["unit"] == "celsius" and S3_PRODUCTS["sst"]["unit"] == "celsius"
    assert S3_PRODUCTS["sst"]["view"] == "dual"
    assert S3_PRODUCTS["sst"]["extra_masks"] == (
        {"member": "*_DUAL_*.nc", "variable": "quality_level", "min_value": 4},
    )
    assert tuple(S3_PRODUCTS["truecolor"]["value_range"]) == (0.0, 0.6)
    assert S3_PRODUCTS["truecolor"]["gamma"] == 0.6
    assert list(S3_PRODUCTS["truecolor"]["reject_flags"]) == ["invalid", "saturated"]


def test_every_reviewed_product_is_made_by_default_for_some_type():
    made = {
        "s5p": {p for names in S5P_TYPE_PRODUCTS.values() for p in names},
        "s3": {p for names in S3_TYPE_PRODUCTS.values() for p in names},
    }
    for pipeline, entry in REVIEWED_SENTINEL_DEFAULTS.items():
        assert set(entry["products"].split()) <= made[pipeline]
    assert set(REVIEWED_SENTINEL_DEFAULTS["s5p"]["products"].split()) == set(S5P_APPROVED)
    assert set(REVIEWED_SENTINEL_DEFAULTS["s3"]["products"].split()) == set(S3_APPROVED) | {"truecolor"}


def test_s5p_defaults_as_the_service_reads_them():
    got = sentinel_product_defaults(TITLES["co"])
    assert (got["family"], got["product_type"], got["pipeline"]) == ("S5P", "L2__CO____", "s5p")
    assert got["products"] == {
        "co": {
            "label": "CO total column",
            "units": "mol m-2",
            "value_range": [0.007, 0.048],
            "colormap": "viridis",
            "scale": "linear",
            "qa_min": 0.5,
            "variable": "/PRODUCT/carbonmonoxide_total_column",
            "reviewed": {"study": "s5p-l2", "approved": "2026-10-30"},
        }
    }


def test_the_profile_products_tropospheric_column_is_not_reviewed():
    products = sentinel_product_defaults(TITLES["o3_pr"])["products"]
    assert list(products) == ["o3_pr_total", "o3_pr_tropospheric"]
    assert products["o3_pr_total"]["value_range"] == [0.10, 0.22]
    assert products["o3_pr_total"]["reviewed"] == {"study": "s5p-l2", "approved": "2026-10-30"}
    assert products["o3_pr_tropospheric"]["reviewed"] is None


def test_s3_defaults_as_the_service_reads_them():
    wfr = sentinel_product_defaults(TITLES["wfr"])["products"]
    assert list(wfr) == ["chl", "tsm", "kd490"]
    assert wfr["chl"] == {
        "label": "Chlorophyll-a concentration",
        "units": "mg m-3",
        "value_range": [0.01, 50.0],
        "colormap": "viridis",
        "scale": "log10",
        "reviewed": {"study": "s3-l2", "approved": "2026-11-02"},
    }
    lst = sentinel_product_defaults(TITLES["lst"])["products"]["lst"]
    assert (lst["units"], lst["value_range"], lst["scale"]) == ("degC", [-43.0, 33.0], "linear")
    sst = sentinel_product_defaults(TITLES["wst"])["products"]["sst"]
    assert (sst["view"], sst["value_range"], sst["colormap"]) == ("dual", [-2.0, 20.0], "magma")
    truecolor = sentinel_product_defaults(TITLES["efr"])["products"]["truecolor"]
    assert truecolor["colormap"] is None
    assert (truecolor["value_range"], truecolor["gamma"]) == ([0.0, 0.6], 0.6)


def test_a_product_outside_the_studies_says_so():
    ndvi = sentinel_product_defaults(TITLES["vg1"])["products"]["ndvi"]
    assert ndvi["reviewed"] is None
    assert ndvi["value_range"] == [-0.1, 0.9]


def test_stretched_families_report_no_fixed_styling():
    assert sentinel_product_defaults(TITLES["s2"]) == {
        "family": "S2", "product_type": "MSIL2A", "pipeline": "s2", "products": None,
    }
    assert sentinel_product_defaults(TITLES["s1"])["products"] is None


def test_a_refused_product_is_refused_the_same_way():
    with pytest.raises(UnsupportedProductError, match="single-look complex"):
        sentinel_product_defaults(TITLES["slc"])
    with pytest.raises(UnsupportedProductError):
        sentinel_product_defaults("not a sentinel product")
