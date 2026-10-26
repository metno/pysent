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
    extract_member,
    flag_selection,
    open_sen3_variable,
    read_flag_table,
    read_packed,
    read_sen3_manifest,
    sen3_root,
)
from ._swath import value_statistics, valid_fraction
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

S3_PRODUCTS: dict[str, dict[str, Any]] = {
    "ndvi": {
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
        "flag_member": "sm.nc",
        "flag_variable": "SM",
    },
}

# Which products each Sentinel-3 type yields.
S3_TYPE_PRODUCTS: dict[str, tuple[str, ...]] = {
    "SY_2_VG1": ("ndvi",),
    "SY_2_V10": ("ndvi",),
}

S3_DEFAULT_PRODUCTS = ("ndvi",)

S3_PRESETS: dict[str, dict[str, Any]] = {
    "full": {},
    # A 46-megapixel tile is more than a quicklook needs, and decimating on
    # read costs nothing because GDAL does it while decoding.
    "quicklook": {"decimate": 8, "overview_factors": [2, 4]},
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
_PRODUCT_OPTIONS = {"variable", "mask_flags", "value_range", "colormap", "decimate", "unit"}


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

        decimate = requested.get("decimate")
        decimate = _coerce_decimate(decimate, key) if decimate not in (None, "") else None

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

        normalized[key] = {
            **defaults,
            "variable": str(requested.get("variable") or defaults["variable"]),
            "mask_flags": mask_flags,
            "value_range": (low, high),
            "colormap": colormap,
            "decimate": decimate,
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

    return {
        "context": context,
        "on_empty": on_empty,
        "decimate": decimate,
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
                selected = flag_selection(flags, table, [reason]) & ~kept
                codes[selected] = REJECT_REASONS[reason]
    # Fill wins: a pixel with no value at all is not "masked by cloud".
    codes[missing & ~kept] = REJECT_REASONS["fill"]
    return codes


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
        kept = kept & flag_selection(flags, table, list(options["mask_flags"]))

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
    results: dict[str, Any] = {
        "product": product,
        "path": str(float_path),
        "output_file": float_path.name,
        "epsg": EPSG_LATLON,
        "resolution": abs(transform[1]),
        "bounds": [float(value) for value in bounds],
        "width": int(width),
        "height": int(height),
        "transform": [float(value) for value in transform],
        "decimate": int(decimate),
        "valid_fraction": usable,
        "source_valid_fraction": valid_fraction(values),
        "value_stats": value_statistics(masked),
        "units": options["units"],
        "mask_flags": list(options["mask_flags"]),
        "available_flags": [row["name"] for row in table],
        "baseline": manifest.get("baseline", ""),
        "style_hint": {
            "value_range": list(options["value_range"]),
            "colormap": options["colormap"],
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
        "footprint": manifest.get("footprint"),
        "time": manifest.get("time", {}),
        "provenance": metadata,
    }

    if settings["styled"]:
        styled_path = float_path.with_name(f"{float_path.stem}_styled.tif")
        write_styled_copy(
            source_path=str(float_path),
            output_path=styled_path,
            value_range=options["value_range"],
            colormap=options["colormap"],
            settings=settings,
            metadata=metadata,
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

    return results


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
) -> dict[str, str]:
    return provenance(
        product=product,
        source=Path(str(input_dataset).split("?")[0]).name,
        product_type=manifest.get("productType", ""),
        baseline=manifest.get("baseline", ""),
        member=options["member"],
        variable=options["variable"],
        mask_flags=",".join(options["mask_flags"]) or "none",
        grid_epsg=str(EPSG_LATLON),
        grid_resolution=f"{resolution:g}",
        decimate=str(decimate),
        units=options["units"],
        style_range=f"{options['value_range'][0]:g},{options['value_range'][1]:g}",
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
