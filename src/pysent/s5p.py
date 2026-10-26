"""Sentinel-5P Level-2 products as map rasters.

Sentinel-5P measures trace gases in the atmosphere. One file holds one orbit or
one five-minute slice of it, as a swath: a ``scanline × ground_pixel`` array
where every pixel carries its own latitude and longitude. This module masks it,
puts it on a map grid through :mod:`pysent._swath`, and writes three files per
product:

1. the **Float32 GeoTIFF** in the product's own physical units, which is what a
   coverage service serves and what a point query reads;
2. a **full-resolution 8-bit copy** with the colour range applied, for a map
   service that serves pixels rather than styling them;
3. a **256 px thumbnail** for a catalogue page.

Two things about these products shape everything here:

**Most of a Sentinel-5P archive is empty.** A granule over the winter pole is
100 % fill because there is no sunlight to measure by, and about a third of
near-real-time files are zero-byte stubs. An empty result is a normal outcome,
not a failure, so it raises :class:`pysent.errors.EmptySceneError` with the
valid fraction rather than writing a blank map.

**The quality threshold decides what a user sees.** On a European granule
``qa_min`` 0.75 keeps 25.6 % of the pixels and 0.5 keeps 80.7 %, because the
quality values are discrete and more than half the granule sits in a single
class. The default follows the mission's recommendation, and the threshold is a
per-product option so a caller can ask for the other picture. It is recorded in
the provenance, so a file always says which threshold made it.
"""
from __future__ import annotations

import datetime as _datetime
import math
from pathlib import Path
from typing import Any

import numpy as np

from ._runtime import (
    apply_preset,
    available_cpus,
    atomic_output,
    gdal_errors,
    gdal_runtime,
    provenance,
    resolve_gdal_runtime,
    resolve_work_dir,
    run_product_jobs,
    scratch_dir,
)
from ._output import (
    COLOUR_ANCHORS,
    colour_table,
    creation_options,
    finish_float_product,
    styled_bytes,
    write_styled_copy,
    write_thumbnail,
)
from ._swath import (
    DEFAULT_LATLON_RESOLUTION,
    DEFAULT_POLAR_RESOLUTION,
    EPSG_LATLON,
    RESAMPLING_METHODS,
    choose_grid,
    footprint_polygon,
    grid_bounds,
    open_root,
    read_array,
    read_raw_array,
    valid_fraction,
    value_statistics,
    warp_swath,
)
from .errors import EmptySceneError

__all__ = [
    "S5P_PRESETS",
    "S5P_PRODUCTS",
    "S5P_TYPE_PRODUCTS",
    "S5P_QUALITY_VARIABLE",
    "build_sentinel_s5p_output_filename",
    "default_sentinel_s5p_products",
    "normalize_sentinel_s5p_product_map",
    "process_sentinel_s5p_netcdf",
]

PRODUCT_GROUP = "/PRODUCT"
S5P_QUALITY_VARIABLE = f"{PRODUCT_GROUP}/qa_value"
_LATITUDE = f"{PRODUCT_GROUP}/latitude"
_LONGITUDE = f"{PRODUCT_GROUP}/longitude"

# One row per product this module can derive. ``qa_min`` follows the mission's
# recommendation per variable; ``value_range`` is the proposed colour range and
# is a *style* decision - it never changes a stored value. Phase 2 of the plan
# adds the remaining Level-2 types to this table.
S5P_PRODUCTS: dict[str, dict[str, Any]] = {
    "no2": {
        "variable": f"{PRODUCT_GROUP}/nitrogendioxide_tropospheric_column",
        "units": "mol m-2",
        "qa_min": 0.75,
        "value_range": (0.0, 1e-4),
        "colormap": "viridis",
        "label": "Tropospheric NO₂ column",
    },
    "co": {
        "variable": f"{PRODUCT_GROUP}/carbonmonoxide_total_column",
        "units": "mol m-2",
        "qa_min": 0.5,
        "value_range": (0.02, 0.04),
        "colormap": "viridis",
        "label": "CO total column",
    },
    "hcho": {
        "variable": f"{PRODUCT_GROUP}/formaldehyde_tropospheric_vertical_column",
        "units": "mol m-2",
        "qa_min": 0.5,
        "value_range": (0.0, 5e-4),
        "colormap": "viridis",
        "label": "Tropospheric HCHO column",
    },
    "so2": {
        "variable": f"{PRODUCT_GROUP}/sulfurdioxide_total_vertical_column",
        "units": "mol m-2",
        "qa_min": 0.5,
        "value_range": (0.0, 2.5e-3),
        "colormap": "viridis",
        "label": "SO₂ total column",
    },
    "o3": {
        "variable": f"{PRODUCT_GROUP}/ozone_total_vertical_column",
        "units": "mol m-2",
        "qa_min": 0.5,
        "value_range": (0.12, 0.15),
        "colormap": "viridis",
        "label": "Total ozone column",
    },
    "ch4": {
        "variable": f"{PRODUCT_GROUP}/methane_mixing_ratio_bias_corrected",
        "units": "ppb",
        "qa_min": 0.5,
        "value_range": (1850.0, 2000.0),
        "colormap": "viridis",
        "label": "Methane mixing ratio",
    },
    "aer_ai": {
        "variable": f"{PRODUCT_GROUP}/aerosol_index_354_388",
        "units": "1",
        "qa_min": 0.5,
        # Diverging about zero, which is the physically meaningful centre:
        # negative is clean or cloudy, positive is absorbing aerosol.
        "value_range": (-2.0, 2.0),
        "colormap": "rdbu",
        "label": "UV aerosol index",
    },
    "cloud": {
        "variable": f"{PRODUCT_GROUP}/cloud_fraction",
        "units": "1",
        "qa_min": 0.5,
        # A fraction: 0 to 1 by definition, not from the percentiles.
        "value_range": (0.0, 1.0),
        "colormap": "grey",
        "label": "Cloud fraction",
    },
    "aer_lh": {
        "variable": f"{PRODUCT_GROUP}/aerosol_mid_height",
        "units": "m",
        "qa_min": 0.5,
        "value_range": (0.0, 9000.0),
        "colormap": "viridis",
        "label": "Aerosol mid height",
    },
    "o3_pr_total": {
        "variable": f"{PRODUCT_GROUP}/ozone_total_column",
        "units": "mol m-2",
        "qa_min": 0.5,
        "value_range": (0.12, 0.15),
        "colormap": "viridis",
        "label": "Total ozone column (profile product)",
        # The profile product is on a 75x77 grid of about 29 km pixels, against
        # 3.5-7 km for every other Level-2 type. The family default of 0.05 deg
        # would oversample it roughly fivefold and claim a resolution it does
        # not have, so this type carries its own - as a distance on the ground,
        # because the granule may land on a geographic or a projected grid and
        # the same number cannot mean both.
        "ground_sampling_m": 28_000.0,
    },
    "o3_pr_tropospheric": {
        "variable": f"{PRODUCT_GROUP}/ozone_tropospheric_column",
        "units": "mol m-2",
        "qa_min": 0.5,
        "value_range": (0.0, 0.04),
        "colormap": "viridis",
        "label": "Tropospheric ozone column (profile product)",
        "ground_sampling_m": 28_000.0,
    },
}

# Which products each Sentinel-5P product type can yield. Lets a caller hand in
# a file and ask what it is for, rather than having to know.
S5P_TYPE_PRODUCTS: dict[str, tuple[str, ...]] = {
    "L2__NO2___": ("no2",),
    "L2__CO____": ("co",),
    "L2__HCHO__": ("hcho",),
    "L2__SO2___": ("so2",),
    "L2__O3____": ("o3",),
    "L2__CH4___": ("ch4",),
    "L2__AER_AI": ("aer_ai",),
    "L2__CLOUD_": ("cloud",),
    "L2__AER_LH": ("aer_lh",),
    "L2__O3__PR": ("o3_pr_total", "o3_pr_tropospheric"),
}

S5P_DEFAULT_PRODUCTS = ("no2",)


def default_sentinel_s5p_products(input_dataset: str) -> tuple[str, ...]:
    """The products this file is for, worked out from its own name.

    A Sentinel-5P file holds one trace gas, so "derive whatever this file is
    for" is the common request and a caller should not have to look it up.
    Falls back to NO2 when the name says nothing.
    """
    from .profiles import detect_nbs_sentinel_product

    product_type = detect_nbs_sentinel_product(str(input_dataset))["product_type"]
    return S5P_TYPE_PRODUCTS.get(product_type, S5P_DEFAULT_PRODUCTS)

# A quicklook trades resolution for speed, the same idea as the Sentinel-2 preset.
S5P_PRESETS: dict[str, dict[str, Any]] = {
    "full": {},
    "quicklook": {"target_resolution": DEFAULT_LATLON_RESOLUTION * 4, "overview_factors": [2, 4]},
}

# Physical units are what gets stored. These are offered so a caller can ask for
# the unit its users read, and the factor is recorded in the provenance.
_UNIT_FACTORS: dict[str, tuple[float, str]] = {
    "native": (1.0, ""),
    "umol_m2": (1e6, "umol m-2"),
    "molec_cm2": (6.02214076e23 / 1e4, "molec cm-2"),
    "du": (1.0 / 4.4615e-4, "DU"),
    "ppb": (1.0, "ppb"),
}



# --------------------------------------------------------------------------- #
# The request
# --------------------------------------------------------------------------- #
def normalize_sentinel_s5p_product_map(
    products: Any, *, default_products: tuple[str, ...] | None = None
) -> dict[str, dict[str, Any]]:
    """Validate a requested product map and fill in the defaults.

    ``products`` is ``{product_key: {field: value}}``; a value of ``None`` or an
    empty mapping asks for the defaults. Unknown keys and out-of-range values
    raise a :class:`ValueError` that names the field, so a bad request fails
    before anything is opened.
    """
    if products in (None, "", {}):
        products = {name: {} for name in (default_products or S5P_DEFAULT_PRODUCTS)}
    if not isinstance(products, dict):
        raise ValueError(f"products must be a mapping of product name to options, not {type(products).__name__}")

    normalized: dict[str, dict[str, Any]] = {}
    for name, requested in products.items():
        key = str(name).strip().lower()
        if key not in S5P_PRODUCTS:
            known = ", ".join(sorted(S5P_PRODUCTS))
            raise ValueError(f"{name!r} is not a Sentinel-5P product pysent derives; it knows: {known}")
        if requested in (None, ""):
            requested = {}
        if not isinstance(requested, dict):
            raise ValueError(f"options for product {key!r} must be a mapping, not {type(requested).__name__}")

        defaults = S5P_PRODUCTS[key]
        unknown = set(requested) - {"variable", "qa_min", "grid", "resampling", "unit", "value_range", "colormap"}
        if unknown:
            raise ValueError(f"product {key!r} has unknown option(s): {', '.join(sorted(unknown))}")

        variable = str(requested.get("variable") or defaults["variable"])
        if not variable.startswith("/"):
            variable = f"{PRODUCT_GROUP}/{variable}"

        qa_min = requested.get("qa_min", defaults["qa_min"])
        qa_min = _coerce_qa_min(qa_min, key)

        grid = requested.get("grid") or {}
        if not isinstance(grid, dict):
            raise ValueError(f"product {key!r}: grid must be a mapping with 'epsg' and/or 'res'")
        unknown_grid = set(grid) - {"epsg", "res"}
        if unknown_grid:
            raise ValueError(f"product {key!r}: grid has unknown key(s): {', '.join(sorted(unknown_grid))}")

        resampling = str(requested.get("resampling") or "average").strip().lower()
        if resampling not in RESAMPLING_METHODS:
            raise ValueError(
                f"product {key!r}: resampling must be one of {', '.join(RESAMPLING_METHODS)}, not {resampling!r}"
            )

        unit = str(requested.get("unit") or "native").strip().lower()
        if unit not in _UNIT_FACTORS:
            raise ValueError(f"product {key!r}: unit must be one of {', '.join(_UNIT_FACTORS)}, not {unit!r}")

        value_range = requested.get("value_range") or defaults["value_range"]
        value_range = _coerce_value_range(value_range, key)

        normalized[key] = {
            "variable": variable,
            "qa_min": qa_min,
            "grid": {"epsg": grid.get("epsg", "auto"), "res": grid.get("res")},
            "resampling": resampling,
            "unit": unit,
            "units": defaults["units"],
            "value_range": value_range,
            "colormap": str(requested.get("colormap") or defaults["colormap"]),
            "label": defaults["label"],
            "ground_sampling_m": defaults.get("ground_sampling_m"),
        }
    if not normalized:
        raise ValueError("no products were requested")
    return normalized


def _coerce_qa_min(value: Any, key: str) -> float:
    try:
        qa_min = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"product {key!r}: qa_min must be a number between 0 and 1, not {value!r}") from None
    if not 0.0 <= qa_min <= 1.0:
        raise ValueError(f"product {key!r}: qa_min must be between 0 and 1, not {qa_min}")
    # The stored quality value is a byte of whole percent, so the threshold has
    # a hundredth as its finest meaningful step.
    return round(qa_min, 2)


def _coerce_value_range(value: Any, key: str) -> tuple[float, float]:
    try:
        low, high = (float(item) for item in value)
    except (TypeError, ValueError):
        raise ValueError(f"product {key!r}: value_range must be two numbers, not {value!r}") from None
    if not math.isfinite(low) or not math.isfinite(high) or low >= high:
        raise ValueError(f"product {key!r}: value_range must be (low, high) with low < high, not {(low, high)}")
    return (low, high)


def build_sentinel_s5p_output_filename(
    input_dataset: str,
    product: str,
    *,
    qa_min: float | None = None,
    suffix: str = ".tif",
) -> str:
    """A default output name, carrying the quality threshold.

    The threshold belongs in the name because it changes the pixels: two
    requests that differ only in ``qa_min`` would otherwise overwrite each
    other, and a runner that skips an output it already has would hand back the
    wrong map.
    """
    stem = Path(str(input_dataset).split("?")[0]).name
    for extension in (".nc", ".nc4", ".h5"):
        if stem.lower().endswith(extension):
            stem = stem[: -len(extension)]
            break
    parts = [stem, product]
    if qa_min is not None:
        parts.append(f"qa{int(round(float(qa_min) * 100)):02d}")
    return "_".join(parts) + suffix


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def _resolve_processing_settings(processing_options: dict[str, Any] | None) -> dict[str, Any]:
    processing = apply_preset(processing_options, S5P_PRESETS)

    context = str(processing.get("context") or "bulk").strip().lower()
    if context not in ("interactive", "bulk"):
        raise ValueError(f"context must be 'interactive' or 'bulk', not {context!r}")

    on_empty = str(processing.get("on_empty") or "raise").strip().lower()
    if on_empty not in ("raise", "write"):
        raise ValueError(f"on_empty must be 'raise' or 'write', not {on_empty!r}")

    target_epsg = processing.get("target_epsg")
    target_epsg = str(target_epsg).strip() if target_epsg not in (None, "") else None
    target_resolution = processing.get("target_resolution")
    target_resolution = float(target_resolution) if target_resolution not in (None, "") else None

    max_output_pixels = processing.get("max_output_pixels")
    if max_output_pixels in (None, "", 0, "0"):
        # Peak memory is linear in output cells, measured at about 39 MB per
        # million on a whole orbit (6 M cells 844 MB, 24 M 1.5 GB, 49 M 2.3 GB,
        # 96 M 3.8 GB). The interactive queue allows 4 GB per task, so 100 M
        # cells would fill it; 40 M leaves room for everything else in the
        # worker. A bulk job gets more, but not the 400 M the plan guessed:
        # that would want 15 GB.
        max_output_pixels = 40_000_000 if context == "interactive" else 150_000_000
    max_output_pixels = int(max_output_pixels)

    overview_factors = processing.get("overview_factors") or [2, 4, 8]
    overview_factors = [int(factor) for factor in overview_factors if int(factor) > 1]

    # Products run one at a time, so each gets every CPU this process may use
    # (affinity and cgroup quota, not os.cpu_count()).
    requested_threads = processing.get("gdal_num_threads")
    if requested_threads in (None, ""):
        gdal_num_threads = str(available_cpus())
    else:
        text = str(requested_threads).strip().upper()
        gdal_num_threads = text if text == "ALL_CPUS" else str(max(1, int(text)))

    return {
        "context": context,
        "on_empty": on_empty,
        "target_epsg": target_epsg,
        "target_resolution": target_resolution,
        "max_output_pixels": max_output_pixels,
        "overview_factors": overview_factors,
        # Float32 geophysical data compresses far better with a horizontal
        # predictor than without; measured in the phase 1 session log.
        "compression": str(processing.get("compression") or "DEFLATE").upper(),
        "predictor": int(processing.get("predictor") or 3),
        "styled": bool(processing.get("styled", True)),
        "thumbnail": bool(processing.get("thumbnail", True)),
        "work_dir": resolve_work_dir(processing.get("work_dir")),
        "runtime": resolve_gdal_runtime(processing, requested_threads, gdal_num_threads),
        "gdal_num_threads": gdal_num_threads,
    }


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# One product
# --------------------------------------------------------------------------- #
def _granule_times(root) -> dict[str, str]:
    """The granule's start and end time, from whichever attribute carries them."""
    times: dict[str, str] = {}
    for attribute, key in (("time_coverage_start", "start"), ("time_coverage_end", "end")):
        try:
            value = root.GetAttribute(attribute)
        except (RuntimeError, AttributeError):
            value = None
        if value is not None:
            try:
                times[key] = str(value.ReadAsString())
            except (RuntimeError, AttributeError):
                pass
    return times


def _process_one_product(
    *,
    product: str,
    options: dict[str, Any],
    input_dataset: str,
    output_path: str,
    settings: dict[str, Any],
    scratch: Path,
    swath: dict[str, Any],
) -> dict[str, Any]:
    lat, lon = swath["lat"], swath["lon"]
    root = swath["root"]

    values = read_array(root, options["variable"])
    if values.shape != lat.shape:
        raise ValueError(
            f"{options['variable']} has shape {values.shape} but the coordinates are {lat.shape}; "
            "this variable is not on the swath grid"
        )

    # Compare the stored byte, never a float against the threshold: 0.75 is not
    # exactly representable and the stored values are whole percent.
    threshold = int(round(options["qa_min"] * 100))
    masked = np.where(swath["qa_raw"] > threshold, values, np.nan).astype(np.float32)

    source_valid = valid_fraction(masked)
    if source_valid == 0.0 and settings["on_empty"] == "raise":
        raise EmptySceneError(
            f"{product}: no pixel of this granule passes qa_min={options['qa_min']:.2f}, so there is nothing to map "
            f"(the granule is {100 * (1 - valid_fraction(values)):.1f} % fill before masking). "
            "A granule over the winter pole is normally empty."
        )

    factor, unit_label = _UNIT_FACTORS[options["unit"]]
    if factor != 1.0:
        masked = (masked * np.float32(factor)).astype(np.float32)

    # Precedence: an explicit request, then the preset, then this product's own
    # native sampling (only the ozone profile has one), then the family default.
    epsg, resolution = choose_grid(
        lat,
        lon,
        epsg=settings["target_epsg"] or options["grid"]["epsg"],
        resolution=options["grid"]["res"] or settings["target_resolution"],
        ground_sampling_m=options.get("ground_sampling_m"),
    )
    bounds = grid_bounds(lat, lon, epsg, resolution)
    cells = ((bounds[2] - bounds[0]) / resolution) * ((bounds[3] - bounds[1]) / resolution)
    if cells > settings["max_output_pixels"]:
        raise ValueError(
            f"{product}: a {epsg} grid at {resolution} would be {cells:,.0f} cells, over the "
            f"{settings['max_output_pixels']:,} limit for a {settings['context']} request; "
            "ask for a coarser resolution or run it as a bulk job"
        )

    float_path = Path(output_path)
    with atomic_output(float_path) as temporary:
        warp_swath(
            values=masked,
            lat=lat,
            lon=lon,
            output_path=str(temporary),
            epsg=epsg,
            resolution=resolution,
            bounds=bounds,
            resampling=options["resampling"],
            scratch=scratch,
            creation_options=creation_options(settings, float_data=True),
            num_threads=settings["gdal_num_threads"],
        )
        finish_float_product(
            path=str(temporary),
            settings=settings,
            metadata=_product_metadata(
                product=product,
                options=options,
                input_dataset=input_dataset,
                epsg=epsg,
                resolution=resolution,
                resampling=options["resampling"],
                unit_label=unit_label,
            ),
        )

    from osgeo import gdal

    written = gdal.Open(str(float_path))
    grid_values = written.GetRasterBand(1).ReadAsArray()
    transform = written.GetGeoTransform()
    width, height = written.RasterXSize, written.RasterYSize
    written = None

    statistics = value_statistics(grid_values)
    output_valid = valid_fraction(grid_values)

    results: dict[str, Any] = {
        "product": product,
        "path": str(float_path),
        "output_file": float_path.name,
        "epsg": int(epsg),
        "resolution": float(resolution),
        "bounds": [float(value) for value in bounds],
        "width": int(width),
        "height": int(height),
        "transform": [float(value) for value in transform],
        "valid_fraction": output_valid,
        "source_valid_fraction": source_valid,
        "value_stats": statistics,
        "units": unit_label or options["units"],
        "qa_min": options["qa_min"],
        "style_hint": {
            "value_range": list(options["value_range"]),
            "colormap": options["colormap"],
            "label": options["label"],
        },
        "query_source": {
            "path": input_dataset,
            "variable": options["variable"],
            "lat": _LATITUDE,
            "lon": _LONGITUDE,
            "qa": S5P_QUALITY_VARIABLE,
            "qa_min": options["qa_min"],
            "scale": 0.01,
        },
        "footprint": swath["footprint"],
        "time": swath["time"],
    }

    if settings["styled"]:
        styled_path = float_path.with_name(f"{float_path.stem}_styled.tif")
        write_styled_copy(
            source_path=str(float_path),
            output_path=styled_path,
            value_range=options["value_range"],
            colormap=options["colormap"],
            settings=settings,
            metadata=_product_metadata(
                product=product,
                options=options,
                input_dataset=input_dataset,
                epsg=epsg,
                resolution=resolution,
                resampling=options["resampling"],
                unit_label=unit_label,
            ),
        )
        results["styled_path"] = str(styled_path)

    if settings["thumbnail"]:
        thumbnail_path = float_path.with_name(f"{float_path.stem}_thumb.png")
        write_thumbnail(
            source_path=str(float_path),
            output_path=thumbnail_path,
            value_range=options["value_range"],
            colormap=options["colormap"],
        )
        results["thumbnail_path"] = str(thumbnail_path)

    results["provenance"] = _product_metadata(
        product=product,
        options=options,
        input_dataset=input_dataset,
        epsg=epsg,
        resolution=resolution,
        resampling=options["resampling"],
        unit_label=unit_label,
    )
    return results


def _product_metadata(
    *,
    product: str,
    options: dict[str, Any],
    input_dataset: str,
    epsg: int,
    resolution: float,
    resampling: str,
    unit_label: str,
) -> dict[str, str]:
    return provenance(
        product=product,
        source=Path(str(input_dataset).split("?")[0]).name,
        variable=options["variable"],
        qa_min=f"{options['qa_min']:.2f}",
        grid_epsg=str(int(epsg)),
        grid_resolution=f"{resolution:g}",
        resampling=resampling,
        units=unit_label or options["units"],
        style_range=f"{options['value_range'][0]:g},{options['value_range'][1]:g}",
        colormap=options["colormap"],
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def process_sentinel_s5p_netcdf(
    *,
    input_dataset: str,
    output_dir: Path,
    products: dict[str, dict[str, Any]] | None = None,
    output_names: dict[str, str] | None = None,
    processing_options: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Derive map rasters from a Sentinel-5P Level-2 file.

    ``input_dataset`` is a local path or anything GDAL can open, including a
    ``/vsicurl/`` URL - the arrays needed are a few tens of megabytes even for a
    whole orbit, so there is no need to download the file first.

    ``products`` selects what to derive and how; see
    :func:`normalize_sentinel_s5p_product_map` for the fields and
    :data:`S5P_PRODUCTS` for the defaults. ``output_names`` overrides the
    default file names, which carry the quality threshold.

    Options in ``processing_options``:

    ``context``
        ``bulk`` (default) or ``interactive``. Interactive caps the output at
        100 M cells so a request cannot tie up a queue; bulk allows 400 M.
    ``target_epsg`` / ``target_resolution``
        Override the grid for every product. By default the projection is
        chosen per granule: lat/lon at 0.05° in the mid-latitudes, and a polar
        stereographic grid at 5 km for a swath that crosses the antimeridian or
        reaches a pole, where lat/lon leaves holes.
    ``on_empty``
        ``raise`` (default) gives :class:`pysent.errors.EmptySceneError` when no
        pixel survives the quality mask; ``write`` produces an empty map.
    ``preset``
        ``full`` (default) or ``quicklook``, which renders four times coarser.
    ``styled`` / ``thumbnail``
        Write the 8-bit copy and the 256 px thumbnail (both on by default).
    ``work_dir``, ``gdal_num_threads``, ``gdal_cachemax_mb``, ``compression``,
    ``overview_factors``
        As for the other pipelines.

    Returns one result dict per product, in request order. Every requested
    product is attempted; if one of several fails,
    :class:`pysent.errors.PartialFailure` is raised once the rest have finished.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    requested = normalize_sentinel_s5p_product_map(
        products, default_products=default_sentinel_s5p_products(input_dataset)
    )
    settings = _resolve_processing_settings(processing_options)
    names = dict(output_names or {})
    for product, options in requested.items():
        names.setdefault(product, build_sentinel_s5p_output_filename(input_dataset, product, qa_min=options["qa_min"]))

    scratch_root = Path(settings["work_dir"]) if settings["work_dir"] else output_dir

    with scratch_dir(scratch_root) as scratch:
        with gdal_runtime(**settings["runtime"]), gdal_errors():
            root = open_root(input_dataset)
            lat = read_array(root, _LATITUDE)
            lon = read_array(root, _LONGITUDE)
            qa_raw = read_raw_array(root, S5P_QUALITY_VARIABLE)
            swath = {
                "root": root,
                "lat": lat,
                "lon": lon,
                "qa_raw": qa_raw,
                "footprint": footprint_polygon(lat, lon),
                "time": _granule_times(root),
            }

            jobs = [
                (
                    product,
                    {
                        "product": product,
                        "options": options,
                        "input_dataset": input_dataset,
                        "output_path": str(output_dir / names[product]),
                        "settings": settings,
                        "scratch": scratch,
                        "swath": swath,
                    },
                )
                for product, options in requested.items()
            ]
            # One product at a time: the warp already uses the cores, and a
            # single GDAL write at a time is what the other pipelines settled on.
            return run_product_jobs(
                _process_one_product,
                jobs,
                parallel_mode="serial",
                workers=1,
                runtime=settings["runtime"],
            )
