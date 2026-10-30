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

The masks below are **proposals read off the flag names in the files**, not
taken from the product notices, and the plan records that someone who knows
these products should review them before a layer is published.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from ._output import (
    COLOUR_ANCHORS,
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
    "build_sentinel_s3_output_filename",
    "default_sentinel_s3_products",
    "normalize_sentinel_s3_product_map",
    "process_sentinel_s3_safe",
]

# The grid the Synergy composites come on: 1/112 degree, about 1 km.
SYNERGY_RESOLUTION = 1.0 / 112.0
EPSG_LATLON = 4326

# Masks below are **proposals read off the flag names in the files**, not taken
# from the product notices, which were not available when this was written. The
# plan records them as open question Q12, and a reviewer who knows these
# products should confirm the defaults before a layer is published. Every one of
# them is a per-request option, so a reversal is a request change.
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
        "flag_member": "sm.nc",
        "flag_variable": "SM",
    },
}

# What a water product rejects. Eleven conditions, each of which makes a
# chlorophyll retrieval meaningless rather than merely uncertain.
_WFR_REJECT = (
    "INVALID", "LAND", "CLOUD", "CLOUD_AMBIGUOUS", "CLOUD_MARGIN", "SNOW_ICE",
    "SATURATED", "HIGHGLINT", "WHITECAPS", "AC_FAIL", "CLOUD_SHADOW",
)
_LFR_REJECT = (
    "INVALID", "CLOUD", "CLOUD_AMBIGUOUS", "CLOUD_MARGIN", "SNOW_ICE",
    "SATURATED", "HISOLZEN",
)

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
        "flag_member": "lqsf.nc",
        "flag_variable": "LQSF",
    },
})

# Geolocation member, shared by every OLCI swath product.
GEO_MEMBER = "geo_coordinates.nc"
# Above this many source pixels a decimated geolocation grid is faster. Measured
# on real frames at 0.003 deg: a 1.4 Mpx frame warps in 2.8 s at step 1 against
# 3.5 s at step 8, while a 19.9 Mpx frame takes 39.4 s at step 1 against 5.6 s
# at step 8. The crossover lies between, and 4 Mpx is also where the plan draws
# the line for an interactive request.
EXACT_WARP_PIXEL_LIMIT = 4_000_000
AUTO_GEOLOCATION_STEP = 8

# Which products each Sentinel-3 type yields.
S3_TYPE_PRODUCTS: dict[str, tuple[str, ...]] = {
    "SY_2_VG1": ("ndvi",),
    "SY_2_V10": ("ndvi",),
    "OL_2_WFR": ("chl", "tsm", "kd490"),
    "OL_2_WRR": ("chl", "tsm", "kd490"),
    "OL_2_LFR": ("gifapar", "otci"),
    "OL_2_LRR": ("gifapar", "otci"),
}

S3_DEFAULT_PRODUCTS = ("ndvi",)

S3_PRESETS: dict[str, dict[str, Any]] = {
    "full": {},
    # A 46-megapixel tile is more than a quicklook needs, and decimating on
    # read costs nothing because GDAL does it while decoding.
    "quicklook": {"decimate": 8, "overview_factors": [2, 4]},
}

# The Synergy presets decimate the raster; an OLCI swath is better served by a
# coarser output grid, since the warp cost follows the output.
S3_SWATH_PRESETS: dict[str, dict[str, Any]] = {
    "full": {},
    "quicklook": {"target_resolution": None, "overview_factors": [2, 4], "geoloc_step": 16},
}

# Why a pixel carries no value, for the optional second band. A point query can
# then answer "masked: cloud" instead of "no data", which is the difference
# between a map that looks broken and one that explains itself.
REJECT_REASONS: dict[str, int] = {
    "valid": 0,
    "fill": 1,
    "cloud": 2,
    "uncertain": 3,
    "unfilled": 4,
    "other": 5,
}


def default_sentinel_s3_products(input_dataset: str) -> tuple[str, ...]:
    """The products this product type can yield, from its name."""
    from .profiles import detect_nbs_sentinel_product

    product_type = detect_nbs_sentinel_product(str(input_dataset))["product_type"]
    return S3_TYPE_PRODUCTS.get(product_type, S3_DEFAULT_PRODUCTS)


# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #
_PRODUCT_OPTIONS = {"variable", "mask_flags", "reject_flags", "value_range", "colormap",
                    "decimate", "unit", "geoloc_step", "log_scale"}


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

        log_scale = bool(requested.get("log_scale", defaults.get("log_scale", False)))
        if log_scale and low <= 0.0:
            raise ValueError(
                f"product {key!r}: a log colour scale needs a positive lower bound, not {low}"
            )

        asked_for_flags = ("mask_flags" in requested) or ("reject_flags" in requested)
        normalized[key] = {
            **defaults,
            # A requested flag name is strict; a default one may be absent in an
            # older baseline and is then skipped and reported.
            "flags_are_default": not asked_for_flags,
            "kind": defaults.get("kind", "grid"),
            "variable": str(requested.get("variable") or defaults.get("variable") or ""),
            "candidates": defaults.get("candidates", ()),
            "mask_flags": mask_flags,
            "reject_flags": reject_flags,
            "value_range": (low, high),
            "colormap": colormap,
            "decimate": decimate,
            "geoloc_step": geoloc_step,
            "log_scale": log_scale,
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


# OLCI full-resolution ground sampling. The quicklook grid is a multiple of it.
OLCI_GROUND_SAMPLING_M = 300.0


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
            "reject_reason_band": 2 if settings["reject_reason"] else None,
            "reject_reasons": REJECT_REASONS if settings["reject_reason"] else None,
        },
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
        )
        result["styled_path"] = str(styled_path)

    if settings["thumbnail"]:
        thumbnail_path = float_path.with_name(f"{float_path.stem}_thumb.png")
        write_thumbnail(
            source_path=str(float_path), output_path=thumbnail_path,
            value_range=options["value_range"], colormap=options["colormap"],
            log_scale=options["log_scale"],
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
    geo_local = extract_member(root, GEO_MEMBER, scratch)
    lat_ds = open_sen3_variable(geo_local, "latitude")
    lon_ds = open_sen3_variable(geo_local, "longitude")
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
    masked = np.where(kept, values, np.nan).astype(np.float32)
    kept_count = int(np.count_nonzero(kept))
    source_valid = valid_fraction(values)

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
        ground_sampling_m=options.get("ground_sampling_m") or OLCI_GROUND_SAMPLING_M,
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
            minimum_valid=max(1, kept_count // 100),
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
        grid_epsg=str(int(epsg)),
        grid_resolution=f"{resolution:g}",
        decimate=str(decimate),
        geoloc_step=str(geoloc_step) if geoloc_step is not None else None,
        units=options["units"],
        style_range=f"{options['value_range'][0]:g},{options['value_range'][1]:g}",
        style_scale="log10" if options["log_scale"] else "linear",
        colormap=options["colormap"],
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
