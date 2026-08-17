import json

import pytest

from pysent.profiles import (
    PROFILE_ENV_VAR,
    apply_nbs_sentinel_metadata_profile,
    detect_nbs_sentinel_platform,
    detect_nbs_sentinel_platform_for_dataset,
    get_nbs_sentinel_platform_profiles,
    resolve_nbs_sentinel_platform_profile,
)


@pytest.mark.parametrize(
    "hint, family, platform",
    [
        ("S1A_IW_GRDH_1SDV_20260411T155118.zip", "S1", "S1A"),
        ("S1C_EW_GRDM_1SDH_scene", "S1", "S1C"),
        ("S2A_MSIL1C_20220319T110701.SAFE", "S2", "S2A"),
        ("some-unrelated-name", "unknown", "unknown"),
    ],
)
def test_detect_platform_from_product_name(hint, family, platform):
    assert detect_nbs_sentinel_platform(hint) == {"family": family, "platform": platform}


def test_detect_platform_prefers_three_character_token():
    # A bare "S2" earlier in the list must not win over a later exact "S2B".
    assert detect_nbs_sentinel_platform("S2", "S2B_MSIL1C")["platform"] == "S2B"


def test_detect_platform_skips_empty_hints():
    assert detect_nbs_sentinel_platform("", None or "", "S1A_IW")["family"] == "S1"


def test_detect_platform_for_dataset_is_duck_typed():
    class Info:
        identifier = ""
        title = "S2B tile"
        source_dataset = ""
        opendap_url = ""
        filename = ""

    assert detect_nbs_sentinel_platform_for_dataset(Info())["platform"] == "S2B"


def test_default_profiles_carry_expected_variables():
    profiles = get_nbs_sentinel_platform_profiles()
    assert profiles["S1"]["query_variables"] == ["Amplitude_VH", "Amplitude_VV"]
    assert "B8A" in profiles["S2"]["query_variables"]


def test_configured_overrides_replace_query_variables():
    profiles = get_nbs_sentinel_platform_profiles(
        {"s1": {"query_variables": ["Amplitude_HH"], "metadata_defaults": {"title": "SAR"}}}
    )
    assert profiles["S1"]["query_variables"] == ["Amplitude_HH"]
    assert profiles["S1"]["metadata_defaults"] == {"title": "SAR"}
    # Untouched platforms keep their defaults.
    assert "B8A" in profiles["S2"]["query_variables"]


def test_env_overrides_apply_only_when_nothing_configured(monkeypatch):
    monkeypatch.setenv(PROFILE_ENV_VAR, json.dumps({"S2": {"query_variables": ["B4"]}}))
    assert get_nbs_sentinel_platform_profiles()["S2"]["query_variables"] == ["B4"]
    # An explicit (even empty) configured mapping wins over the environment.
    assert "B8A" in get_nbs_sentinel_platform_profiles({})["S2"]["query_variables"]


def test_malformed_env_override_is_ignored(monkeypatch):
    monkeypatch.setenv(PROFILE_ENV_VAR, "{not json")
    assert "B8A" in get_nbs_sentinel_platform_profiles()["S2"]["query_variables"]


def test_resolve_profile_applies_default_family_platform_chain():
    profiles = get_nbs_sentinel_platform_profiles(
        {"S1": {"query_variables": ["Amplitude_VV"]}, "S1A": {"metadata_defaults": {"abstract": "unit A"}}}
    )
    resolved = resolve_nbs_sentinel_platform_profile({"family": "S1", "platform": "S1A"}, profiles=profiles)
    assert resolved["query_variables"] == ["Amplitude_VV"]
    assert resolved["metadata_defaults"] == {"abstract": "unit A"}
    assert resolved["applied_profiles"] == ["__default__", "S1", "S1A"]


def test_resolve_profile_handles_missing_platform_info():
    resolved = resolve_nbs_sentinel_platform_profile(None)
    assert resolved["query_variables"] == []
    assert resolved["applied_profiles"] == ["__default__"]


def test_apply_metadata_profile_fills_defaults_then_forces_overrides():
    merged = apply_nbs_sentinel_metadata_profile(
        {"title": "Existing", "identifier": ""},
        {
            "metadata_defaults": {"title": "Ignored", "abstract": "Filled in"},
            "metadata_overrides": {"identifier": "forced-id"},
        },
    )
    assert merged == {"title": "Existing", "identifier": "forced-id", "abstract": "Filled in"}
