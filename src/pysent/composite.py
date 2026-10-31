"""Many granules, one map: composites on a caller-supplied grid.

Every other entry point in pysent takes one input and fans it out to several
products. A composite is the opposite shape - several inputs collapsing into
one product - so it is a new entry point rather than an option on an existing
one.

The arithmetic is deliberately plain. Each input is masked exactly as the
single-granule pipeline masks it, warped onto **one fixed grid**, and added
into a running sum and count; the product is the sum divided by the count.
Nothing is written per input, so memory is O(grid) rather than O(inputs):
measured on eight Sentinel-5P NO2 granules over a Nordic box, peak memory
moved from 128 MB to 136 MB *as the eighth was added*.

Four measured facts shape what this module does, all of them in
``PLANNING/TODO_PLANNING_composites.md``:

**A composite cannot derive its own grid.** The inputs are the thing being
combined, and half the granules a spatial catalogue query selects report a
degenerate -180..180 longitude box. So ``region`` is required - a bounding box,
an EPSG code and a resolution - and nothing is inferred from the inputs. Two
composites of the same region on the same grid can be differenced; two on
grids chosen per input cannot.

**A useless input is normal.** Of eight granules fetched for one day, two had
no pixel above the quality threshold and two more contributed no cell to the
region. An input that adds nothing is reported, not raised; only a composite
with nothing in it at all is an :class:`~pysent.errors.EmptySceneError`.

**A composite looks more authoritative than it is.** One day of near-real-time
NO2 fills about 80 % of a Nordic box, and a third of the cells it does fill
hold a single observation. The per-cell observation count is therefore written
as a **second band** of the product, not merely reported, because a user
holding the file cannot otherwise tell a mean of three passes from one pass.

**Weighting the mean by the quality value changes nothing.** At the mission's
recommended threshold 96 % of the surviving quality values are exactly 1.00
(mean 0.996, sd 0.020), so the quality-weighted and plain means differ by
1.39 % of the p1-p99 spread at the 99th percentile and by nothing at all
typically - for a second warp of every granule. ``mean`` is the default and
``weighted_mean`` is there for a caller who lowers the threshold, where the
weights do spread out.

Provenance is written as flat ``PYSENT_COMPOSITE_*`` tags, **one fact per
tag**: a JSON-LD serialisation bound to a PROV-O ontology and MET's own
vocabulary is planned, and a tag holding a single fact maps onto an ontology
term later, whereas a composed string would have to be parsed back apart.
"""
from __future__ import annotations

import datetime as _datetime
import hashlib
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np

from ._output import (
    creation_options,
    finish_float_product,
    write_styled_copy,
    write_thumbnail,
)
from ._runtime import (
    atomic_output,
    available_cpus,
    gdal_errors,
    gdal_runtime,
    provenance,
    resolve_gdal_runtime,
    resolve_work_dir,
    scratch_dir,
)
from ._swath import (
    RESAMPLING_METHODS,
    open_root,
    read_array,
    read_raw_array,
    valid_fraction,
    value_statistics,
    warp_swath,
)
from .errors import EmptySceneError

__all__ = [
    "COMPOSITE_METHODS",
    "COUNT_BAND",
    "MAX_COMPOSITE_INPUTS",
    "build_composite_output_filename",
    "composite_input_digest",
    "composite_key",
    "normalize_composite_region",
    "process_sentinel_composite",
    "region_transform",
]

COMPOSITE_METHODS = ("mean", "weighted_mean")

# The product's second band holds the number of observations behind each cell.
COUNT_BAND = 2

# The count band is uint16 while it is accumulated, which is the only reason
# there is a ceiling at all. A day over a region is 23 granules and a month is
# about 700, so this is far above any real request; it is here so that an
# absurd one fails with a sentence instead of silently wrapping to zero.
MAX_COMPOSITE_INPUTS = 65_535

_S5P_PRODUCT_GROUP = "/PRODUCT"
_S5P_LATITUDE = f"{_S5P_PRODUCT_GROUP}/latitude"
_S5P_LONGITUDE = f"{_S5P_PRODUCT_GROUP}/longitude"
_S5P_QUALITY = f"{_S5P_PRODUCT_GROUP}/qa_value"


# --------------------------------------------------------------------------- #
# The region
# --------------------------------------------------------------------------- #
def normalize_composite_region(region: Any) -> dict[str, Any]:
    """Validate the caller's region and work out the grid it implies.

    ``region`` is ``{"bbox": [west, south, east, north], "epsg": 4326,
    "res": 0.05}``, with ``res`` in the units of ``epsg`` - degrees for a
    geographic code, metres for a projected one. All three are required: a
    composite has no single input to take a grid from, and a default would make
    two composites of "the same" region quietly incomparable.

    The span must be a whole number of cells. Snapping it silently would shift
    one composite by up to half a cell against another built from the same
    request, which is exactly the comparison these products exist to support.
    """
    if region in (None, "", {}):
        raise ValueError(
            "a composite needs an explicit region: {'bbox': [west, south, east, north], "
            "'epsg': 4326, 'res': 0.05}. Unlike a single-granule call it cannot take the grid "
            "from its input, because the inputs are what is being combined"
        )
    if not isinstance(region, dict):
        raise ValueError(f"region must be a mapping, not {type(region).__name__}")

    unknown = set(region) - {"bbox", "epsg", "res"}
    if unknown:
        raise ValueError(f"region has unknown key(s): {', '.join(sorted(unknown))}")
    for field in ("bbox", "epsg", "res"):
        if region.get(field) in (None, ""):
            raise ValueError(f"region is missing {field!r}; a composite needs bbox, epsg and res")

    try:
        west, south, east, north = (float(value) for value in region["bbox"])
    except (TypeError, ValueError):
        raise ValueError(
            f"region bbox must be four numbers [west, south, east, north], not {region['bbox']!r}"
        ) from None
    if not all(math.isfinite(value) for value in (west, south, east, north)):
        raise ValueError(f"region bbox must be finite, not {region['bbox']!r}")

    try:
        epsg = int(region["epsg"])
    except (TypeError, ValueError):
        raise ValueError(f"region epsg must be an EPSG code, not {region['epsg']!r}") from None
    try:
        resolution = float(region["res"])
    except (TypeError, ValueError):
        raise ValueError(f"region res must be a number, not {region['res']!r}") from None
    if not math.isfinite(resolution) or resolution <= 0:
        raise ValueError(f"region res must be positive, not {resolution}")

    if west >= east:
        raise ValueError(
            f"region bbox needs west < east, not {west} and {east}. A box that wraps the "
            "antimeridian is refused rather than smeared: ask for a polar or UTM grid instead"
        )
    if south >= north:
        raise ValueError(f"region bbox needs south < north, not {south} and {north}")

    if epsg == 4326:
        if not (-180.0 <= west and east <= 180.0):
            raise ValueError(f"region bbox longitudes must be within -180..180, not {west}..{east}")
        if not (-90.0 <= south and north <= 90.0):
            raise ValueError(f"region bbox latitudes must be within -90..90, not {south}..{north}")
        if south <= -90.0 or north >= 90.0:
            raise ValueError(
                "a lat/lon region reaching a pole is refused: every cell on the pole row covers "
                "the same point and the mean there means nothing. Use EPSG:32661 or 32761"
            )

    width = _whole_cells(east - west, resolution, "east - west")
    height = _whole_cells(north - south, resolution, "north - south")
    return {
        "bbox": (west, south, east, north),
        "epsg": epsg,
        "res": resolution,
        "width": width,
        "height": height,
        "cells": width * height,
    }


def _whole_cells(span: float, resolution: float, label: str) -> int:
    cells = span / resolution
    rounded = int(round(cells))
    if rounded < 1 or abs(cells - rounded) > 1e-6 * max(1.0, abs(cells)):
        low, high = math.floor(cells), math.ceil(cells)
        raise ValueError(
            f"region {label} is {span:g}, which is {cells:.6f} cells of {resolution:g} - not a whole "
            f"number. Use a span of {low * resolution:g} or {high * resolution:g}, so that two "
            "composites of this region land on the same cells"
        )
    return rounded


def region_transform(region: dict[str, Any]) -> tuple[float, float, float, float, float, float]:
    """The GDAL geotransform of a normalized region, north-up."""
    west, _south, _east, north = region["bbox"]
    resolution = region["res"]
    return (west, resolution, 0.0, north, 0.0, -resolution)


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def composite_input_digest(input_datasets: Sequence[str]) -> str:
    """A digest of the input set, as ``sha256:<hex>``.

    Over the sorted **file names**, not the paths: the same granules read from a
    different mount point or over ``/vsicurl`` are the same input set, and a
    composite should be traceable to them however they were reached.
    """
    names = sorted(Path(str(item).split("?")[0]).name for item in input_datasets)
    digest = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def composite_key(
    input_datasets: Sequence[str],
    *,
    product: str,
    options: dict[str, Any],
    region: dict[str, Any],
    method: str,
) -> str:
    """A short idempotency key over the inputs and everything that shapes the pixels.

    A runner that skips an output it already has needs this to be computable
    **without opening anything**, so it is built from names and options only.
    Two requests that differ in any of them get different keys, and the same
    request in a different input order gets the same one.
    """
    parts = [
        composite_input_digest(input_datasets),
        f"product={product}",
        f"method={method}",
        "region=" + ",".join(f"{value:g}" for value in region["bbox"]),
        f"epsg={region['epsg']}",
        f"res={region['res']:g}",
        f"variable={options['variable']}",
        f"qa_min={options['qa_min']:.2f}",
        f"resampling={options['resampling']}",
        f"unit={options['unit']}",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def build_composite_output_filename(
    *,
    product: str,
    qa_min: float,
    key: str,
    prefix: str = "composite",
    suffix: str = ".tif",
) -> str:
    """A default output name carrying the product, the threshold and the key.

    The key stands in for the input list: a composite of 23 granules cannot name
    its inputs in a filename, and two composites that differ in one granule must
    not land on the same name.
    """
    return f"{prefix}_{product}_qa{int(round(float(qa_min) * 100)):02d}_{key}{suffix}"


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def _resolve_processing_settings(processing_options: dict[str, Any] | None) -> dict[str, Any]:
    """The same settings vocabulary as the single-granule pipelines, minus the grid.

    There is no ``target_epsg``, ``target_resolution`` or ``preset`` here: the
    region decides the grid, and an option that silently overrode it would undo
    the comparability the region exists to give.
    """
    processing = dict(processing_options or {})
    for ignored in ("target_epsg", "target_resolution", "preset"):
        if processing.get(ignored) not in (None, ""):
            raise ValueError(
                f"{ignored!r} does not apply to a composite: the grid comes from region "
                "(bbox, epsg, res), which the caller must pass"
            )

    context = str(processing.get("context") or "bulk").strip().lower()
    if context not in ("interactive", "bulk"):
        raise ValueError(f"context must be 'interactive' or 'bulk', not {context!r}")

    max_output_pixels = processing.get("max_output_pixels")
    if max_output_pixels in (None, "", 0, "0"):
        # Measured on the accumulation itself: 0.18 Mcell cost 136 MB and
        # 4.5 Mcell cost 386 MB, so the accumulators are about 60 MB per
        # million cells - heavier per cell than a single-granule warp, because
        # a sum, a count and the warped granule are all live at once. The
        # single-granule limits are kept so that a caller meets one set of
        # numbers, and Phase 4 of the plan sets the inputs x cells cap.
        max_output_pixels = 40_000_000 if context == "interactive" else 150_000_000
    max_output_pixels = int(max_output_pixels)

    overview_factors = processing.get("overview_factors") or [2, 4, 8]
    overview_factors = [int(factor) for factor in overview_factors if int(factor) > 1]

    requested_threads = processing.get("gdal_num_threads")
    if requested_threads in (None, ""):
        gdal_num_threads = str(available_cpus())
    else:
        text = str(requested_threads).strip().upper()
        gdal_num_threads = text if text == "ALL_CPUS" else str(max(1, int(text)))

    return {
        "context": context,
        "max_output_pixels": max_output_pixels,
        "overview_factors": overview_factors,
        "compression": str(processing.get("compression") or "DEFLATE").upper(),
        "predictor": int(processing.get("predictor") or 3),
        "styled": bool(processing.get("styled", True)),
        "thumbnail": bool(processing.get("thumbnail", True)),
        "work_dir": resolve_work_dir(processing.get("work_dir")),
        "runtime": resolve_gdal_runtime(processing, requested_threads, gdal_num_threads),
        "gdal_num_threads": gdal_num_threads,
    }


# --------------------------------------------------------------------------- #
# Reading one input
# --------------------------------------------------------------------------- #
class _SkipInput(Exception):
    """An input that cannot contribute. Carries the reason, which is reported."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


def _read_s5p_granule(
    input_dataset: str, options: dict[str, Any], *, want_weights: bool
) -> dict[str, Any]:
    """One Sentinel-5P granule, masked the way the single-granule pipeline masks it.

    The threshold is compared against the **stored byte** of whole percent, not
    a float: 0.75 is not exactly representable and a float comparison moves the
    mask by a whole quality class. This repeats two lines of
    :mod:`pysent.s5p` on purpose, and the test that a one-input composite
    equals the single-granule product is what keeps the two from drifting.
    """
    try:
        root = open_root(input_dataset)
        lat = read_array(root, _S5P_LATITUDE)
        lon = read_array(root, _S5P_LONGITUDE)
        values = read_array(root, options["variable"])
        qa_raw = read_raw_array(root, _S5P_QUALITY)
        # While the file is open: a second open per input only to read two
        # attributes would double the opens of a 23-granule job.
        times = _granule_times(root)
    except (RuntimeError, ValueError, AttributeError) as exc:
        # A third of near-real-time files in the archive are zero-byte stubs,
        # and a stub is a normal thing to be handed, not a failure of the job.
        raise _SkipInput("unreadable", str(exc)[:200]) from None
    finally:
        root = None

    if values.shape != lat.shape or qa_raw.shape != lat.shape:
        raise _SkipInput(
            "shape_mismatch",
            f"values {values.shape}, quality {qa_raw.shape}, coordinates {lat.shape}",
        )

    threshold = int(round(options["qa_min"] * 100))
    keep = (qa_raw > threshold) & np.isfinite(values)
    if not keep.any():
        raise _SkipInput("no_pixels_above_qa_min", f"qa_min={options['qa_min']:.2f}")

    masked = np.where(keep, values, np.nan).astype(np.float32)
    factor = options.get("unit_factor", 1.0)
    if factor != 1.0:
        masked = (masked * np.float32(factor)).astype(np.float32)
    weights = None
    if want_weights:
        # The stored quality value is already 0..100, so it doubles as the
        # weight once divided by a hundred.
        weights = np.where(keep, qa_raw.astype(np.float32) / 100.0, np.nan).astype(np.float32)

    return {"lat": lat, "lon": lon, "values": masked, "weights": weights, "time": times}


def _granule_times(root) -> dict[str, str]:
    """The granule's coverage start and end, from whichever attribute carries them."""
    times: dict[str, str] = {}
    for attribute, key in (("time_coverage_start", "start"), ("time_coverage_end", "end")):
        try:
            value = root.GetAttribute(attribute)
            if value is not None:
                times[key] = str(value.ReadAsString())
        except (RuntimeError, AttributeError):
            continue
    return times


def _reader_for(input_dataset: str):
    """Which reader a given input needs.

    Sentinel-5P only, which is Phase 1's scope. Sentinel-3 composites are
    Phase 3 of the plan: the flag masks give "valid" by a different route and
    the reader is a different module, so refusing here is honest rather than
    producing a map from a file this code has not been measured against.
    """
    from .profiles import detect_nbs_sentinel_product

    platform = detect_nbs_sentinel_product(str(input_dataset)).get("platform") or ""
    if platform in ("", "S5P"):
        return _read_s5p_granule
    raise ValueError(
        f"{Path(str(input_dataset)).name}: composites are implemented for Sentinel-5P; "
        f"{platform} composites are Phase 3 of PLANNING_composites.md"
    )


# --------------------------------------------------------------------------- #
# One product
# --------------------------------------------------------------------------- #
def _accumulate(
    *,
    product: str,
    options: dict[str, Any],
    input_datasets: list[str],
    region: dict[str, Any],
    method: str,
    settings: dict[str, Any],
    scratch: Path,
) -> dict[str, Any]:
    """Sum, count and - for a weighted mean - weight, over the region grid."""
    from osgeo import gdal

    shape = (region["height"], region["width"])
    weighted = method == "weighted_mean"
    # float64 for the accumulators. The values are float32 and the output is
    # float32, but a sum has to be exact in a way a value does not: adding a
    # few hundred numbers of 1e-4 in float32 loses digits that the division
    # then keeps.
    totals = np.zeros(shape, dtype=np.float64)
    weight_totals = np.zeros(shape, dtype=np.float64) if weighted else None
    counts = np.zeros(shape, dtype=np.uint16)

    contributed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    times: list[dict[str, str]] = []

    for index, input_dataset in enumerate(input_datasets):
        name = Path(str(input_dataset).split("?")[0]).name
        try:
            granule = _reader_for(input_dataset)(input_dataset, options, want_weights=weighted)
        except _SkipInput as skip:
            skipped.append({"input": name, "reason": skip.reason, "detail": skip.detail})
            continue
        times.append(granule["time"])

        value_path = scratch / f"input_{index:05d}.tif"
        weight_path = scratch / f"weight_{index:05d}.tif"
        try:
            grids = []
            for array, path in ((granule["values"], value_path), (granule["weights"], weight_path)):
                if array is None:
                    continue
                Path(path).unlink(missing_ok=True)
                warp_swath(
                    values=array,
                    lat=granule["lat"],
                    lon=granule["lon"],
                    output_path=str(path),
                    epsg=region["epsg"],
                    resolution=region["res"],
                    bounds=region["bbox"],
                    resampling=options["resampling"],
                    scratch=scratch,
                    creation_options=["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=3"],
                    num_threads=settings["gdal_num_threads"],
                )
                dataset = gdal.Open(str(path))
                grids.append(dataset.GetRasterBand(1).ReadAsArray())
                dataset = None
        except (RuntimeError, ValueError) as exc:
            skipped.append({"input": name, "reason": "warp_failed", "detail": str(exc)[:200]})
            continue
        finally:
            # One input at a time: the intermediates go as soon as they have
            # been added in, so the scratch directory holds one granule's worth
            # however many inputs the job has.
            for path in (value_path, weight_path):
                Path(path).unlink(missing_ok=True)

        value_grid = grids[0]
        if value_grid.shape != shape:
            # Not an input problem: the region's span is a whole number of
            # cells, so a warp that lands on a different grid means the request
            # and the accumulator disagree, and adding them would broadcast
            # into nonsense.
            raise ValueError(
                f"{name}: the warp produced a {value_grid.shape} grid but the region is {shape}"
            )
        present = np.isfinite(value_grid)
        if weighted:
            weight_grid = grids[1]
            present &= np.isfinite(weight_grid)
        cells = int(np.count_nonzero(present))
        if cells == 0:
            # Normal: 12 of 23 granules a spatial query returns for a Nordic day
            # report a degenerate -180..180 box, and 4 of 8 real inputs added
            # nothing. Recorded so that "it ran" and "it has data in it" stay
            # different questions.
            skipped.append({"input": name, "reason": "no_overlap", "detail": "no cell in the region"})
            continue

        if weighted:
            totals[present] += value_grid[present] * weight_grid[present]
            weight_totals[present] += weight_grid[present]
        else:
            totals[present] += value_grid[present]
        counts[present] += 1
        contributed.append({"input": name, "cells": cells})

    if weighted:
        denominator = weight_totals
        composite = np.where(denominator > 0, totals / np.maximum(denominator, 1e-12), np.nan)
    else:
        composite = np.where(counts > 0, totals / np.maximum(counts, 1), np.nan)

    filled = counts > 0
    filled_cells = int(np.count_nonzero(filled))
    if filled_cells == 0:
        reasons = ", ".join(sorted({entry["reason"] for entry in skipped})) or "none given"
        raise EmptySceneError(
            f"{product}: none of the {len(input_datasets)} inputs contributed a cell to the region, "
            f"so there is nothing to map (reasons: {reasons}). A granule whose bounding box the "
            "catalogue could not wrap, or one with nothing above the quality threshold, is a normal "
            "thing to be handed"
        )

    return {
        "values": composite.astype(np.float32),
        "counts": counts,
        "contributed": contributed,
        "skipped": skipped,
        "window": _time_window(times),
        "coverage": filled_cells / counts.size,
        "observations": {
            "mean": float(counts[filled].mean()),
            "max": int(counts.max()),
            "single_share": float(np.mean(counts[filled] == 1)),
        },
    }


def _write_composite(
    *,
    output_path: Path,
    values: np.ndarray,
    counts: np.ndarray,
    region: dict[str, Any],
    settings: dict[str, Any],
    metadata: dict[str, str],
    label: str,
) -> None:
    """The two-band Float32 product: the composite, then the observation count.

    One file rather than two, because the count is not a separate product: it
    is how a reader tells a mean of three passes from a single pass, and the
    two must not be separable by accident. Both bands are Float32 because a
    GeoTIFF's bands share a type; a count is exact in Float32 far beyond
    anything a composite can reach.

    The count band carries no NoData: zero observations is a value, and the
    cells where it is zero are exactly the cells where band 1 is NaN.
    """
    from osgeo import gdal
    from osgeo import osr

    reference = osr.SpatialReference()
    reference.ImportFromEPSG(int(region["epsg"]))

    dataset = gdal.GetDriverByName("GTiff").Create(
        str(output_path),
        int(region["width"]),
        int(region["height"]),
        2,
        gdal.GDT_Float32,
        options=creation_options(settings, float_data=True),
    )
    dataset.SetGeoTransform(region_transform(region))
    dataset.SetProjection(reference.ExportToWkt())

    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(float("nan"))
    band.SetDescription(label)
    band.WriteArray(np.asarray(values, dtype=np.float32))

    count_band = dataset.GetRasterBand(COUNT_BAND)
    count_band.SetDescription("observations")
    count_band.WriteArray(counts.astype(np.float32))

    dataset.SetMetadata(metadata)
    dataset.FlushCache()
    dataset = None


def _composite_metadata(
    *,
    product: str,
    options: dict[str, Any],
    region: dict[str, Any],
    method: str,
    key: str,
    digest: str,
    input_count: int,
    contributing: int,
    window: dict[str, str],
    coverage: float,
    observations: dict[str, Any],
    unit_label: str,
) -> dict[str, str]:
    """Provenance as flat tags, one fact per tag.

    The owner's note on this: a JSON-LD string bound to an ad-hoc PROV-O
    ontology and MET's own vocabulary is the intended format, but it is not
    designed yet. So this records the facts and composes nothing - the bounding
    box is four tags rather than one string, times are ISO 8601 UTC, and
    nothing is abbreviated. A tag that holds one fact maps onto an ontology
    term; a packed string would have to be taken apart again first.
    """
    west, south, east, north = region["bbox"]
    tags: dict[str, Any] = {
        "product": product,
        "variable": options["variable"],
        "qa_min": f"{options['qa_min']:.2f}",
        "grid_epsg": str(int(region["epsg"])),
        "grid_resolution": f"{region['res']:g}",
        "resampling": options["resampling"],
        "units": unit_label or options["units"],
        "style_range": f"{options['value_range'][0]:g},{options['value_range'][1]:g}",
        "colormap": options["colormap"],
        "composite_method": method,
        "composite_key": key,
        "composite_input_digest": digest,
        "composite_input_count": str(int(input_count)),
        "composite_inputs_contributing": str(int(contributing)),
        "composite_region_west": f"{west:g}",
        "composite_region_south": f"{south:g}",
        "composite_region_east": f"{east:g}",
        "composite_region_north": f"{north:g}",
        "composite_coverage": f"{coverage:.4f}",
        "composite_observations_mean": f"{observations['mean']:.3f}",
        "composite_observations_max": str(int(observations["max"])),
        "composite_count_band": str(COUNT_BAND),
    }
    if window.get("start"):
        tags["composite_time_start"] = window["start"]
    if window.get("end"):
        tags["composite_time_end"] = window["end"]
    return provenance(**tags)


def _process_one_product(
    *,
    product: str,
    options: dict[str, Any],
    input_datasets: list[str],
    region: dict[str, Any],
    method: str,
    output_path: str,
    settings: dict[str, Any],
    scratch: Path,
) -> dict[str, Any]:
    accumulated = _accumulate(
        product=product,
        options=options,
        input_datasets=input_datasets,
        region=region,
        method=method,
        settings=settings,
        scratch=scratch,
    )

    window = accumulated["window"]
    key = composite_key(
        input_datasets, product=product, options=options, region=region, method=method
    )
    digest = composite_input_digest(input_datasets)
    metadata = _composite_metadata(
        product=product,
        options=options,
        region=region,
        method=method,
        key=key,
        digest=digest,
        input_count=len(input_datasets),
        contributing=len(accumulated["contributed"]),
        window=window,
        coverage=accumulated["coverage"],
        observations=accumulated["observations"],
        unit_label=options.get("unit_label", ""),
    )

    float_path = Path(output_path)
    with atomic_output(float_path) as temporary:
        _write_composite(
            output_path=temporary,
            values=accumulated["values"],
            counts=accumulated["counts"],
            region=region,
            settings=settings,
            metadata=metadata,
            label=options["label"],
        )
        finish_float_product(path=str(temporary), settings=settings, metadata=metadata)

    west, south, east, north = region["bbox"]
    results: dict[str, Any] = {
        "product": product,
        "path": str(float_path),
        "output_file": float_path.name,
        "method": method,
        "epsg": int(region["epsg"]),
        "resolution": float(region["res"]),
        "bounds": [west, south, east, north],
        "width": int(region["width"]),
        "height": int(region["height"]),
        "transform": list(region_transform(region)),
        "count_band": COUNT_BAND,
        "valid_fraction": valid_fraction(accumulated["values"]),
        "coverage": accumulated["coverage"],
        "observations_per_cell": accumulated["observations"],
        "value_stats": value_statistics(accumulated["values"]),
        "units": options.get("unit_label") or options["units"],
        "qa_min": options["qa_min"],
        "composite_key": key,
        "input_digest": digest,
        # The full list lives here rather than in the file's tags: 23 names is a
        # 1.6 kB tag that grows with the window, and the digest above ties the
        # file to this list.
        "inputs": [Path(str(item).split("?")[0]).name for item in input_datasets],
        "inputs_used": accumulated["contributed"],
        "inputs_skipped": accumulated["skipped"],
        "time": window,
        "style_hint": {
            "value_range": list(options["value_range"]),
            "colormap": options["colormap"],
            "label": options["label"],
        },
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
        # ``materialise`` reads band 1 out and warps that, which here is not an
        # optimisation but a correctness requirement: left to itself the warp
        # would resample both bands and a thumbnail of a count band is not a
        # picture of anything.
        write_thumbnail(
            source_path=str(float_path),
            output_path=thumbnail_path,
            value_range=options["value_range"],
            colormap=options["colormap"],
            materialise=True,
        )
        results["thumbnail_path"] = str(thumbnail_path)

    return results


def _time_window(granule_times: Sequence[dict[str, str]]) -> dict[str, str]:
    """The window the inputs actually cover, as ISO 8601 UTC.

    Taken from the granules rather than from the request: the caller selects
    inputs by a catalogue query, and what the file should record is the span of
    the data in it, not the span that was asked for. An input that contributed
    nothing still counts towards the window, because it was still part of what
    was looked at.
    """
    starts: list[str] = []
    ends: list[str] = []
    for times in granule_times:
        if times.get("start"):
            starts.append(times["start"])
        if times.get("end"):
            ends.append(times["end"])
    window: dict[str, str] = {}
    if starts:
        window["start"] = _iso_utc(min(starts, key=_sort_key))
    if ends:
        window["end"] = _iso_utc(max(ends, key=_sort_key))
    return window


def _sort_key(text: str) -> tuple[int, Any]:
    parsed = _parse_time(text)
    # A parsed time sorts before an unparseable one, so a single odd attribute
    # cannot win a min() or max() and silently become the window.
    return (0, parsed) if parsed is not None else (1, text)


def _parse_time(text: str) -> _datetime.datetime | None:
    cleaned = str(text).strip().replace("Z", "+00:00")
    try:
        parsed = _datetime.datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_datetime.timezone.utc)


def _iso_utc(text: str) -> str:
    parsed = _parse_time(text)
    if parsed is None:
        return str(text)
    return parsed.astimezone(_datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def process_sentinel_composite(
    *,
    input_datasets: Sequence[str],
    output_dir: Path,
    region: dict[str, Any],
    products: dict[str, dict[str, Any]] | None = None,
    method: str = "mean",
    output_names: dict[str, str] | None = None,
    processing_options: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Combine several granules into one map per product.

    ``input_datasets`` is the set to combine - local paths or anything GDAL can
    open. The caller chooses it, normally from a catalogue query by time; an
    input that turns out to contribute nothing is reported rather than fatal.

    ``region`` is required: ``{"bbox": [west, south, east, north],
    "epsg": 4326, "res": 0.05}``. See :func:`normalize_composite_region`.

    ``products`` selects what to derive and how, in the same shape as
    :func:`pysent.s5p.normalize_sentinel_s5p_product_map` - a composite of
    ``no2`` is the mean of exactly the masked, gridded ``no2`` that a
    single-granule call produces, so the product table is the same one.

    ``method`` is ``mean`` (default) or ``weighted_mean``, which weights each
    pixel by its quality value. The default is the plain mean because at the
    mission's recommended threshold the weights are all but identical - 96 % of
    them are exactly 1.00 - so the weighted mean costs a second warp of every
    granule and returns the same number.

    Options in ``processing_options``: ``context``, ``max_output_pixels``,
    ``styled``, ``thumbnail``, ``work_dir``, ``gdal_num_threads``,
    ``gdal_cachemax_mb``, ``compression``, ``overview_factors`` - as for the
    other pipelines. There is no ``target_epsg``, ``target_resolution`` or
    ``preset``: the region carries the grid.

    Each result dict carries the usual product fields plus ``inputs``,
    ``inputs_used``, ``inputs_skipped`` with a reason each, ``coverage``,
    ``observations_per_cell``, ``composite_key`` and ``input_digest``. The
    product file has two bands: the composite, and the per-cell observation
    count.

    Raises :class:`pysent.errors.EmptySceneError` if no input contributes a
    single cell.
    """
    from .s5p import normalize_sentinel_s5p_product_map

    inputs = [str(item) for item in (input_datasets or [])]
    if not inputs:
        raise ValueError("a composite needs at least one input dataset")
    if len(inputs) > MAX_COMPOSITE_INPUTS:
        raise ValueError(
            f"{len(inputs):,} inputs is more than this accumulates ({MAX_COMPOSITE_INPUTS:,}); "
            "split the window"
        )

    method = str(method or "mean").strip().lower()
    if method not in COMPOSITE_METHODS:
        raise ValueError(f"method must be one of {', '.join(COMPOSITE_METHODS)}, not {method!r}")

    resolved_region = normalize_composite_region(region)
    settings = _resolve_processing_settings(processing_options)
    if resolved_region["cells"] > settings["max_output_pixels"]:
        raise ValueError(
            f"a {resolved_region['epsg']} grid at {resolved_region['res']:g} over this region would "
            f"be {resolved_region['cells']:,} cells, over the {settings['max_output_pixels']:,} limit "
            f"for a {settings['context']} request; ask for a coarser resolution or a smaller region"
        )

    from .s5p import default_sentinel_s5p_products

    requested = normalize_sentinel_s5p_product_map(
        products, default_products=default_sentinel_s5p_products(inputs[0])
    )
    from .s5p import _UNIT_FACTORS

    for options in requested.values():
        # The grid is the region's, so a per-product grid request cannot be
        # honoured and must not be ignored either.
        if options["grid"].get("res") or options["grid"].get("epsg") not in (None, "", "auto"):
            raise ValueError(
                "a per-product grid does not apply to a composite: every product lands on the "
                "region's grid, so that the bands of one request can be compared"
            )
        # The unit conversion is applied to each granule before it is added in,
        # which is the same factor the single-granule pipeline applies and is
        # recorded in the provenance the same way.
        factor, unit_label = _UNIT_FACTORS[options["unit"]]
        options["unit_factor"] = factor
        options["unit_label"] = unit_label

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    names = dict(output_names or {})
    for product, options in requested.items():
        names.setdefault(
            product,
            build_composite_output_filename(
                product=product,
                qa_min=options["qa_min"],
                key=composite_key(
                    inputs, product=product, options=options, region=resolved_region, method=method
                ),
            ),
        )

    scratch_root = Path(settings["work_dir"]) if settings["work_dir"] else output_dir
    results: list[dict[str, Any]] = []
    with scratch_dir(scratch_root) as scratch:
        with gdal_runtime(**settings["runtime"]), gdal_errors():
            # One product at a time, like the single-granule pipelines: the warp
            # already uses the cores and one GDAL write at a time is what the
            # rest of the library settled on. Unlike them, products are not run
            # through ``run_product_jobs``: each one re-reads every input, so a
            # partial failure here is a different bargain and is left for
            # Phase 4 along with the cost cap.
            for product, options in requested.items():
                results.append(
                    _process_one_product(
                        product=product,
                        options=options,
                        input_datasets=inputs,
                        region=resolved_region,
                        method=method,
                        output_path=str(output_dir / names[product]),
                        settings=settings,
                        scratch=scratch,
                    )
                )
    return results
