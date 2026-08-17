"""Generate the Sentinel GeoTIFF benchmark notebook (04_benchmarks.ipynb).

Run:  ``python docs/notebooks/_build_benchmark_notebook.py``
A single notebook (``sentinel_gtiff_quality.ipynb``) is emitted next to this
script; it **auto-detects** S1 vs S2 from the product and runs the matching
pipeline. Edit the cell text below and re-run to regenerate (keeps the .ipynb
JSON out of hand-editing). The old per-platform notebooks are removed on run.
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent
BUNDLE_NAME = OUT.name  # "test_nbs_pipeline"


def md(*lines):
    return {"cell_type": "markdown", "metadata": {}, "source": _src(lines)}


def code(*lines, tags=None):
    metadata = {"tags": list(tags)} if tags else {}
    return {"cell_type": "code", "metadata": metadata, "execution_count": None,
            "outputs": [], "source": _src(lines)}


def _src(lines):
    if len(lines) == 1 and "\n" in lines[0]:
        text = lines[0]
    else:
        text = "\n".join(lines)
    text = text.strip("\n")
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + [parts[-1]]


def notebook(cells):
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3 (sentinel-qa)", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.x"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


# --------------------------------------------------------------------------- #
PARAMETERS = '''
# --- papermill parameters -------------------------------------------------
# Defaults run against the committed test fixtures, so this notebook executes
# anywhere (that is how CI keeps it from rotting). Point these at real data to
# benchmark the full pipeline including the warp, which is where the cost is.
DATA_ROOT = ""      # mounted NBS archive, e.g. "/data/nbsArchive"
SAFE_PATH = ""      # explicit .SAFE / .zip product
IDENTIFIER = ""     # catalogue UUID, resolved via pysent.archive
ENDPOINT = None     # None -> NBS_SENTINEL_CSW_ENDPOINT / https://nbs.csw.met.no
PLATFORM = ""       # "" -> detect from the product; force with "S1" / "S2"
OUTPUT_DIR = "_qa_output"
RUN_PROFILE_COMPARISON = True   # section 10 re-warps per profile (~4x runtime)
'''.strip()


SETUP = '''
import os
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling

import pysent
import pysent.qa as squ
import nbtools

print("pysent", pysent.__version__, "from", Path(pysent.__file__).parent)

OUTPUT_DIR = Path(OUTPUT_DIR)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
BUNDLE_DIR = Path.cwd()
'''.strip()


INPUTS_COMMON = '''
# Archive/marker are only consulted when resolving a catalogue UUID; nbtools
# reads NBS_ARCHIVE_ROOT itself. Kept visible so the mapping is explicit.
ARCHIVE_ROOT = DATA_ROOT or os.environ.get("NBS_ARCHIVE_ROOT") or "/data/nbsArchive"
print("archive root:", ARCHIVE_ROOT)
print("outputs     :", OUTPUT_DIR.resolve())
'''.strip()


RESOLVE = '''
product = nbtools.resolve_input(
    PLATFORM or "S1",
    safe_path=SAFE_PATH or None,
    identifier=IDENTIFIER or None,
    data_root=DATA_ROOT or None,
    endpoint=ENDPOINT,
)
safe_input = str(product.path)
print(nbtools.describe(product))
'''.strip()


# ---- Platform detection ---- #
DETECT = '''
# In SAFE mode the platform is sniffed from the product name/URL; in fixture
# mode it is already known from the manifest. Override PLATFORM to force it.
if PLATFORM:
    platform = PLATFORM
elif product.can_warp:
    platform = squ.detect_platform(safe_input, IDENTIFIER)
else:
    platform = product.platform
PLATFORM = platform
FIXTURE_MODE = not product.can_warp
print("platform:", PLATFORM, "| fixture mode:", FIXTURE_MODE)
'''.strip()


# ---- Combined processing parameters ---- #
PARAMS = '''
# Tunable processing parameters. Only the detected platform's block is applied;
# defaults are the pysent.s1 / pysent.s2 production constants.
from pysent.s1 import (
    S1_TARGET_EPSG, S1_TARGET_RESOLUTION, S1_OVERVIEW_FACTORS, S1_STRETCH_PERCENTILES,
)
from pysent.s2 import (
    S2_DEFAULT_PRODUCTS, S2_OVERVIEW_FACTORS, S2_STRETCH_PERCENTILES,
)

BLOCK_SIZE = 256

if PLATFORM == "S1":
    # --- Sentinel-1 (SAR amplitude) ---
    # Polarisations actually present in THIS product, auto-detected from the SAFE:
    # IW SDV -> VV+VH, EW SDH -> HH+HV, single-pol -> one band. Override by hand
    # if needed, e.g. VARIABLES = ["Amplitude_HH"].
    # In fixture mode there is no SAFE manifest to read, so fall back to the
    # mode token in the product name recorded by the fixture generator.
    VARIABLES = ([] if FIXTURE_MODE else squ.list_s1_polarizations(safe_input))
    ITEMS = list(VARIABLES)                             # one product per polarisation
    STRETCH_PERCENTILES = S1_STRETCH_PERCENTILES        # (2.0, 98.0)   <-- tune me
    TARGET_EPSG = S1_TARGET_EPSG                        # "EPSG:32661"
    TARGET_RESOLUTION = S1_TARGET_RESOLUTION            # 40.0 m        <-- tune me
    RESAMPLE_ALG = "average"                            # warp resampling  <-- tune me
    OVERVIEW_FACTORS = S1_OVERVIEW_FACTORS
    # --- experiments (A/B against the production defaults) ---
    S1_DB_SCALE = False     # True -> stretch in dB (20*log10) - recommended for SAR  <-- try
    S1_USE_TPS = True       # False -> faster polynomial-GCP warp (benchmark speed vs accuracy)  <-- try
    print("S1 | variables:", VARIABLES or "(fixture)", "| stretch:", STRETCH_PERCENTILES,
          "| dB:", S1_DB_SCALE, "| tps:", S1_USE_TPS)

    # --- comparison profiles (used by the "Profile comparison" section) ---
    # Each profile is a full override of the knobs the process functions read.
    _BASE = dict(STRETCH_PERCENTILES=STRETCH_PERCENTILES, TARGET_RESOLUTION=TARGET_RESOLUTION,
                 RESAMPLE_ALG=RESAMPLE_ALG, S1_DB_SCALE=S1_DB_SCALE, S1_USE_TPS=S1_USE_TPS)
    _COARSE = (TARGET_RESOLUTION or 40.0) * 2   # coarsen for the "fastest" profile
    PROFILES = {
        "raw":             {**_BASE, "S1_DB_SCALE": False, "S1_USE_TPS": True,  "RESAMPLE_ALG": "average"},
        "best-quality":    {**_BASE, "S1_DB_SCALE": True,  "S1_USE_TPS": True,  "RESAMPLE_ALG": "cubic"},
        "fastest-results": {**_BASE, "S1_DB_SCALE": False, "S1_USE_TPS": False, "RESAMPLE_ALG": "average",
                            "TARGET_RESOLUTION": _COARSE},
        # Fast warp (polynomial GCP + coarse res) but the BEST stretch (dB): the
        # warp dominates cost and the stretch is cheap, so this aims for near-fastest
        # speed with near-best quality.
        "fast-warp+best-stretch": {**_BASE, "S1_DB_SCALE": True, "S1_USE_TPS": False, "RESAMPLE_ALG": "average",
                                   "TARGET_RESOLUTION": _COARSE},
    }
else:
    # --- Sentinel-2 (optical RGB composites) ---
    PRODUCTS = dict(S2_DEFAULT_PRODUCTS)               # edit to test other band combos
    ITEMS = list(PRODUCTS.items())                     # one product per band combination
    STRETCH_PERCENTILES = S2_STRETCH_PERCENTILES       # (2.0, 98.0)   <-- tune me
    TARGET_EPSG = None                                 # None -> native UTM of the bands
    TARGET_RESOLUTION = None                           # None -> finest band res (10 m)  <-- tune me
    RESAMPLE_ALG = "bilinear"                          # warp resampling  <-- tune me
    OVERVIEW_FACTORS = S2_OVERVIEW_FACTORS
    # --- experiment: "percentile" (robust) vs "minmax" (what production does) ---
    S2_STRETCH_METHOD = "percentile"   # "minmax" mimics the active GDAL auto stretch  <-- try
    print("S2 | method:", S2_STRETCH_METHOD, "| stretch:", STRETCH_PERCENTILES)
    for name, bands in PRODUCTS.items():
        print(f"S2 | {name:24s} {bands}")

    # --- comparison profiles (used by the "Profile comparison" section) ---
    _BASE = dict(STRETCH_PERCENTILES=STRETCH_PERCENTILES, TARGET_RESOLUTION=TARGET_RESOLUTION,
                 RESAMPLE_ALG=RESAMPLE_ALG, S2_STRETCH_METHOD=S2_STRETCH_METHOD)
    _COARSE = (TARGET_RESOLUTION or 10.0) * 4   # coarsen for the "fastest" profile (warp dominates)
    PROFILES = {
        "raw":             {**_BASE, "S2_STRETCH_METHOD": "minmax",     "RESAMPLE_ALG": "bilinear"},
        "best-quality":    {**_BASE, "S2_STRETCH_METHOD": "percentile", "RESAMPLE_ALG": "cubic"},
        "fastest-results": {**_BASE, "S2_STRETCH_METHOD": "minmax",     "RESAMPLE_ALG": "average",
                            "TARGET_RESOLUTION": _COARSE},
        # Fast warp (coarse res + cheap resample) but the BEST stretch (percentile):
        # warp dominates cost, stretch is cheap -> near-fastest speed, better quality.
        "fast-warp+best-stretch": {**_BASE, "S2_STRETCH_METHOD": "percentile", "RESAMPLE_ALG": "average",
                                   "TARGET_RESOLUTION": _COARSE},
    }
'''.strip()


# ---- S1 pipeline (defined always, called only when PLATFORM == "S1") ---- #
S1_PROCESS = '''
from pysent.s1 import _warp_sentinel_s1_safe_amplitude, stretch_sentinel_s1_grayscale


def process_s1_amplitude(safe_input, variable, out_dir, *, bench):
    """Warp one amplitude polarisation to a raw Float32 GeoTIFF, then apply the
    exact production percentile stretch to an 8-bit gray+alpha quicklook.

    Steps are timed separately so each shows up as its own benchmark row.
    Returns the raw + final paths and the stretch statistics.
    """
    stem = variable.lower()
    raw_path = out_dir / f"{stem}_raw.tif"          # raw warped amplitude (Float32)
    final_path = out_dir / f"{stem}_stretched.tif"  # histogram-stretched quicklook (uint8)

    # 1) Warp SAFE band -> raw amplitude GeoTIFF (this IS the raw product).
    #    use_tps=False switches to the faster polynomial-GCP warp (benchmark it).
    with bench("warp_raw", product=variable, out_path=str(raw_path)):
        _warp_sentinel_s1_safe_amplitude(
            safe_input, variable, raw_path,
            target_epsg=TARGET_EPSG, target_resolution=TARGET_RESOLUTION,
            resample_alg=RESAMPLE_ALG, block_size=BLOCK_SIZE, use_tps=S1_USE_TPS,
        )

    # 2) Read the warped raster into memory.
    with bench("read", product=variable):
        with rasterio.open(raw_path) as src:
            data = src.read(1).astype(np.float32, copy=False)
            profile = src.profile.copy()
            nodata, width, height = src.nodata, src.width, src.height

    # 3) Histogram stretch - production linear routine, or the dB variant (S1_DB_SCALE).
    with bench("stretch", product=variable) as row:
        if S1_DB_SCALE:
            gray, alpha, stats = squ.stretch_s1_grayscale_db(
                data, nodata=nodata, percentiles=STRETCH_PERCENTILES,
            )
        else:
            gray, alpha, stats = stretch_sentinel_s1_grayscale(
                data, nodata=nodata, percentiles=STRETCH_PERCENTILES,
            )
        row["p_low"], row["p_high"] = round(stats["p_low"], 2), round(stats["p_high"], 2)
        row["unit"] = stats.get("unit", "linear")

    # 4) Write the final 8-bit quicklook + overviews (lossless DEFLATE for QA;
    #    production default is lossy JPEG - flip ``compress`` to compare).
    with bench("write_stretched", product=variable, out_path=str(final_path)):
        profile.update(
            driver="GTiff", dtype="uint8", count=2, nodata=None,
            compress="deflate", tiled=True, blockxsize=BLOCK_SIZE, blockysize=BLOCK_SIZE,
            interleave="pixel", photometric="minisblack",
        )
        with rasterio.open(final_path, "w", **profile) as dst:
            dst.write(gray, 1)
            dst.write(alpha, 2)
            dst.colorinterp = (ColorInterp.gray, ColorInterp.alpha)
            factors = [f for f in OVERVIEW_FACTORS if width // f >= 1 and height // f >= 1]
            if factors:
                dst.build_overviews(factors, Resampling.average)
                dst.update_tags(ns="rio_overview", resampling="average")

    return {"variable": variable, "raw_path": raw_path, "final_path": final_path, "stretch": stats}
'''.strip()


# ---- S2 pipeline (defined always, called only when PLATFORM == "S2") ---- #
S2_PROCESS = '''
from pysent.s2 import _warp_sentinel_s2_rgb, stretch_sentinel_s2_rgb


def process_s2_product(safe_input, product_name, bands, out_dir, *, bench):
    """Warp a 3-band combination to a raw UInt16 RGB GeoTIFF (the raw false-colour
    composite), then apply the production percentile stretch to an 8-bit RGB.

    Each step is timed individually for the benchmark table.
    """
    raw_path = out_dir / f"{product_name}_raw.tif"          # raw warped RGB (UInt16)
    final_path = out_dir / f"{product_name}_stretched.tif"  # stretched RGB (uint8)

    # 1) Build the 3-band VRT and warp -> raw composite GeoTIFF.
    with bench("warp_raw", product=product_name, out_path=str(raw_path)) as row:
        eff_epsg, eff_res = _warp_sentinel_s2_rgb(
            safe_input, bands, raw_path,
            target_epsg=TARGET_EPSG, target_resolution=TARGET_RESOLUTION,
            resample_alg=RESAMPLE_ALG, block_size=BLOCK_SIZE,
            gdal_num_threads="1", warp_memory_limit_mb=None,
        )
        row["epsg"], row["res"] = eff_epsg, eff_res

    # 2) Read the warped 3-band raster.
    with bench("read", product=product_name):
        with rasterio.open(raw_path) as src:
            data = src.read([1, 2, 3]).astype(np.float32, copy=False)
            profile = src.profile.copy()
            nodata, width, height = src.nodata, src.width, src.height

    # 3) Stretch: percentile (robust) or min/max (what production currently does).
    with bench("stretch", product=product_name) as row:
        if S2_STRETCH_METHOD == "minmax":
            stretched, stats = squ.stretch_rgb_minmax(data, nodata=nodata)
        else:
            stretched, stats = stretch_sentinel_s2_rgb(
                data, nodata=nodata, percentiles=STRETCH_PERCENTILES,
            )
        row["method"] = S2_STRETCH_METHOD
        row["p_low"] = [round(v, 1) for v in stats["p_low"]]
        row["p_high"] = [round(v, 1) for v in stats["p_high"]]

    # 4) Write the final 8-bit RGB + overviews (lossless DEFLATE for QA).
    with bench("write_stretched", product=product_name, out_path=str(final_path)):
        profile.update(
            driver="GTiff", dtype="uint8", count=3, nodata=None,
            compress="deflate", tiled=True, blockxsize=BLOCK_SIZE, blockysize=BLOCK_SIZE,
            interleave="pixel", photometric="rgb",
        )
        with rasterio.open(final_path, "w", **profile) as dst:
            dst.write(stretched)
            dst.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
            factors = [f for f in OVERVIEW_FACTORS if width // f >= 1 and height // f >= 1]
            if factors:
                dst.build_overviews(factors, Resampling.average)
                dst.update_tags(ns="rio_overview", resampling="average")

    return {"product": product_name, "bands": bands,
            "raw_path": raw_path, "final_path": final_path, "stretch": stats}
'''.strip()


FIXTURE_PROCESS = '''
def process_fixture(path, label, output_dir, *, bench):
    """read -> stretch -> write on a windowed extract (no SAFE, so no warp).

    Deliberately the *same* stretch and writer as the SAFE pipelines above, so
    the numbers are comparable step-for-step; only `warp_raw` is missing. This
    is what lets the notebook execute in CI without gigabytes of Sentinel data.
    """
    raw_path = Path(path)
    final_path = output_dir / f"{label}_fixture_stretched.tif"

    with bench("read", product=label):
        with rasterio.open(raw_path) as src:
            count = min(src.count, 3)
            indexes = [1, 2, 3][:count] if count == 3 else 1
            data = src.read(indexes).astype(np.float32, copy=False)
            profile = src.profile.copy()
            nodata, width = src.nodata, src.width

    mode = "rgb" if count == 3 else "gray"
    with bench("stretch", product=label):
        if mode == "rgb":
            stretched, stats = stretch_sentinel_s2_rgb(
                data, nodata=nodata, percentiles=STRETCH_PERCENTILES)
        else:
            gray, alpha, stats = stretch_sentinel_s1_grayscale(
                data, nodata=nodata, percentiles=STRETCH_PERCENTILES)

    with bench("write_stretched", product=label, out_path=str(final_path)):
        bands = 3 if mode == "rgb" else 2
        profile.update(driver="GTiff", dtype="uint8", count=bands, nodata=None,
                       compress="deflate", tiled=True, blockxsize=BLOCK_SIZE,
                       blockysize=BLOCK_SIZE, interleave="pixel",
                       photometric="rgb" if mode == "rgb" else "minisblack")
        with rasterio.open(final_path, "w", **profile) as dst:
            if mode == "rgb":
                dst.write(stretched)
                dst.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
            else:
                dst.write(gray, 1); dst.write(alpha, 2)
                dst.colorinterp = (ColorInterp.gray, ColorInterp.alpha)
            dst.build_overviews([f for f in OVERVIEW_FACTORS if width // f >= 1],
                                Resampling.average)

    return {"label": label, "mode": mode, "raw_path": raw_path,
            "final_path": final_path, "stretch": stats}
'''.strip()


# ---- Combined dispatch cells (platform-adaptive) ---- #
RUN = '''
# Dispatch to the pipeline for the detected platform; every step times into one
# bench. Results carry a uniform "label"/"mode" so the cells below work alike.
bench = squ.Bench()
results = []

if FIXTURE_MODE:
    print("fixture mode: benchmarking read -> stretch -> write (no warp)")
    r = process_fixture(safe_input, product.label.split("_")[0], OUTPUT_DIR, bench=bench)
    results.append(r)
elif PLATFORM == "S1":
    for variable in ITEMS:
        print(f"processing {variable} ...")
        r = process_s1_amplitude(safe_input, variable, OUTPUT_DIR, bench=bench)
        results.append({**r, "label": r["variable"], "mode": "gray"})
else:
    for product_name, bands in ITEMS:
        print(f"processing {product_name} {bands} ...")
        r = process_s2_product(safe_input, product_name, bands, OUTPUT_DIR, bench=bench)
        results.append({**r, "label": r["product"], "mode": "rgb"})

print("done:", [r["final_path"].name for r in results])
'''.strip()

STATS = '''
import pandas as pd

rows = []
for r in results:
    raw = squ.raster_stats(r["raw_path"], band=1)
    fin = squ.raster_stats(r["final_path"], band=1)
    row = {
        "product": r["label"],
        "size": f"{raw['width']}x{raw['height']}",
        "raw_dtype": raw["dtype"],
        "final_overviews": fin.get("overviews"),
    }
    if r["mode"] == "gray":                       # S1: scalar stretch stats
        row.update(
            raw_valid_frac=raw.get("valid_frac"), raw_p2=raw.get("p2"), raw_p98=raw.get("p98"),
            stretch_p_low=round(r["stretch"]["p_low"], 2),
            stretch_p_high=round(r["stretch"]["p_high"], 2),
        )
    else:                                         # S2: per-band stretch stats
        row.update(
            bands="/".join(r["bands"]), res_m=raw.get("res"),
            stretch_p_low=[round(v, 1) for v in r["stretch"]["p_low"]],
            stretch_p_high=[round(v, 1) for v in r["stretch"]["p_high"]],
        )
    rows.append(row)
pd.DataFrame(rows)
'''.strip()

VIZ = '''
# Left = raw product at full min/max (NO clipping) so the stretch's effect shows;
# right = the stretched output (title shows % pixels clipped). For S1 the
# histograms also mark p_low/p_high and report the clipped tails.
for r in results:
    title = f"{r['label']} ({'/'.join(r['bands'])})" if r["mode"] == "rgb" else r["label"]
    squ.show_before_after(r["raw_path"], r["final_path"], title=title,
                          mode=r["mode"], raw_display="minmax")
    if r["mode"] == "gray":
        squ.plot_histograms(r["raw_path"], r["final_path"], band=1, mode="gray",
                            p_low=r["stretch"]["p_low"], p_high=r["stretch"]["p_high"])
'''.strip()

CLIP = '''
import pandas as pd

# Stretch-quality report. Read two columns together as you tune STRETCH_PERCENTILES:
#   clip_total_%  (over-stretch)  - pixels forced to pure black/white; want it low.
#   range_used_%  (under-stretch) - how much of 0-255 the bulk of pixels span; want it high.
# Sweet spot = high range_used_% with low clip_total_%. Re-run sections 4-5 and compare.
clip_rows = []
for r in results:
    labels = list(r["bands"]) if r["mode"] == "rgb" else None
    for row in squ.stretch_clip_report(r["raw_path"], r["final_path"], mode=r["mode"], band_labels=labels):
        clip_rows.append({"product": r["label"], **row})
pd.DataFrame(clip_rows)
'''.strip()

PROFILE_COMPARE = '''
# The profile sweep re-warps every product once per profile, so it needs a real
# SAFE and roughly 4x the runtime of a single pass.
if FIXTURE_MODE or not RUN_PROFILE_COMPARISON:
    reason = ("needs a real SAFE (fixture mode has no warp)" if FIXTURE_MODE
              else "RUN_PROFILE_COMPARISON is False")
    print(f"skipped: profile comparison {reason}")
    profile_rows = []
else:
    import numpy as np
    import pandas as pd

    # Run every product through each profile and collect time + quality side by side.
    # Profiles override the knobs the process functions read (set via globals()), so
    # the SAME production warp/stretch code runs - only the parameters change.
    # NOTE: this re-warps each product once per profile, so it takes ~Nx a single run.


    def _profile_metrics(bench, product):
        rows = [row for row in bench.rows if row.get("product") == product]
        warp = next((row for row in rows if row["step"] == "warp_raw"), {})
        write = next((row for row in rows if row["step"] == "write_stretched"), {})
        return {
            "total_s": round(sum(row["seconds"] for row in rows), 2),
            "warp_s": warp.get("seconds"),
            "rss_peak_mb": max((row.get("rss_peak_mb", 0) for row in rows), default=None),
            "out_size_mb": write.get("out_size_mb"),
        }


    compare_rows = []
    for profile_name, overrides in PROFILES.items():
        globals().update(overrides)                 # apply this profile's knobs
        pbench = squ.Bench()
        pdir = OUTPUT_DIR / "profiles" / profile_name
        pdir.mkdir(parents=True, exist_ok=True)
        print(f"=== {profile_name} ===  " + ", ".join(f"{k}={v}" for k, v in overrides.items()))
        for item in ITEMS:
            if PLATFORM == "S1":
                r = process_s1_amplitude(safe_input, item, pdir, bench=pbench)
                label, bands, mode = r["variable"], None, "gray"
            else:
                product_name, bands = item
                r = process_s2_product(safe_input, product_name, bands, pdir, bench=pbench)
                label, mode = r["product"], "rgb"
            clip = squ.stretch_clip_report(r["raw_path"], r["final_path"], mode=mode,
                                           band_labels=list(bands) if bands else None)
            compare_rows.append({
                "profile": profile_name, "product": label,
                **_profile_metrics(pbench, label),
                "clip_total_%": round(float(np.mean([c["clip_total_%"] for c in clip])), 2),
                "range_used_%": round(float(np.mean([c["range_used_%"] for c in clip])), 1),
                "final_path": str(r["final_path"]),
            })
    globals().update(_BASE)                          # restore the single-run knobs

    compare = pd.DataFrame(compare_rows)
    compare[[c for c in compare.columns if c != "final_path"]]
'''.strip()

PROFILE_SUMMARY = '''
if not profile_rows:
    print("skipped: section 10 produced no profile results")
else:
    # Per-profile averages across products: SPEED (total_s, warp_s, rss) vs QUALITY
    # (range_used_% up = good, clip_total_% down = good). This is the trade-off table.
    summary = (compare.groupby("profile")[
            ["total_s", "warp_s", "rss_peak_mb", "out_size_mb", "clip_total_%", "range_used_%"]]
        .mean().round(2).reindex(list(PROFILES)))
    summary
'''.strip()

PROFILE_VIZ = '''
if not profile_rows:
    print("skipped: section 10 produced no profile results")
else:
    # Final stretched output of each profile, side by side, per product.
    mode = "gray" if PLATFORM == "S1" else "rgb"
    for label in dict.fromkeys(compare["product"]):
        paths = {row.profile: row.final_path for row in compare.itertuples() if row.product == label}
        squ.show_profiles(paths, title=label, mode=mode)
'''.strip()

BENCH_CELL = '''
df = bench.to_dataframe()
display(df)
print("\\nWall-clock per product (seconds):")
display(bench.summary())
'''.strip()

BENCH_NOTE = (
    "## 6. Benchmark metrics\n",
    "Per step: **time** and **memory**, plus output size.\n",
    "- **`seconds`** and **`out_size_mb`** — the headline cost numbers.\n"
    "- **`rss_peak_mb`** / **`rss_delta_mb`** — peak *process* resident memory during the step, "
    "and the rise over it, sampled on a background thread. This includes **GDAL's C-side buffers**, "
    "so it's the real memory cost of `warp_raw`.\n"
    "- **`py_peak_mb`** — peak *Python-side* allocation (`tracemalloc`). Accurate for `read`/`stretch` "
    "(numpy), but blind to GDAL's C library, so it under-reports `warp_raw` — compare it against "
    "`rss_peak_mb` to see how much work happens outside Python.",
)


# --------------------------------------------------------------------------- #
def build_unified():
    cells = [
        md(
            "# Sentinel GeoTIFF quality evaluation (S1 + S2)\n",
            "Load a **Sentinel-1 or Sentinel-2** product from its SAFE archive (resolved from a CSW "
            "**UUID**, or pointed at a local file). The notebook **auto-detects the platform** and runs "
            "the matching pipeline:\n",
            "- **Sentinel-1** -> for each amplitude polarisation present (**VV+VH**, **HH+HV**, or "
            "single-pol - auto-detected from the product): a raw Float32 amplitude GeoTIFF + an 8-bit "
            "gray quicklook.\n"
            "- **Sentinel-2** -> for three band combinations (true- + false-colour): a raw UInt16 RGB "
            "GeoTIFF + an 8-bit stretched RGB.\n",
            "Both use the *same* warp + percentile-stretch routines as the ingestion pipeline - imported "
            "from the [`pysent`](https://github.com/metno/pysent) library, which is what the service "
            "runs too - and every step is **benchmarked** so you can tune parameters and weigh cost vs. "
            "quality.\n",
            "> New here? See [`README.md`](README.md) for clean-machine setup, then set `IDENTIFIER` "
            "(or `SAFE_PATH_OVERRIDE`) in the Inputs cell and Run All.",
        ),
        code(PARAMETERS, tags=["parameters"]),
        md("## 0. Setup"),
        code(SETUP),
        md("## 1. Inputs"),
        code(INPUTS_COMMON),
        md("## 2. Resolve the UUID to a local SAFE archive\n",
           "`pysent.archive`, the same resolution the service uses: CSW `getRecordByID` -> download URL -> "
           "local `nbsArchive` path. Skipped when `SAFE_PATH_OVERRIDE` is set."),
        code(RESOLVE),
        md("## 3. Detect the platform\n",
           "Sniffs **S1 vs S2** from the product name / URL (same logic the ingestion code uses). "
           "Everything below adapts automatically; override `PLATFORM` in the cell if detection is wrong."),
        code(DETECT),
        md("## 4. Processing parameters\n",
           "Only the detected platform's block is applied. The `<-- tune me` values - above all "
           "`STRETCH_PERCENTILES` - are what you iterate on; for S2 edit `PRODUCTS` to test other band combos."),
        code(PARAMS),
        md("## 5. Pipeline (warp -> read -> stretch -> write), per step, benchmarked\n",
           "Both platform pipelines are defined below (each a thin wrapper around the `pysent` production "
           "warp + stretch); the run cell calls the one for the detected platform. Every step is timed "
           "into a shared benchmark."),
        code(S1_PROCESS),
        code(S2_PROCESS),
        code(FIXTURE_PROCESS),
        code(RUN),
        md(*BENCH_NOTE),
        code(BENCH_CELL),
        md("## 7. Output statistics (raw vs final)"),
        code(STATS),
        md("## 8. Visual quality check (raw vs stretched)\n",
           "**Left:** raw product at full **min/max** - no clipping, the un-stretched reference. "
           "**Right:** the stretched output on disk (its title shows the % of pixels clipped).\n",
           "Because the left panel is *not* pre-stretched, narrowing `STRETCH_PERCENTILES` visibly "
           "brightens/contrasts the right panel relative to it - that delta is the benefit you tune for. "
           "For S1 the histograms also mark `p_low`/`p_high` and report the clipped tails."),
        code(VIZ),
        md("## 9. Stretch quality report (over- and under-stretch)\n",
           "Per band, read **two columns together**:\n"
           "- **`clip_total_%`** (over-stretch): valid pixels forced to pure black/white. A few % each side is "
           "healthy; double digits = crushed shadows / blown highlights, i.e. lost detail. For S2, watch one "
           "channel blowing out before the others (SWIR over snow/cloud, blue over water).\n"
           "- **`range_used_%`** (under-stretch): how much of 0-255 the central 96% of pixels (`out_p2`..`out_p98`) "
           "span. **Low** (e.g. < ~60%) = wasted dynamic range, a flat / dull image.\n"
           "The sweet spot is **high `range_used_%` with low `clip_total_%`**. Re-run sections 4-5 with a "
           "different `STRETCH_PERCENTILES` and compare this table."),
        code(CLIP),
        md("## 10. Profile comparison: raw / best-quality / fastest-results / fast-warp+best-stretch\n",
           "Run every product through **four profiles** and compare the speed/quality trade-off in one "
           "table. Same production warp + stretch code each time - only the parameters change:\n"
           "- **`raw`** - the current production-equivalent baseline (S1 linear + tps warp; S2 GDAL "
           "min/max + bilinear).\n"
           "- **`best-quality`** - S1 **dB** stretch + tps + cubic; S2 **percentile** + cubic; finest resolution.\n"
           "- **`fastest-results`** - the faster warp (S1 **polynomial GCP**, both coarser resolution + cheaper "
           "resampling) **and** the cheap stretch - the dominant cost is the warp, so this is where the time is won.\n"
           "- **`fast-warp+best-stretch`** - the **fast warp** (as `fastest-results`) but the **best stretch** "
           "(S1 dB / S2 percentile). Because the warp dominates and the stretch is nearly free, this should land "
           "close to `fastest-results` on time while recovering most of `best-quality`'s `range_used_%` - usually "
           "the best speed/quality compromise.\n"
           "Edit `PROFILES` in the parameters cell to change what each profile does. **This re-warps each "
           "product once per profile, so expect ~4x a single run.**"),
        code(PROFILE_COMPARE),
        md("### 10b. Speed vs quality summary\n",
           "Per-profile averages across products. Read **left-to-right as the cost** (`total_s`, `warp_s`, "
           "`rss_peak_mb`, `out_size_mb`) **vs the benefit** (`range_used_%` up = better contrast, "
           "`clip_total_%` down = less blown/crushed). `fastest-results` should cut `warp_s` sharply; "
           "`best-quality` should lift `range_used_%` (especially S1 dB)."),
        code(PROFILE_SUMMARY),
        md("### 10c. Visual comparison\n",
           "The final stretched image from each profile, side by side, per product."),
        code(PROFILE_VIZ),
        md("## 11. Tuning notes\n",
           "**Goal: high `range_used_%` (use the range) with low `clip_total_%` (rule of thumb < ~2-3% per side).**\n"
           "**Experiment toggles** (in the *Processing parameters* cell; re-run sections 4-9 and compare the "
           "stretch-quality table + visuals):\n"
           "- **`S2_STRETCH_METHOD`** = `\"percentile\"` vs `\"minmax\"` — `minmax` reproduces what the active "
           "ingestion code does (GDAL auto min/max); percentile is outlier-robust. Expect `minmax` to show higher "
           "`clip_*`/blown highlights on cloudy scenes.\n"
           "- **`S1_DB_SCALE`** = `True` converts amplitude to **dB** (`20*log10`) before the clip — usually a large "
           "`range_used_%` gain for SAR vs the linear default.\n"
           "- **`S1_USE_TPS`** = `False` switches the warp from thin-plate-spline to the faster **polynomial GCP** "
           "transform — compare the `warp_raw` `seconds`/`rss_peak_mb` for the speed win, and the imagery for any "
           "geolocation drift.\n"
           "- **`STRETCH_PERCENTILES`** - the main lever, trading the two off. **Narrow** (e.g. `(5, 95)`) = more "
           "contrast / `range_used_%` but more clipping (watch the 255 spike and `clip_high_%`); **widen** "
           "(e.g. `(1, 99)` / `(0.5, 99.5)`) = less clipping but lower `range_used_%` (flatter).\n"
           "- **S1 / SAR** - amplitude spans orders of magnitude, so linear percentile clipping over-stretches "
           "easily; a `dB` transform (`20*log10`) before clipping usually needs far gentler percentiles. "
           "`RESAMPLE_ALG=\"average\"` smooths speckle; `bilinear`/`cubic` are sharper.\n"
           "- **S2 / optical** - the stretch is per-band, so colour balance shifts if one band clips first. The "
           "active ingestion path instead uses GDAL auto **min/max** (`gdal.Translate(scaleParams=[[]])`), which is "
           "outlier-sensitive and over-stretches on bright pixels (clouds, sunglint) - A/B it against your "
           "percentile choice here. Mixing 10 m and 20 m bands upsamples the coarse one (watch `res_m`).\n"
           "- **`TARGET_RESOLUTION`** - smaller = sharper but quadratically slower warp + larger files "
           "(`None` on S2 keeps the finest 10 m band).\n"
           "- **Compression** - finals are written lossless **DEFLATE** for QA; production uses lossy **JPEG**. "
           "Set `compress=\"jpeg\"` in the process function to compare delivery output."),
    ]
    return notebook(cells)


OUT_NOTEBOOK = OUT / "04_benchmarks.ipynb"
OUT_NOTEBOOK.write_text(json.dumps(build_unified(), indent=1))

print("wrote", OUT_NOTEBOOK.name, "to", OUT)
