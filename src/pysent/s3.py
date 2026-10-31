"""Sentinel-3 products as map rasters.

This phase covers the Synergy vegetation composites, ``SY_2_VG1`` (one day) and
``SY_2_V10`` (ten days). They are the simplest Sentinel-3 products to put on a
map and the right place to build the foundation on, because they are **already
on a regular latitude-longitude grid** at 1/112° - about a kilometre - so there
is no swath to resample. The OLCI and SLSTR products that follow share the
reader in :mod:`pysent._sen3` and the output code in :mod:`pysent._output`, and
add the geolocation warp from :mod:`pysent._swath` on top.

Three things about these composites are worth knowing before reading a map:

**They are regional, not global.** Each product covers one of three fixed
boxes named in its own title - ``EUROPE`` (25-75°N, 11°W-62°E),
``NORTH_AMERICA`` and ``NORTH_ASIA`` - and they **stop at 75°N**, which leaves
most of Svalbard and the whole Arctic Ocean outside. For a Nordic service that
is a real limitation and should be said plainly rather than discovered.

**Most of a tile is not usable.** About 38 % of a daily tile carries a value at
all, and only about 28 % of it is cloud-free, so an empty-looking map is normal.
A ten-day composite is better by construction.

**The grid has no projection on it.** The files declare an unnamed datum, so
the CRS is assigned here rather than read; the geotransform in the file is
correct and is used as it stands.

The masks below were read off the flag names in the files rather than from the
product notices. The owner accepted them as the defaults on 2026-10-30, so they
are what ships - but that is a decision to ship rather than a confirmation that
a domain expert has checked them, and the Sentinel-3 review study is where they
can still be revised. Each one is a per-request option.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from ._output import (
    COLOUR_ANCHORS,
    write_rgb_product,
    write_rgb_styled,
    write_rgb_thumbnail,
    creation_options,
    finish_float_product,
    write_styled_copy,
    write_thumbnail,
)
from ._runtime import (
    apply_preset,
    atomic_output,
    available_cpus,
    gdal_errors,
    gdal_runtime,
    provenance,
    resolve_gdal_runtime,
    resolve_work_dir,
    run_product_jobs,
    scratch_dir,
)
from ._sen3 import (
    check_members_stored,
    resolve_variable,
    extract_member,
    flag_selection,
    open_sen3_variable,
    read_flag_table,
    read_packed,
    read_sen3_manifest,
    sen3_root,
)
from ._swath import (
    GEOLOCATION_STEPS,
    RESAMPLING_METHODS,
    choose_grid,
    footprint_polygon,
    grid_bounds,
    valid_fraction,
    value_statistics,
    warp_swath,
)
from .errors import EmptySceneError

__all__ = [
    "S3_PRESETS",
    "S3_PRODUCTS",
    "S3_TYPE_PRODUCTS",
    "BROWSE_HIDDEN_REASONS",
    "REJECT_REASONS",
    "build_sentinel_s3_output_filename",
    "default_sentinel_s3_products",
    "normalize_sentinel_s3_product_map",
    "process_sentinel_s3_safe",
    "restyle_sentinel_s3_product",
]

# The grid the Synergy composites come on: 1/112 degree, about 1 km.
SYNERGY_RESOLUTION = 1.0 / 112.0
EPSG_LATLON = 4326

# Full-resolution ground sampling per instrument, as a distance on the ground
# rather than in the units of a grid: a product cannot know whether its granule
# will land on a geographic or a projected grid, and the same number cannot mean
# both. The ozone-profile bug of phase 2 was exactly this mistake.
OLCI_GROUND_SAMPLING_M = 300.0
SLSTR_GROUND_SAMPLING_M = 1000.0

# The masks below were read off the flag names in the files rather than from the
# product notices, which were not available when this was written. The owner
# accepted them as the defaults on 2026-10-30 (open question Q12), so they are a
# decision rather than a proposal - but a decision to ship, not a statement that
# a domain expert has checked them. The Sentinel-3 review study is where they
# can still be revised, and every one of them is a per-request option, so a
# revision is a request change and not a code change.
#
# "keep" flags must be set; "reject" flags must not be. That is how the product
# documentation phrases a mask, and it is not expressible as one list: a water
# product keeps WATER and rejects eleven separate failure conditions.
S3_PRODUCTS: dict[str, dict[str, Any]] = {
    "ndvi": {
        "kind": "grid",
        "member": "NDVI.nc",
        "variable": "NDVI",
        "units": "1",
        # Byte packed as raw*0.004-0.08, so -0.08 is the bottom of the scale
        # rather than a measurement. Measured p01-p99 of clear pixels was
        # 0.03-0.85 on a European summer tile and -0.08-0.86 on a North
        # American winter one, which rounds outwards to this.
        "value_range": (-0.1, 0.9),
        "colormap": "brown_green",
        "label": "NDVI",
        "log10": False,
        # Keep only what the product calls clear. The status word puts clear,
        # unfilled, uncertain and cloud in its low two bits, so "clear" is an
        # enumeration value rather than a bit - read_flag_table handles both.
        "mask_flags": ("clear",),
        "reject_flags": (),
        # The Synergy status word has no marginal tier: its classes are the
        # whole decision, so nothing is kept in the float and hidden later.
        "browse_reject_flags": (),
        "flag_member": "sm.nc",
        "flag_variable": "SM",
    },
}

# What a water product rejects. Eleven conditions, each of which makes a
# chlorophyll retrieval meaningless rather than merely uncertain.
# Conditions under which the retrieval is not a measurement: dropped from every
# output.
_WFR_REJECT = (
    "INVALID", "LAND", "CLOUD", "SNOW_ICE",
    "SATURATED", "HIGHGLINT", "WHITECAPS", "AC_FAIL",
)
_LFR_REJECT = ("INVALID", "CLOUD", "SNOW_ICE", "SATURATED", "HISOLZEN")
# Conditions under which it may be contaminated: kept in the float product with
# the reason recorded, hidden in the browse image. See BROWSE_HIDDEN_REASONS.
_MARGINAL_CLOUD = ("CLOUD_AMBIGUOUS", "CLOUD_MARGIN", "CLOUD_SHADOW")

S3_PRODUCTS.update({
    "chl": {
        "kind": "swath",
        # Variable names move between processing baselines, so the first match
        # wins and the packing is read from whichever file is found.
        "candidates": (("chl_nn.nc", "CHL_NN"), ("chlor_a.nc", "chlor_a"), ("chl_oc4me.nc", "CHL_OC4ME")),
        "units": "mg m-3",
        # Q7's rule is the measured p01-p99 rounded outwards, but chlorophyll
        # needs the union of two regimes rather than one granule: a clear-water
        # reference gave p01/p99 of 0.29/30, a Nordic coastal frame 2.07/32.5,
        # and a range taken from either alone clips the other. 0.01 keeps
        # oligotrophic open ocean on the ramp; 50 covers the coastal p99 with
        # headroom (the frame's own maximum was 74.8).
        "value_range": (0.01, 50.0),
        # Chlorophyll spans four orders of magnitude, so a linear ramp shows
        # one estuary and a uniformly dark ocean.
        "log_scale": True,
        "colormap": "viridis",
        "label": "Chlorophyll-a concentration",
        "mask_flags": ("WATER",),
        "reject_flags": _WFR_REJECT + ("OCNN_FAIL",),
        "browse_reject_flags": _MARGINAL_CLOUD,
        "flag_member": "wqsf.nc",
        "flag_variable": "WQSF",
    },
    "tsm": {
        "kind": "swath",
        "candidates": (("tsm_nn.nc", "TSM_NN"),),
        "units": "g m-3",
        "value_range": (0.01, 100.0),
        "log_scale": True,
        "colormap": "magma",
        "label": "Total suspended matter",
        "mask_flags": ("WATER",),
        "reject_flags": _WFR_REJECT + ("OCNN_FAIL",),
        "browse_reject_flags": _MARGINAL_CLOUD,
        "flag_member": "wqsf.nc",
        "flag_variable": "WQSF",
    },
    "kd490": {
        "kind": "swath",
        "candidates": (("trsp.nc", "kd_490"), ("trsp.nc", "KD490_M07")),
        "units": "m-1",
        "value_range": (0.01, 10.0),
        "log_scale": True,
        "colormap": "viridis",
        "label": "Diffuse attenuation at 490 nm",
        "mask_flags": ("WATER",),
        "reject_flags": _WFR_REJECT,
        "browse_reject_flags": _MARGINAL_CLOUD,
        "flag_member": "wqsf.nc",
        "flag_variable": "WQSF",
    },
    "gifapar": {
        "kind": "swath",
        "candidates": (("gifapar.nc", "GIFAPAR"),),
        "units": "1",
        "value_range": (0.0, 1.0),
        "log_scale": False,
        "colormap": "brown_green",
        "label": "Green instantaneous FAPAR",
        "mask_flags": ("LAND",),
        "reject_flags": _LFR_REJECT + ("GIFAPAR_FAIL",),
        "browse_reject_flags": _MARGINAL_CLOUD,
        "flag_member": "lqsf.nc",
        "flag_variable": "LQSF",
    },
    "otci": {
        "kind": "swath",
        "candidates": (("otci.nc", "OTCI"),),
        "units": "1",
        "value_range": (0.0, 5.0),
        "log_scale": False,
        "colormap": "brown_green",
        "label": "Terrestrial chlorophyll index",
        "mask_flags": ("LAND",),
        "reject_flags": _LFR_REJECT + ("OTCI_FAIL",),
        "browse_reject_flags": _MARGINAL_CLOUD,
        "flag_member": "lqsf.nc",
        "flag_variable": "LQSF",
    },
})

# Geolocation member and variable names, per instrument. OLCI keeps them in
# their own member; SLSTR names them with an ``_in`` suffix for the nadir grid;
# the sea-surface-temperature product keeps plain Float32 ``lat``/``lon`` beside
# the data, needing no scaling at all.
GEO_MEMBER = "geo_coordinates.nc"
OLCI_GEOLOCATION = (GEO_MEMBER, "latitude", "longitude")
# Band centres in nm, for naming the three composite bands.
BAND_LABEL = {4: 490, 6: 560, 8: 665}
SLSTR_GEOLOCATION = ("geodetic_in.nc", "latitude_in", "longitude_in")

# Physical units are what gets stored. A conversion is applied before writing
# and recorded in the provenance, so a file always says what its numbers are.
_UNIT_CONVERSIONS: dict[str, tuple[float, float, str]] = {
    # name: (scale, offset, label)
    "native": (1.0, 0.0, ""),
    "celsius": (1.0, -273.15, "degC"),
    "kelvin": (1.0, 0.0, "K"),
}
# Above this many source pixels a decimated geolocation grid is faster. Measured
# on real frames at 0.003 deg: a 1.4 Mpx frame warps in 2.8 s at step 1 against
# 3.5 s at step 8, while a 19.9 Mpx frame takes 39.4 s at step 1 against 5.6 s
# at step 8. The crossover lies between, and 4 Mpx is also where the plan draws
# the line for an interactive request.
EXACT_WARP_PIXEL_LIMIT = 4_000_000
AUTO_GEOLOCATION_STEP = 8

S3_PRODUCTS.update({
    "lst": {
        "kind": "swath",
        "candidates": (("LST_in.nc", "LST"),),
        "geolocation": SLSTR_GEOLOCATION,
        "units": "degC",
        "unit": "celsius",
        # Q7's rule gives 10-45 from a June Nordic frame (masked p01/p50/p99 of
        # 12.0 / 26.2 / 43.0 °C), and a February polar frame medians at -31 °C.
        # No single range satisfies both: the plan's note that "a Nordic winter
        # wants -10 to 40" is the practical compromise, with the top widened to
        # cover the measured summer p99. A polar winter scene will clip at the
        # bottom, which is a real limitation and an open question for a
        # reviewer rather than something to decide from two frames.
        "value_range": (-10.0, 45.0),
        "log_scale": False,
        "colormap": "magma",
        "label": "Land surface temperature",
        "ground_sampling_m": SLSTR_GROUND_SAMPLING_M,
        "mask_flags": ("land",),
        "reject_flags": ("summary_cloud", "unfilled", "summary_pointing", "duplicate"),
        "browse_reject_flags": (),
        "flag_member": "flags_in.nc",
        "flag_variable": "confidence_in",
        # A second condition on a different variable. The exception word lives
        # in the science member, not the flag member, and any bit set at all
        # means the retrieval did not complete.
        "extra_masks": (
            {"member": "LST_in.nc", "variable": "exception", "reject_any": True},
        ),
    },
    "sst": {
        "kind": "swath",
        # The GHRSST member names are generated, so they are matched by shape.
        # DUAL is the two-view retrieval and the default; NADIR is the wider
        # swath. A view is one choice spanning three table entries - the
        # variable's member, the geolocation's and the flag member's - so it is
        # requested as ``view`` and sets all three together. Overriding
        # ``variable`` alone cannot do it: the swath path resolves the variable
        # from ``candidates``, so a bare variable request was accepted and
        # ignored.
        "views": {
            "dual": {
                "candidates": (("*_DUAL_*.nc", "sea_surface_temperature"),),
                "geolocation": ("*_DUAL_*.nc", "lat", "lon"),
                "flag_member": "*_DUAL_*.nc",
                "extra_masks": (
                    {"member": "*_DUAL_*.nc", "variable": "quality_level", "min_value": 4},
                ),
            },
            "nadir": {
                "candidates": (("*_NADIR_*.nc", "sea_surface_temperature"),),
                "geolocation": ("*_NADIR_*.nc", "lat", "lon"),
                "flag_member": "*_NADIR_*.nc",
                "extra_masks": (
                    {"member": "*_NADIR_*.nc", "variable": "quality_level", "min_value": 4},
                ),
            },
        },
        "view": "dual",
        "candidates": (("*_DUAL_*.nc", "sea_surface_temperature"),),
        "geolocation": ("*_DUAL_*.nc", "lat", "lon"),
        "units": "degC",
        "unit": "celsius",
        "value_range": (-2.0, 20.0),
        "log_scale": False,
        "colormap": "magma",
        "label": "Sea surface temperature",
        "ground_sampling_m": SLSTR_GROUND_SAMPLING_M,
        "mask_flags": (),
        "reject_flags": ("land", "ice"),
        "browse_reject_flags": (),
        "flag_member": "*_DUAL_*.nc",
        "flag_variable": "l2p_flags",
        # quality_level is an ordered enumeration, not a bit field: 0 no_data,
        # 1 cloud, 2 worst, 3 low, 4 acceptable, 5 best. The mission's own
        # recommendation for a quantitative use is 4 or better; 3 buys coverage
        # at a cost in accuracy, which is Q13 and the owner's provisional answer.
        "extra_masks": (
            {"member": "*_DUAL_*.nc", "variable": "quality_level", "min_value": 4},
        ),
    },
})

S3_PRODUCTS.update({
    "truecolor": {
        "kind": "rgb",
        # Red, green, blue. OLCI has no true blue band at 443 that is clean
        # over land, so 490 nm stands in - the usual choice for an OLCI
        # composite, and the one the plan proposed.
        "bands": (8, 6, 4),
        "geolocation": OLCI_GEOLOCATION,
        "units": "1",
        "unit": "native",
        # Top-of-atmosphere reflectance, measured on a real frame: 0.04-0.25
        # over open water, median 0.52 over cloud. 0.6 puts cloud near white
        # and leaves the sea and the land in the lower half of the ramp.
        "value_range": (0.0, 0.6),
        # Below one, to lift the dark end: without it the sea is black and the
        # land is nearly black under white cloud.
        "gamma": 0.6,
        "log_scale": False,
        "colormap": "grey",            # unused for an RGB composite
        "label": "True colour (top-of-atmosphere reflectance)",
        "mask_flags": (),
        "reject_flags": ("invalid", "saturated"),
        "browse_reject_flags": (),
        "flag_member": "qualityFlags.nc",
        "flag_variable": "quality_flags",
    },
})

# Which products each Sentinel-3 type yields.
S3_TYPE_PRODUCTS: dict[str, tuple[str, ...]] = {
    "SY_2_VG1": ("ndvi",),
    "SY_2_V10": ("ndvi",),
    "OL_2_WFR": ("chl", "tsm", "kd490"),
    "OL_2_WRR": ("chl", "tsm", "kd490"),
    "OL_2_LFR": ("gifapar", "otci"),
    "OL_2_LRR": ("gifapar", "otci"),
    "SL_2_LST": ("lst",),
    "SL_2_WST": ("sst",),
    "OL_1_EFR": ("truecolor",),
    "OL_1_ERR": ("truecolor",),
}

S3_DEFAULT_PRODUCTS = ("ndvi",)

S3_PRESETS: dict[str, dict[str, Any]] = {
    "full": {},
    # Two different things have to get coarser, and only one of them is obvious.
    # ``decimate`` reads fewer source pixels, which is cheap because GDAL does
    # it while decoding. But on a swath the output grid is set by the product's
    # ground sampling, so decimating the read alone gives a raster of exactly
    # the same size, only blurrier - and for a three-band composite that raster
    # is over the size cap. ``grid_multiplier`` is what actually makes the
    # output smaller.
    "quicklook": {"decimate": 8, "grid_multiplier": 8, "overview_factors": [2, 4]},
}

# The Synergy presets decimate the raster; an OLCI swath is better served by a
# coarser output grid, since the warp cost follows the output.
S3_SWATH_PRESETS: dict[str, dict[str, Any]] = {
    "full": {},
    "quicklook": {"target_resolution": None, "overview_factors": [2, 4], "geoloc_step": 16},
}

# Why a pixel carries no value, for the second band. A point query can then
# answer "masked: cloud" instead of "no data", which is the difference between a
# map that looks broken and one that explains itself.
REJECT_REASONS: dict[str, int] = {
    "valid": 0,
    "fill": 1,
    # Synergy status enumeration.
    "cloud": 2,
    "uncertain": 3,
    "unfilled": 4,
    # OLCI, kept apart because they are different kinds of doubt.
    "snow_ice": 5,
    "invalid": 6,
    "land": 7,
    "retrieval_failed": 8,
    "cloud_margin": 9,
    "cloud_ambiguous": 10,
    "cloud_shadow": 11,
    "other": 15,
}

# A flag name to the reason it stands for. Anything unlisted becomes "other",
# which is still more use to a point query than "no data".
_FLAG_REASONS: dict[str, str] = {
    "CLOUD": "cloud",
    "CLOUD_MARGIN": "cloud_margin",
    "CLOUD_AMBIGUOUS": "cloud_ambiguous",
    "CLOUD_SHADOW": "cloud_shadow",
    "SNOW_ICE": "snow_ice",
    "INVALID": "invalid",
    "LAND": "land",
    "AC_FAIL": "retrieval_failed",
    "OCNN_FAIL": "retrieval_failed",
    "CHLOR_A_FAIL": "retrieval_failed",
    "GIFAPAR_FAIL": "retrieval_failed",
    "OTCI_FAIL": "retrieval_failed",
    "OTCI_BAD_IN": "retrieval_failed",
    "SATURATED": "invalid",
    "HIGHGLINT": "other",
    "WHITECAPS": "other",
    "HISOLZEN": "other",
    "cloud": "cloud",
    "uncertain": "uncertain",
    "unfilled": "unfilled",
}

# Reasons the browse image hides although the float product keeps them.
#
# The split exists because the two outputs answer to different audiences. The
# Float32 product serves a coverage service, whose users can filter for
# themselves and are better served by having the pixel and the reason than by
# having neither. The styled copy serves a map service, where a cloud edge in
# the picture is simply wrong.
#
# What goes in here is doubt about *contamination*: a pixel next to a cloud,
# possibly a cloud, or in a cloud's shadow. What stays out is doubt about
# *validity*: an invalid pixel, or one whose own retrieval reported failure,
# has no value worth keeping anywhere.
#
# It also happens to be where the data is. On a marine frame the water mask
# discarded 59 % of the pixels that had a value, and CLOUD_MARGIN alone was
# 70 % of that loss - a safety buffer around cloud, not a cloud detection.
BROWSE_HIDDEN_REASONS: tuple[int, ...] = (
    REJECT_REASONS["cloud_margin"],
    REJECT_REASONS["cloud_ambiguous"],
    REJECT_REASONS["cloud_shadow"],
)


def default_sentinel_s3_products(input_dataset: str) -> tuple[str, ...]:
    """The products this product type can yield, from its name."""
    from .profiles import detect_nbs_sentinel_product

    product_type = detect_nbs_sentinel_product(str(input_dataset))["product_type"]
    return S3_TYPE_PRODUCTS.get(product_type, S3_DEFAULT_PRODUCTS)


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #
_PRODUCT_OPTIONS = {"variable", "mask_flags", "reject_flags", "browse_reject_flags",
                    "value_range", "colormap", "decimate", "unit", "geoloc_step",
                    "log_scale", "extra_masks", "gamma", "bands", "view"}


def normalize_sentinel_s3_product_map(
    products: Any, *, default_products: tuple[str, ...] | None = None
) -> dict[str, dict[str, Any]]:
    """Validate a requested product map and fill in the defaults.

    ``mask_flags`` accepts ``"default"``, ``"none"``, or a list of flag names
    taken from the file's own flag table. An unknown name fails here rather
    than quietly masking nothing.
    """
    if products in (None, "", {}):
        products = {name: {} for name in (default_products or S3_DEFAULT_PRODUCTS)}
    if not isinstance(products, dict):
        raise ValueError(f"products must be a mapping of product name to options, not {type(products).__name__}")

    normalized: dict[str, dict[str, Any]] = {}
    for name, requested in products.items():
        key = str(name).strip().lower()
        if key not in S3_PRODUCTS:
            known = ", ".join(sorted(S3_PRODUCTS))
            raise ValueError(f"{name!r} is not a Sentinel-3 product pysent derives; it knows: {known}")
        if requested in (None, ""):
            requested = {}
        if not isinstance(requested, dict):
            raise ValueError(f"options for product {key!r} must be a mapping, not {type(requested).__name__}")
        unknown = set(requested) - _PRODUCT_OPTIONS
        if unknown:
            raise ValueError(f"product {key!r} has unknown option(s): {', '.join(sorted(unknown))}")

        defaults = S3_PRODUCTS[key]

        # A view is one request that sets three table entries. Resolve it
        # first, then treat its entries as this product's defaults.
        views = defaults.get("views") or {}
        view = requested.get("view")
        if view in (None, ""):
            view = defaults.get("view")
        else:
            view = str(view).strip().lower()
            if not views:
                raise ValueError(
                    f"product {key!r} has no views to choose between, so 'view' does not apply to it"
                )
            if view not in views:
                raise ValueError(
                    f"product {key!r}: view must be one of {', '.join(sorted(views))}, not {view!r}"
                )
        if view and views:
            defaults = {**defaults, **views[view], "view": view}

        mask_flags = requested.get("mask_flags", "default")
        if isinstance(mask_flags, str):
            if mask_flags.strip().lower() == "default":
                mask_flags = list(defaults["mask_flags"])
            elif mask_flags.strip().lower() == "none":
                mask_flags = []
            else:
                mask_flags = [mask_flags.strip()]
        elif isinstance(mask_flags, (list, tuple)):
            mask_flags = [str(item).strip() for item in mask_flags if str(item).strip()]
        else:
            raise ValueError(
                f"product {key!r}: mask_flags must be 'default', 'none' or a list of flag names, "
                f"not {type(mask_flags).__name__}"
            )

        reject_flags = requested.get("reject_flags", "default")
        if isinstance(reject_flags, str):
            lowered = reject_flags.strip().lower()
            if lowered == "default":
                reject_flags = list(defaults.get("reject_flags", ()))
            elif lowered == "none":
                reject_flags = []
            else:
                reject_flags = [reject_flags.strip()]
        elif isinstance(reject_flags, (list, tuple)):
            reject_flags = [str(item).strip() for item in reject_flags if str(item).strip()]
        else:
            raise ValueError(
                f"product {key!r}: reject_flags must be 'default', 'none' or a list of flag names, "
                f"not {type(reject_flags).__name__}"
            )

        # The browse mask is a separate audience's decision from the float
        # product's mask, so it is a separate option - and it has to be read
        # from the request. Taken only from ``**defaults`` below, a request for
        # ``browse_reject_flags: []`` passed validation and then silently kept
        # the default cloud margin.
        browse_reject_flags = requested.get("browse_reject_flags", "default")
        if isinstance(browse_reject_flags, str):
            lowered = browse_reject_flags.strip().lower()
            if lowered == "default":
                browse_reject_flags = list(defaults.get("browse_reject_flags", ()))
            elif lowered == "none":
                browse_reject_flags = []
            else:
                browse_reject_flags = [browse_reject_flags.strip()]
        elif isinstance(browse_reject_flags, (list, tuple)):
            browse_reject_flags = [
                str(item).strip() for item in browse_reject_flags if str(item).strip()
            ]
        else:
            raise ValueError(
                f"product {key!r}: browse_reject_flags must be 'default', 'none' or a list of "
                f"flag names, not {type(browse_reject_flags).__name__}"
            )

        decimate = requested.get("decimate")
        decimate = _coerce_decimate(decimate, key) if decimate not in (None, "") else None

        geoloc_step = requested.get("geoloc_step")
        if geoloc_step not in (None, "", "auto"):
            geoloc_step = int(geoloc_step)
            if geoloc_step not in GEOLOCATION_STEPS:
                raise ValueError(
                    f"product {key!r}: geoloc_step must be 'auto' or one of "
                    f"{', '.join(str(value) for value in GEOLOCATION_STEPS)}, not {geoloc_step}"
                )
        else:
            geoloc_step = None

        value_range = requested.get("value_range") or defaults["value_range"]
        try:
            low, high = (float(item) for item in value_range)
        except (TypeError, ValueError):
            raise ValueError(f"product {key!r}: value_range must be two numbers, not {value_range!r}") from None
        if not low < high:
            raise ValueError(f"product {key!r}: value_range must be (low, high) with low < high")

        colormap = str(requested.get("colormap") or defaults["colormap"])
        if colormap not in COLOUR_ANCHORS:
            raise ValueError(
                f"product {key!r}: {colormap!r} is not a colormap pysent ships; "
                f"it has: {', '.join(sorted(COLOUR_ANCHORS))}"
            )

        unit = str(requested.get("unit") or defaults.get("unit") or "native").strip().lower()
        if unit not in _UNIT_CONVERSIONS:
            raise ValueError(
                f"product {key!r}: unit must be one of {', '.join(_UNIT_CONVERSIONS)}, not {unit!r}"
            )

        log_scale = bool(requested.get("log_scale", defaults.get("log_scale", False)))
        if log_scale and low <= 0.0:
            raise ValueError(
                f"product {key!r}: a log colour scale needs a positive lower bound, not {low}"
            )

        asked_for_flags = any(
            name in requested for name in ("mask_flags", "reject_flags", "browse_reject_flags")
        )
        normalized[key] = {
            **defaults,
            # A requested flag name is strict; a default one may be absent in an
            # older baseline and is then skipped and reported.
            "flags_are_default": not asked_for_flags,
            "kind": defaults.get("kind", "grid"),
            "variable": str(requested.get("variable") or defaults.get("variable") or ""),
            # A requested variable has to reach the swath path too, which
            # resolves from the candidates: taking the candidates from the
            # table alone meant a requested variable was accepted and silently
            # ignored for every swath product. The member pattern stays the
            # table's - what a caller names is the variable inside it.
            "candidates": (
                tuple(
                    (member, str(requested["variable"]).strip())
                    for member, _variable in defaults.get("candidates", ())
                )
                if requested.get("variable")
                else defaults.get("candidates", ())
            ),
            "geolocation": defaults.get("geolocation", OLCI_GEOLOCATION),
            "extra_masks": tuple(requested.get("extra_masks", defaults.get("extra_masks", ()))),
            "mask_flags": mask_flags,
            "reject_flags": reject_flags,
            "browse_reject_flags": browse_reject_flags,
            "value_range": (low, high),
            "colormap": colormap,
            "decimate": decimate,
            "geoloc_step": geoloc_step,
            "log_scale": log_scale,
            "unit": unit,
            "gamma": float(requested.get("gamma", defaults.get("gamma", 1.0))),
            "bands": tuple(requested.get("bands", defaults.get("bands", ()))),
            # Log *packing* is detected from the file's units; this is only the
            # override, and it is a different thing from the log colour scale.
            "log10": defaults.get("log10", "auto"),
        }
    if not normalized:
        raise ValueError("no products were requested")
    return normalized


def _coerce_decimate(value: Any, key: str) -> int:
    try:
        decimate = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"product {key!r}: decimate must be a whole number of 1 or more, not {value!r}") from None
    if decimate < 1:
        raise ValueError(f"product {key!r}: decimate must be 1 or more, not {decimate}")
    return decimate


def build_sentinel_s3_output_filename(input_dataset: str, product: str, *, suffix: str = ".tif") -> str:
    """A default output name, from the product name and the derived quantity."""
    stem = Path(str(input_dataset).split("?")[0]).name
    for extension in (".zip", ".SEN3", ".sen3"):
        if stem.endswith(extension):
            stem = stem[: -len(extension)]
            break
    return f"{stem}_{product}{suffix}"


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def _resolve_processing_settings(processing_options: dict[str, Any] | None) -> dict[str, Any]:
    processing = apply_preset(processing_options, S3_PRESETS)

    context = str(processing.get("context") or "bulk").strip().lower()
    if context not in ("interactive", "bulk"):
        raise ValueError(f"context must be 'interactive' or 'bulk', not {context!r}")

    on_empty = str(processing.get("on_empty") or "raise").strip().lower()
    if on_empty not in ("raise", "write"):
        raise ValueError(f"on_empty must be 'raise' or 'write', not {on_empty!r}")

    requested_threads = processing.get("gdal_num_threads")
    if requested_threads in (None, ""):
        gdal_num_threads = str(available_cpus())
    else:
        text = str(requested_threads).strip().upper()
        gdal_num_threads = text if text == "ALL_CPUS" else str(max(1, int(text)))

    overview_factors = processing.get("overview_factors") or [2, 4, 8]
    overview_factors = [int(factor) for factor in overview_factors if int(factor) > 1]

    decimate = processing.get("decimate")
    decimate = _coerce_decimate(decimate, "all") if decimate not in (None, "") else None

    geoloc_step = processing.get("geoloc_step")
    if geoloc_step in (None, ""):
        geoloc_step = "auto"

    grid_multiplier = float(processing.get("grid_multiplier") or 1.0)
    if grid_multiplier < 1.0:
        raise ValueError(f"grid_multiplier must be 1 or more, not {grid_multiplier}")

    resampling = str(processing.get("resampling") or "near").strip().lower()
    if resampling not in RESAMPLING_METHODS:
        raise ValueError(f"resampling must be one of {', '.join(RESAMPLING_METHODS)}, not {resampling!r}")

    target_epsg = processing.get("target_epsg")
    target_epsg = str(target_epsg).strip() if target_epsg not in (None, "") else None
    target_resolution = processing.get("target_resolution")
    target_resolution = float(target_resolution) if target_resolution not in (None, "") else None

    return {
        "context": context,
        "on_empty": on_empty,
        "decimate": decimate,
        "geoloc_step": geoloc_step,
        "grid_multiplier": grid_multiplier,
        "resampling": resampling,
        "target_epsg": target_epsg,
        "target_resolution": target_resolution,
        "overview_factors": overview_factors,
        "compression": str(processing.get("compression") or "DEFLATE").upper(),
        "predictor": int(processing.get("predictor") or 3),
        "styled": bool(processing.get("styled", True)),
        "thumbnail": bool(processing.get("thumbnail", True)),
        "reject_reason": bool(processing.get("reject_reason", False)),
        "max_output_pixels": int(processing.get("max_output_pixels") or (40_000_000 if context == "interactive" else 150_000_000)),
        "work_dir": resolve_work_dir(processing.get("work_dir")),
        "runtime": resolve_gdal_runtime(processing, requested_threads, gdal_num_threads),
        "gdal_num_threads": gdal_num_threads,
    }


# --------------------------------------------------------------------------- #
# One product
# --------------------------------------------------------------------------- #
def _apply_extra_mask(
    *,
    spec: dict[str, Any],
    root: str,
    scratch: Path,
    shape: tuple[int, int],
    decimate: int,
) -> tuple[np.ndarray, str]:
    """One further condition, as a rejection mask and a label for the provenance.

    Three forms, each the natural reading of the variable it applies to:

    ``reject_any``
        any bit set at all. An exception word says the retrieval did not
        complete, and there is no bit of it worth keeping.
    ``min_value``
        an ordered enumeration, kept at or above a level. Quality level runs
        0 no-data, 1 cloud, 2 worst, 3 low, 4 acceptable, 5 best, so a
        threshold is the only sensible reading - a bit test would be nonsense.
    ``reject``
        flag names, as elsewhere.
    """
    local = extract_member(root, spec["member"], scratch)
    dataset = open_sen3_variable(local, spec["variable"])
    band = dataset.GetRasterBand(1)
    raw = band.ReadAsArray(buf_xsize=shape[1], buf_ysize=shape[0])
    table = read_flag_table(band)
    dataset = None

    if spec.get("reject_any"):
        return raw != 0, f"{spec['variable']}!=0"
    if "min_value" in spec:
        minimum = int(spec["min_value"])
        return raw < minimum, f"{spec['variable']}>={minimum}"
    names = list(spec.get("reject", ()))
    if names:
        selected, _ = flag_selection(raw, table, names, allow_missing=True)
        return selected, f"{spec['variable']} not {','.join(names)}"
    return np.zeros(shape, dtype=bool), spec["variable"]


def _swath_reject_codes(
    *,
    values: np.ndarray,
    flags: np.ndarray | None,
    table: list[dict[str, Any]],
    kept: np.ndarray,
    browse_clean: np.ndarray,
    reject: list[str],
    marginal: list[str],
    extra_rejected: np.ndarray | None = None,
) -> np.ndarray:
    """Why each pixel is not a plain measurement, as :data:`REJECT_REASONS` codes.

    Evaluated from least to most decisive, so a pixel ends up with the firmest
    reason that applies: one that is both beside a cloud and invalid reads as
    invalid.

    The last step is the subtle one. ``valid`` is written only where the pixel
    survived the hard mask **and** carries no marginal doubt. A pixel the float
    product keeps but the browse image hides has to retain its marginal code -
    that code is the only thing that tells the styled copy to hide it, and the
    only thing a point query can report.
    """
    codes = np.full(values.shape, REJECT_REASONS["other"], dtype=np.float32)
    if flags is not None and table:
        # Marginal first, then hard, so a hard reason overwrites a marginal one.
        for group in (marginal, reject):
            for name in group:
                reason = _FLAG_REASONS.get(name, "other")
                selected, _ = flag_selection(flags, table, [name], allow_missing=True)
                codes[selected] = REJECT_REASONS[reason]
    if extra_rejected is not None:
        # A further condition is firmer than a flag: it is why the value is not
        # a measurement, so it outranks a contamination code.
        codes[extra_rejected] = REJECT_REASONS["retrieval_failed"]
    codes[~np.isfinite(values)] = REJECT_REASONS["fill"]
    codes[kept & browse_clean] = REJECT_REASONS["valid"]
    return codes


def _reject_codes(
    *,
    values: np.ndarray,
    flags: np.ndarray | None,
    table: list[dict[str, Any]],
    kept: np.ndarray,
) -> np.ndarray:
    """Why each masked pixel was dropped, as the codes in :data:`REJECT_REASONS`."""
    codes = np.full(values.shape, REJECT_REASONS["other"], dtype=np.uint8)
    codes[kept] = REJECT_REASONS["valid"]
    missing = ~np.isfinite(values)
    if flags is not None:
        defined = {row["name"] for row in table}
        for reason in ("cloud", "uncertain", "unfilled"):
            if reason in defined:
                selected, _ = flag_selection(flags, table, [reason])
                selected = selected & ~kept
                codes[selected] = REJECT_REASONS[reason]
    # Fill wins: a pixel with no value at all is not "masked by cloud".
    codes[missing & ~kept] = REJECT_REASONS["fill"]
    return codes




def _assemble_result(
    *,
    product: str,
    options: dict[str, Any],
    float_path: Path,
    settings: dict[str, Any],
    metadata: dict[str, str],
    manifest: dict[str, Any],
    epsg: int,
    resolution: float,
    decimate: int,
    source_valid: float,
    table: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read back what was written and describe it, then write the styled pair.

    Shared by the regular-grid and swath paths so the two cannot drift in what
    they report. The float product is reopened rather than described from
    memory, so every number returned is a property of the file on disk.
    """
    from osgeo import gdal

    written = gdal.Open(str(float_path))
    values = written.GetRasterBand(1).ReadAsArray()
    transform = written.GetGeoTransform()
    width, height = written.RasterXSize, written.RasterYSize
    extra_has_reason = written.RasterCount >= 2
    written = None

    bounds = [
        transform[0],
        transform[3] + transform[5] * height,
        transform[0] + transform[1] * width,
        transform[3],
    ]
    result: dict[str, Any] = {
        "product": product,
        "path": str(float_path),
        "output_file": float_path.name,
        "epsg": int(epsg),
        "resolution": float(resolution),
        "bounds": [float(value) for value in bounds],
        "width": int(width),
        "height": int(height),
        "transform": [float(value) for value in transform],
        "decimate": int(decimate),
        "valid_fraction": valid_fraction(values),
        "source_valid_fraction": source_valid,
        "value_stats": value_statistics(values),
        "units": options["units"],
        "mask_flags": list(options["mask_flags"]),
        "reject_flags": list(options["reject_flags"]),
        "available_flags": [row["name"] for row in table],
        "baseline": manifest.get("baseline", ""),
        "style_hint": {
            "value_range": list(options["value_range"]),
            "colormap": options["colormap"],
            "scale": "log10" if options["log_scale"] else "linear",
            "label": options["label"],
        },
        # Q11: the point query samples the published raster, so nothing extra is
        # stored or served. With reject_reason on, band 2 says why a pixel is
        # empty, which lets a query answer "masked: cloud" rather than "no data".
        "query_source": {
            "path": str(float_path),
            "method": "sample_raster",
            "band": 1,
            "reject_reason_band": 2 if extra_has_reason else None,
            "reject_reasons": REJECT_REASONS if extra_has_reason else None,
        },
        # Pixels the float product keeps but the browse image hides, so a
        # caller knows the two are not the same picture.
        "browse_hidden_reasons": [
            name for name, code in REJECT_REASONS.items() if code in BROWSE_HIDDEN_REASONS
        ] if extra_has_reason and options["browse_reject_flags"] else [],
        "time": manifest.get("time", {}),
        "provenance": metadata,
    }
    result.update(extra or {})

    if settings["styled"]:
        styled_path = float_path.with_name(f"{float_path.stem}_styled.tif")
        write_styled_copy(
            source_path=str(float_path), output_path=styled_path,
            value_range=options["value_range"], colormap=options["colormap"],
            settings=settings, metadata=metadata, log_scale=options["log_scale"],
            hide_codes=BROWSE_HIDDEN_REASONS if extra_has_reason else (),
            reason_band=2 if extra_has_reason else None,
        )
        result["styled_path"] = str(styled_path)

    if settings["thumbnail"]:
        thumbnail_path = float_path.with_name(f"{float_path.stem}_thumb.png")
        write_thumbnail(
            source_path=str(float_path), output_path=thumbnail_path,
            value_range=options["value_range"], colormap=options["colormap"],
            log_scale=options["log_scale"],
            hide_codes=BROWSE_HIDDEN_REASONS if extra_has_reason else (),
            reason_band=2 if extra_has_reason else None,
            # Sentinel-3 thumbnails average the data, not the overviews.
            materialise=True,
        )
        result["thumbnail_path"] = str(thumbnail_path)

    return result


def _process_one_product(
    *,
    product: str,
    options: dict[str, Any],
    input_dataset: str,
    root: str,
    output_path: str,
    settings: dict[str, Any],
    scratch: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    if options["kind"] == "rgb":
        return _process_rgb_product(
            product=product, options=options, input_dataset=input_dataset, root=root,
            output_path=output_path, settings=settings, scratch=scratch, manifest=manifest,
        )
    if options["kind"] == "swath":
        return _process_swath_product(
            product=product, options=options, input_dataset=input_dataset, root=root,
            output_path=output_path, settings=settings, scratch=scratch, manifest=manifest,
        )
    return _process_grid_product(
        product=product, options=options, input_dataset=input_dataset, root=root,
        output_path=output_path, settings=settings, scratch=scratch, manifest=manifest,
    )


def _apply_flag_mask(
    *,
    values: np.ndarray,
    flags: np.ndarray | None,
    table: list[dict[str, Any]],
    keep: list[str],
    reject: list[str],
    allow_missing: bool = False,
) -> tuple[np.ndarray, list[str]]:
    """Pixels that are finite, carry every required flag and none of the refused.

    Two lists rather than one because that is how the product documentation
    phrases a mask: a water product keeps ``WATER`` and rejects eleven separate
    failure conditions, and neither half implies the other.
    """
    kept = np.isfinite(values)
    if flags is None or not table:
        return kept, []
    missing: list[str] = []
    if keep:
        selected, absent = flag_selection(flags, table, keep, allow_missing=allow_missing)
        kept &= selected
        missing += absent
    if reject:
        selected, absent = flag_selection(flags, table, reject, allow_missing=allow_missing)
        kept &= ~selected
        missing += absent
    return kept, missing


def _resolve_geoloc_step(requested: Any, source_pixels: int) -> int:
    """The geolocation decimation to warp with.

    ``auto`` picks on cost, because accuracy barely depends on it once the
    half-block offset is right: measured 88.1 % of source pixels landing in
    their own output cell at step 1 against 86.7 % at step 16. Cost does depend
    on it, and the direction reverses with frame size - a small frame is
    fastest exact, a full OLCI frame is seven times faster decimated.
    """
    if requested in (None, "", "auto"):
        return 1 if source_pixels <= EXACT_WARP_PIXEL_LIMIT else AUTO_GEOLOCATION_STEP
    return int(requested)


def _process_swath_product(
    *,
    product: str,
    options: dict[str, Any],
    input_dataset: str,
    root: str,
    output_path: str,
    settings: dict[str, Any],
    scratch: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    from osgeo import gdal

    decimate = options["decimate"] or settings["decimate"] or 1

    # The science variable, by whichever name this baseline uses.
    local, member, variable = resolve_variable(root, list(options["candidates"]), scratch)
    science = open_sen3_variable(local, variable)
    science_band = science.GetRasterBand(1)
    values = read_packed(science_band, decimate=decimate, log10=options["log10"])
    science = None

    # Geolocation. Stored as Int32 micro-degrees with a 1e-6 scale, and GDAL
    # uses a GEOLOCATION array exactly as it finds it, so it has to be scaled
    # into real degrees first - read_packed does that and masks the fill.
    geo_member, lat_name, lon_name = options["geolocation"]
    geo_local = extract_member(root, geo_member, scratch)
    lat_ds = open_sen3_variable(geo_local, lat_name)
    lon_ds = open_sen3_variable(geo_local, lon_name)
    # read_packed applies whatever scaling the file declares, so an Int32
    # micro-degree array and a plain Float32 one both come back in degrees.
    lat = read_packed(lat_ds.GetRasterBand(1), decimate=decimate, log10=False)
    lon = read_packed(lon_ds.GetRasterBand(1), decimate=decimate, log10=False)
    lat_ds = lon_ds = None
    if lat.shape != values.shape:
        raise ValueError(
            f"{product}: {member}:{variable} is {values.shape} but the geolocation is {lat.shape}; "
            "this variable is not on the full-resolution grid"
        )

    flags = None
    table: list[dict[str, Any]] = []
    if options["mask_flags"] or options["reject_flags"]:
        flag_local = extract_member(root, options["flag_member"], scratch)
        flag_ds = open_sen3_variable(flag_local, options["flag_variable"])
        flag_band = flag_ds.GetRasterBand(1)
        table = read_flag_table(flag_band)
        flags = flag_band.ReadAsArray(buf_xsize=values.shape[1], buf_ysize=values.shape[0])
        flag_ds = None

    kept, missing_flags = _apply_flag_mask(
        values=values, flags=flags, table=table,
        keep=list(options["mask_flags"]), reject=list(options["reject_flags"]),
        # A default mask may name a flag an older baseline does not define.
        allow_missing=options["flags_are_default"],
    )
    marginal = list(options["browse_reject_flags"])
    # True where no marginal doubt applies. The float product keeps the rest;
    # the browse image hides them, told apart by the reason band.
    if marginal and flags is not None and table:
        selected, _ = flag_selection(flags, table, marginal, allow_missing=True)
        browse_clean = ~selected
    else:
        browse_clean = np.ones(values.shape, dtype=bool)

    # Further conditions on other variables. A product may need more than one:
    # land surface temperature is masked by a confidence word *and* by an
    # exception word that lives in the science member, and sea surface
    # temperature by a flag word *and* by an ordered quality enumeration.
    extra_rejected = np.zeros(values.shape, dtype=bool)
    extra_applied: list[str] = []
    for spec in options.get("extra_masks", ()):
        rejected, label = _apply_extra_mask(
            spec=spec, root=root, scratch=scratch, shape=values.shape, decimate=decimate,
        )
        extra_rejected |= rejected
        extra_applied.append(label)
    kept = kept & ~extra_rejected

    unit_scale, unit_offset, unit_label = _UNIT_CONVERSIONS[options["unit"]]
    if unit_scale != 1.0 or unit_offset != 0.0:
        values = (values * np.float32(unit_scale) + np.float32(unit_offset)).astype(np.float32)

    masked = np.where(kept, values, np.nan).astype(np.float32)
    kept_count = int(np.count_nonzero(kept))
    source_valid = valid_fraction(values)
    reason_codes = _swath_reject_codes(
        values=values, flags=flags, table=table, kept=kept, browse_clean=browse_clean,
        reject=list(options["reject_flags"]), marginal=marginal,
        extra_rejected=extra_rejected,
    )

    if kept_count == 0 and settings["on_empty"] == "raise":
        raise EmptySceneError(
            f"{product}: no pixel of this frame survives the mask "
            f"(keep {', '.join(options['mask_flags']) or 'anything'}; "
            f"reject {', '.join(options['reject_flags']) or 'nothing'}), so there is nothing to map "
            f"({100 * source_valid:.1f} % of the frame carried a value before masking). "
            "A water product over land, or a land product over sea, is normally empty."
        )

    epsg, resolution = choose_grid(
        lat, lon,
        epsg=settings["target_epsg"] or "auto",
        resolution=settings["target_resolution"],
        ground_sampling_m=(options.get("ground_sampling_m") or OLCI_GROUND_SAMPLING_M)
        * settings["grid_multiplier"],
    )
    bounds = grid_bounds(lat, lon, epsg, resolution)
    cells = ((bounds[2] - bounds[0]) / resolution) * ((bounds[3] - bounds[1]) / resolution)
    if cells > settings["max_output_pixels"]:
        raise ValueError(
            f"{product}: a {epsg} grid at {resolution:g} would be {cells:,.0f} cells, over the "
            f"{settings['max_output_pixels']:,} limit for a {settings['context']} request; "
            "ask for a coarser resolution or run it as a bulk job"
        )

    step = _resolve_geoloc_step(
        options["geoloc_step"] if options["geoloc_step"] is not None else settings["geoloc_step"],
        values.size,
    )

    metadata = _product_metadata(
        product=product, options=options, input_dataset=input_dataset, manifest=manifest,
        decimate=decimate, resolution=resolution, epsg=epsg,
        member=member, variable=variable, geoloc_step=step,
    )

    float_path = Path(output_path)
    with atomic_output(float_path) as temporary:
        warp_swath(
            values=masked, lat=lat, lon=lon, output_path=str(temporary),
            epsg=epsg, resolution=resolution, bounds=bounds,
            resampling=settings["resampling"], scratch=scratch,
            creation_options=creation_options(settings, float_data=True),
            num_threads=settings["gdal_num_threads"],
            geoloc_step=step,
            # A geolocation array GDAL cannot use does not raise: it writes a
            # correctly sized raster with nothing in it. One per cent of the
            # kept source pixels is far below any real result and far above
            # zero, so it catches that without rejecting a coarse grid.
            # With nothing kept there is nothing to check: this guard asks
            # "did the warp lose the data?", and a frame whose mask kept no
            # pixel has none to lose. Left at 1 it turned on_empty="write"
            # into a different exception instead of an empty product.
            minimum_valid=(kept_count // 100 or 1) if kept_count else None,
            categorical=reason_codes,
        )
        finish_float_product(path=str(temporary), settings=settings, metadata=metadata)

    return _assemble_result(
        product=product, options=options, float_path=float_path, settings=settings,
        metadata=metadata, manifest=manifest, epsg=epsg, resolution=resolution,
        decimate=decimate, source_valid=source_valid, table=table,
        extra={"geoloc_step": step, "member": member, "variable": variable,
               "unavailable_flags": missing_flags,
               "footprint": footprint_polygon(lat, lon)},
    )


def _process_rgb_product(
    *,
    product: str,
    options: dict[str, Any],
    input_dataset: str,
    root: str,
    output_path: str,
    settings: dict[str, Any],
    scratch: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """A three-band composite from Level-1 radiance.

    Different from every other product here in two ways. It needs three bands
    rather than one, so it warps three times onto one grid. And what it mainly
    produces is a *picture*: the Float32 bands are kept because the numbers are
    still a measurement, but they are top-of-atmosphere reflectance derived by
    this module and not a validated product, which the provenance says.
    """
    from osgeo import gdal, osr

    from ._olci_l1 import quality_mask, solar_zenith, toa_reflectance

    decimate = options["decimate"] or settings["decimate"] or 1
    bands = list(options["bands"])

    geo_member, lat_name, lon_name = options["geolocation"]
    geo_local = extract_member(root, geo_member, scratch)
    lat_ds = open_sen3_variable(geo_local, lat_name)
    lon_ds = open_sen3_variable(geo_local, lon_name)
    lat = read_packed(lat_ds.GetRasterBand(1), decimate=decimate, log10=False)
    lon = read_packed(lon_ds.GetRasterBand(1), decimate=decimate, log10=False)
    lat_ds = lon_ds = None

    # One expansion of the solar-zenith tie grid, shared by the three bands:
    # it is the expensive part and it does not depend on the band.
    zenith = solar_zenith(root, scratch, lat.shape)

    rejected, missing_flags, bright = quality_mask(
        root, scratch, lat.shape, list(options["reject_flags"])
    )

    reflectances: list[np.ndarray] = []
    for band in bands:
        values = toa_reflectance(
            root, band, scratch, decimate=decimate, solar_zenith_degrees=zenith
        )
        if values.shape != lat.shape:
            raise ValueError(
                f"{product}: band {band} is {values.shape} but the geolocation is {lat.shape}"
            )
        reflectances.append(np.where(rejected, np.nan, values).astype(np.float32))

    kept = np.isfinite(reflectances[0])
    kept_count = int(np.count_nonzero(kept))
    if kept_count == 0 and settings["on_empty"] == "raise":
        raise EmptySceneError(
            f"{product}: no pixel of this frame survives the quality mask "
            f"({', '.join(options['reject_flags']) or 'nothing rejected'}), so there is nothing to draw"
        )

    epsg, resolution = choose_grid(
        lat, lon,
        epsg=settings["target_epsg"] or "auto",
        resolution=settings["target_resolution"],
        ground_sampling_m=(options.get("ground_sampling_m") or OLCI_GROUND_SAMPLING_M)
        * settings["grid_multiplier"],
    )
    bounds = grid_bounds(lat, lon, epsg, resolution)
    cells = ((bounds[2] - bounds[0]) / resolution) * ((bounds[3] - bounds[1]) / resolution)
    if cells * len(bands) > settings["max_output_pixels"]:
        raise ValueError(
            f"{product}: three bands on a {epsg} grid at {resolution:g} would be "
            f"{cells * len(bands):,.0f} cells, over the {settings['max_output_pixels']:,} limit for a "
            f"{settings['context']} request; ask for a coarser resolution or run it as a bulk job"
        )

    step = _resolve_geoloc_step(
        options["geoloc_step"] if options["geoloc_step"] is not None else settings["geoloc_step"],
        reflectances[0].size,
    )
    metadata = _product_metadata(
        product=product, options=options, input_dataset=input_dataset, manifest=manifest,
        decimate=decimate, resolution=resolution, epsg=epsg,
        member="Oa{:02d}/Oa{:02d}/Oa{:02d}_radiance.nc".format(*bands),
        variable="top-of-atmosphere reflectance (derived)", geoloc_step=step,
    )

    # Each band is warped on its own, onto the same grid, then stacked.
    warped: list[np.ndarray] = []
    transform = projection = None
    for band, values in zip(bands, reflectances):
        single = scratch / f"{product}_Oa{band:02d}.tif"
        warp_swath(
            values=values, lat=lat, lon=lon, output_path=str(single),
            epsg=epsg, resolution=resolution, bounds=bounds,
            resampling=settings["resampling"], scratch=scratch,
            creation_options=["TILED=YES", "COMPRESS=NONE"],
            num_threads=settings["gdal_num_threads"], geoloc_step=step,
            # With nothing kept there is nothing to check: this guard asks
            # "did the warp lose the data?", and a frame whose mask kept no
            # pixel has none to lose. Left at 1 it turned on_empty="write"
            # into a different exception instead of an empty product.
            minimum_valid=(kept_count // 100 or 1) if kept_count else None,
        )
        dataset = gdal.Open(str(single))
        warped.append(dataset.GetRasterBand(1).ReadAsArray())
        transform, projection = dataset.GetGeoTransform(), dataset.GetProjection()
        dataset = None
        Path(single).unlink(missing_ok=True)

    float_path = Path(output_path)
    with atomic_output(float_path) as temporary:
        write_rgb_product(
            path=str(temporary), bands=warped, transform=transform, projection=projection,
            settings=settings, metadata=metadata,
            descriptions=[f"Oa{band:02d}_{BAND_LABEL[band]}nm_toa_reflectance" for band in bands],
        )
        finish_float_product(path=str(temporary), settings=settings, metadata=metadata)

    statistics = value_statistics(warped[0])
    result: dict[str, Any] = {
        "product": product,
        "path": str(float_path),
        "output_file": float_path.name,
        "epsg": int(epsg),
        "resolution": float(resolution),
        "bounds": [float(value) for value in bounds],
        "width": int(warped[0].shape[1]),
        "height": int(warped[0].shape[0]),
        "transform": [float(value) for value in transform],
        "decimate": int(decimate),
        "geoloc_step": step,
        "bands": [f"Oa{band:02d}" for band in bands],
        "wavelengths_nm": [BAND_LABEL[band] for band in bands],
        "valid_fraction": valid_fraction(warped[0]),
        "source_valid_fraction": valid_fraction(reflectances[0]),
        "value_stats": statistics,
        "units": options["units"],
        "mask_flags": list(options["mask_flags"]),
        "reject_flags": list(options["reject_flags"]),
        "browse_reject_flags": [],
        "unavailable_flags": missing_flags,
        "available_flags": [],
        "baseline": manifest.get("baseline", ""),
        "bright_fraction": float(np.mean(bright[kept])) if kept_count else 0.0,
        "style_hint": {
            "value_range": list(options["value_range"]),
            "gamma": options["gamma"],
            "composite": [f"Oa{band:02d}" for band in bands],
            "label": options["label"],
        },
        "query_source": {
            "path": str(float_path), "method": "sample_raster", "band": 1,
            "reject_reason_band": None, "reject_reasons": None,
        },
        "browse_hidden_reasons": [],
        "footprint": footprint_polygon(lat, lon),
        "time": manifest.get("time", {}),
        "provenance": metadata,
    }

    if settings["styled"]:
        styled_path = float_path.with_name(f"{float_path.stem}_styled.tif")
        write_rgb_styled(
            source_path=str(float_path), output_path=styled_path,
            value_range=options["value_range"], gamma=options["gamma"],
            settings=settings, metadata=metadata,
        )
        result["styled_path"] = str(styled_path)
    if settings["thumbnail"]:
        thumbnail_path = float_path.with_name(f"{float_path.stem}_thumb.png")
        write_rgb_thumbnail(
            source_path=str(float_path), output_path=thumbnail_path,
            value_range=options["value_range"], gamma=options["gamma"],
        )
        result["thumbnail_path"] = str(thumbnail_path)
    return result


def _process_grid_product(
    *,
    product: str,
    options: dict[str, Any],
    input_dataset: str,
    root: str,
    output_path: str,
    settings: dict[str, Any],
    scratch: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    from osgeo import gdal

    decimate = options["decimate"] or settings["decimate"] or 1

    member = extract_member(root, options["member"], scratch)
    dataset = open_sen3_variable(member, options["variable"])
    band = dataset.GetRasterBand(1)

    width = max(1, dataset.RasterXSize // decimate)
    height = max(1, dataset.RasterYSize // decimate)
    if width * height > settings["max_output_pixels"]:
        raise ValueError(
            f"{product}: a {width} x {height} raster is {width * height:,} pixels, over the "
            f"{settings['max_output_pixels']:,} limit for a {settings['context']} request; "
            "ask for a larger decimate or run it as a bulk job"
        )

    values = read_packed(band, decimate=decimate, log10=options["log10"])
    source_transform = dataset.GetGeoTransform()
    dataset = None

    flags = None
    table: list[dict[str, Any]] = []
    kept = np.isfinite(values)
    if options["mask_flags"]:
        flag_member = extract_member(root, options["flag_member"], scratch)
        flag_dataset = open_sen3_variable(flag_member, options["flag_variable"])
        flag_band = flag_dataset.GetRasterBand(1)
        table = read_flag_table(flag_band)
        flags = flag_band.ReadAsArray(buf_xsize=width, buf_ysize=height)
        flag_dataset = None
        selected, missing_flags = flag_selection(
            flags, table, list(options["mask_flags"]),
            allow_missing=options["flags_are_default"],
        )
        kept = kept & selected

    masked = np.where(kept, values, np.nan).astype(np.float32)
    usable = valid_fraction(masked)
    if usable == 0.0 and settings["on_empty"] == "raise":
        raise EmptySceneError(
            f"{product}: no pixel of this tile survives the mask "
            f"({', '.join(options['mask_flags']) or 'no flags'}), so there is nothing to map "
            f"({100 * valid_fraction(values):.1f} % of the tile carried a value before masking). "
            "A winter tile is often nearly all cloud or darkness."
        )

    # Decimation changes the pixel size but not the origin.
    transform = (
        source_transform[0],
        source_transform[1] * decimate,
        source_transform[2],
        source_transform[3],
        source_transform[4],
        source_transform[5] * decimate,
    )

    metadata = _product_metadata(
        product=product,
        options=options,
        input_dataset=input_dataset,
        manifest=manifest,
        decimate=decimate,
        resolution=abs(transform[1]),
    )

    float_path = Path(output_path)
    with atomic_output(float_path) as temporary:
        _write_float_product(
            path=str(temporary),
            values=masked,
            transform=transform,
            settings=settings,
            metadata=metadata,
            reject_codes=(
                _reject_codes(values=values, flags=flags, table=table, kept=kept)
                if settings["reject_reason"]
                else None
            ),
        )
        finish_float_product(path=str(temporary), settings=settings, metadata=metadata)

    bounds = [
        transform[0],
        transform[3] + transform[5] * height,
        transform[0] + transform[1] * width,
        transform[3],
    ]
    return _assemble_result(
        product=product, options=options, float_path=float_path, settings=settings,
        metadata=metadata, manifest=manifest, epsg=EPSG_LATLON, resolution=abs(transform[1]),
        decimate=decimate, source_valid=valid_fraction(values), table=table,
        extra={"footprint": manifest.get("footprint"),
               "unavailable_flags": locals().get("missing_flags", [])},
    )


def _write_float_product(
    *,
    path: str,
    values: np.ndarray,
    transform: tuple[float, ...],
    settings: dict[str, Any],
    metadata: dict[str, str],
    reject_codes: np.ndarray | None,
) -> None:
    from osgeo import gdal, osr

    bands = 2 if reject_codes is not None else 1
    driver = gdal.GetDriverByName("GTiff")
    # One Float32 band, plus a Byte band of reasons when asked for. Mixed types
    # are not possible in one GeoTIFF, so the reason band is Float32 too and
    # carries small whole numbers; the value is what matters, not the storage.
    dataset = driver.Create(
        path,
        values.shape[1],
        values.shape[0],
        bands,
        gdal.GDT_Float32,
        options=creation_options(settings, float_data=True),
    )
    dataset.SetGeoTransform(tuple(float(value) for value in transform))

    # The files declare an unnamed datum, so the CRS is assigned rather than
    # copied: the grid really is plain WGS 84 latitude-longitude.
    reference = osr.SpatialReference()
    reference.ImportFromEPSG(EPSG_LATLON)
    reference.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dataset.SetProjection(reference.ExportToWkt())

    first = dataset.GetRasterBand(1)
    first.SetNoDataValue(float("nan"))
    first.WriteArray(values)
    first.SetDescription(metadata.get("PYSENT_PRODUCT", "value"))
    if reject_codes is not None:
        second = dataset.GetRasterBand(2)
        second.WriteArray(reject_codes.astype(np.float32))
        second.SetDescription("reject_reason")
    dataset.SetMetadata(metadata)
    dataset.FlushCache()
    dataset = None


def _product_metadata(
    *,
    product: str,
    options: dict[str, Any],
    input_dataset: str,
    manifest: dict[str, Any],
    decimate: int,
    resolution: float,
    epsg: int = EPSG_LATLON,
    member: str | None = None,
    variable: str | None = None,
    geoloc_step: int | None = None,
) -> dict[str, str]:
    return provenance(
        product=product,
        source=Path(str(input_dataset).split("?")[0]).name,
        product_type=manifest.get("productType", ""),
        baseline=manifest.get("baseline", ""),
        member=member or options.get("member"),
        variable=variable or options.get("variable"),
        mask_flags=",".join(options["mask_flags"]) or "none",
        reject_flags=",".join(options["reject_flags"]) or "none",
        browse_reject_flags=",".join(options["browse_reject_flags"]) or "none",
        grid_epsg=str(int(epsg)),
        grid_resolution=f"{resolution:g}",
        decimate=str(decimate),
        geoloc_step=str(geoloc_step) if geoloc_step is not None else None,
        units=options["units"],
        style_range=f"{options['value_range'][0]:g},{options['value_range'][1]:g}",
        style_scale="log10" if options["log_scale"] else "linear",
        colormap=options["colormap"],
        # Only a picture product has a gamma, and only it needs one recorded:
        # without it a restyle of a true-colour float cannot reproduce the
        # rendering the pipeline produced.
        style_gamma=f"{options['gamma']:g}" if options.get("kind") == "rgb" else None,
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def process_sentinel_s3_safe(
    *,
    input_dataset: str,
    output_dir: Path,
    products: dict[str, dict[str, Any]] | None = None,
    output_names: dict[str, str] | None = None,
    processing_options: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Derive map rasters from a Sentinel-3 product.

    ``input_dataset`` may be a local ``.zip``, an unpacked ``.SEN3`` directory,
    or a URL - a remote archive is read in place through
    ``/vsizip//vsicurl/`` and only the members needed cross the network, so
    there is no need to download a 143 MB product to read a 13 MB variable.

    Options in ``processing_options``:

    ``decimate``
        Read every n-th pixel. A Synergy tile is 8176 x 5601, so a quicklook
        rarely wants all of it; GDAL decimates while decoding, so this is
        cheaper than reading and shrinking.
    ``reject_reason``
        Add a second band saying *why* each empty pixel is empty - cloud,
        uncertain, unfilled or fill - so a point query can explain itself
        instead of answering "no data". Off by default.
    ``on_empty``
        ``raise`` (default) gives :class:`pysent.errors.EmptySceneError` when
        nothing survives the mask, which for a winter tile is normal.
    ``preset``
        ``full`` (default) or ``quicklook``, which decimates by 8.
    ``context``, ``work_dir``, ``gdal_num_threads``, ``gdal_cachemax_mb``,
    ``compression``, ``overview_factors``, ``styled``, ``thumbnail``
        As for the other pipelines.

    Returns one result dict per product, in request order.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    requested = normalize_sentinel_s3_product_map(
        products, default_products=default_sentinel_s3_products(input_dataset)
    )
    settings = _resolve_processing_settings(processing_options)
    names = dict(output_names or {})
    for product in requested:
        names.setdefault(product, build_sentinel_s3_output_filename(input_dataset, product))

    scratch_root = Path(settings["work_dir"]) if settings["work_dir"] else output_dir

    with scratch_dir(scratch_root) as scratch:
        with gdal_runtime(**settings["runtime"]), gdal_errors():
            root = sen3_root(input_dataset)
            compressed = check_members_stored(input_dataset)
            manifest = read_sen3_manifest(root)
            if compressed:
                # Not fatal, but worth saying: every member of every archive
                # inspected so far is stored uncompressed, which is what makes
                # reading one variable out of a remote product cheap.
                manifest["compressed_members"] = compressed

            jobs = [
                (
                    product,
                    {
                        "product": product,
                        "options": options,
                        "input_dataset": input_dataset,
                        "root": root,
                        "output_path": str(output_dir / names[product]),
                        "settings": settings,
                        "scratch": scratch,
                        "manifest": manifest,
                    },
                )
                for product, options in requested.items()
            ]
            return run_product_jobs(
                _process_one_product,
                jobs,
                parallel_mode="serial",
                workers=1,
                runtime=settings["runtime"],
            )


def _restyle_rgb(
    *,
    float_path: Path,
    value_range: tuple[float, float],
    gamma: float,
    tags: dict[str, str],
    output_dir: Path | None,
    styled: bool,
    thumbnail: bool,
    processing_options: dict[str, Any] | None,
) -> dict[str, Any]:
    """Re-render a three-band picture product as a picture.

    The same two writers the pipeline used, so a restyle differs from the
    original only in the range and the gamma it was given. No colour table and
    no reason band: a true-colour float has three bands of reflectance and
    nothing to hide behind a mask code.
    """
    settings = _resolve_processing_settings(processing_options)
    destination = Path(output_dir) if output_dir else float_path.parent
    destination.mkdir(parents=True, exist_ok=True)

    metadata = {
        **{key: value for key, value in tags.items() if key.startswith("PYSENT_")},
        "PYSENT_STYLE_RANGE": f"{value_range[0]:g},{value_range[1]:g}",
        "PYSENT_STYLE_GAMMA": f"{gamma:g}",
        "PYSENT_STYLE_SCALE": "linear",
    }
    written: dict[str, Any] = {
        "source": str(float_path),
        "value_range": [value_range[0], value_range[1]],
        "gamma": gamma,
        "scale": "linear",
        "rgb": True,
        "browse_hidden": False,
    }
    if styled:
        styled_path = destination / f"{float_path.stem}_styled.tif"
        write_rgb_styled(
            source_path=str(float_path), output_path=styled_path,
            value_range=value_range, gamma=gamma, settings=settings, metadata=metadata,
        )
        written["styled_path"] = str(styled_path)
    if thumbnail:
        thumbnail_path = destination / f"{float_path.stem}_thumb.png"
        write_rgb_thumbnail(
            source_path=str(float_path), output_path=thumbnail_path,
            value_range=value_range, gamma=gamma,
        )
        written["thumbnail_path"] = str(thumbnail_path)
    return written


def restyle_sentinel_s3_product(
    *,
    product_path: str | Path,
    output_dir: Path | None = None,
    value_range: tuple[float, float] | None = None,
    colormap: str | None = None,
    log_scale: bool | None = None,
    gamma: float | None = None,
    styled: bool = True,
    thumbnail: bool = True,
    processing_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-render the styled copy and thumbnail of a product already processed.

    The expensive half of making one of these layers is the warp; the colour
    range, the ramp and the linear-or-log choice cost nothing to change once the
    Float32 product exists. So a range that turns out wrong is a re-render, not
    a reprocess - which is what makes a colour decision reversible after
    publication, and what lets a review study put several candidate ranges in
    front of a reviewer without warping anything twice.

    Everything not given is read back from the product's own ``PYSENT_*`` tags,
    so a caller can change one thing and keep the rest. The float product is
    opened read-only and never rewritten.

    **A three-band picture product is restyled as a picture**: true colour goes
    to the RGB writers with ``gamma``, not through the colour ramp. ``colormap``
    and ``log_scale`` have no meaning for it and are refused rather than
    ignored, since a caller who passes them has misunderstood what they are
    restyling. Which kind a product is comes from what its bands say they are -
    three bands of reflectance, or a value band and a ``reject_reason``
    companion - because counting bands cannot tell the two apart.

    Returns the paths written and the style that was applied.
    """
    from osgeo import gdal

    float_path = Path(product_path)
    if not float_path.exists():
        raise ValueError(f"{float_path} does not exist")

    dataset = gdal.Open(str(float_path))
    if dataset is None:
        raise ValueError(f"{float_path} could not be opened")
    tags = dataset.GetMetadata()
    band_count = dataset.RasterCount
    band_descriptions = [
        dataset.GetRasterBand(index + 1).GetDescription() for index in range(band_count)
    ]
    dataset = None
    # Which kind of product this is, by what it says it is rather than by how
    # many bands it has: the tag first, then the band descriptions, because a
    # picture product and a value-plus-reason product can both have more than
    # one band. Only the grid path names its reason band, so the reason band
    # stays a count test - it is the picture that has to be recognised.
    is_rgb = S3_PRODUCTS.get(tags.get("PYSENT_PRODUCT", ""), {}).get("kind") == "rgb" or (
        band_count == 3 and all("reflectance" in name for name in band_descriptions)
    )

    stored_range = tags.get("PYSENT_STYLE_RANGE", "")
    if value_range is None:
        try:
            low, high = (float(part) for part in stored_range.split(","))
        except ValueError:
            raise ValueError(
                f"{float_path.name} carries no usable PYSENT_STYLE_RANGE tag "
                f"({stored_range!r}), so a value_range has to be given"
            ) from None
        value_range = (low, high)
    low, high = (float(value) for value in value_range)
    if not low < high:
        raise ValueError(f"value_range must be (low, high) with low < high, not {(low, high)}")

    if is_rgb:
        for name, value in (("colormap", colormap), ("log_scale", log_scale)):
            if value is not None:
                raise ValueError(
                    f"{float_path.name} is a three-band picture product, so {name!r} does not "
                    "apply to it; pass gamma and value_range instead"
                )
        # The pipeline's own gamma, so a restyle that changes only the range
        # reproduces the rendering rather than inventing a new one.
        if gamma is None:
            stored_gamma = tags.get("PYSENT_STYLE_GAMMA")
            if stored_gamma:
                gamma = float(stored_gamma)
            else:
                product = tags.get("PYSENT_PRODUCT", "")
                gamma = float(S3_PRODUCTS.get(product, {}).get("gamma", 1.0))
        if gamma <= 0:
            raise ValueError(f"gamma must be positive, not {gamma}")
        return _restyle_rgb(
            float_path=float_path,
            value_range=(low, high),
            gamma=float(gamma),
            tags=tags,
            output_dir=output_dir,
            styled=styled,
            thumbnail=thumbnail,
            processing_options=processing_options,
        )
    if gamma is not None:
        raise ValueError(
            f"{float_path.name} is a single-value product styled through a colour ramp, so gamma "
            "does not apply to it"
        )

    colormap = colormap or tags.get("PYSENT_COLORMAP") or "viridis"
    if colormap not in COLOUR_ANCHORS:
        raise ValueError(
            f"{colormap!r} is not a colormap pysent ships; it has: {', '.join(sorted(COLOUR_ANCHORS))}"
        )
    if log_scale is None:
        log_scale = tags.get("PYSENT_STYLE_SCALE", "linear") == "log10"
    if log_scale and low <= 0.0:
        raise ValueError(f"a log colour scale needs a positive lower bound, not {low}")

    settings = _resolve_processing_settings(processing_options)
    destination = Path(output_dir) if output_dir else float_path.parent
    destination.mkdir(parents=True, exist_ok=True)

    metadata = {
        **{key: value for key, value in tags.items() if key.startswith("PYSENT_")},
        "PYSENT_STYLE_RANGE": f"{low:g},{high:g}",
        "PYSENT_STYLE_SCALE": "log10" if log_scale else "linear",
        "PYSENT_COLORMAP": colormap,
    }
    # A value product's second band is its reject reasons, which is what lets
    # the browse image hide what the float keeps. A picture product never
    # reaches here - it was routed above - so this count test is safe again.
    reason_band = 2 if band_count >= 2 else None
    hide = BROWSE_HIDDEN_REASONS if reason_band else ()

    written: dict[str, Any] = {
        "source": str(float_path),
        "value_range": [low, high],
        "colormap": colormap,
        "scale": "log10" if log_scale else "linear",
        "browse_hidden": bool(hide),
    }
    if styled:
        styled_path = destination / f"{float_path.stem}_styled.tif"
        write_styled_copy(
            source_path=str(float_path), output_path=styled_path,
            value_range=(low, high), colormap=colormap, settings=settings,
            metadata=metadata, log_scale=log_scale,
            hide_codes=hide, reason_band=reason_band,
        )
        written["styled_path"] = str(styled_path)
    if thumbnail:
        thumbnail_path = destination / f"{float_path.stem}_thumb.png"
        write_thumbnail(
            source_path=str(float_path), output_path=thumbnail_path,
            value_range=(low, high), colormap=colormap, log_scale=log_scale,
            hide_codes=hide, reason_band=reason_band, materialise=True,
        )
        written["thumbnail_path"] = str(thumbnail_path)
    return written
