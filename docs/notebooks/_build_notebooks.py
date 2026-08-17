#!/usr/bin/env python3
"""Generate the pysent documentation notebooks.

    python docs/notebooks/_build_notebooks.py

This file is the **source of truth** - edit the cell text here and regenerate,
rather than hand-editing the .ipynb JSON (which makes reviews unreadable and
loses the ability to keep the notebooks consistent with each other).

Emits, next to this script:

    01_quickstart.ipynb   what the library is and the shortest path to output
    02_sentinel1.ipynb    SAR amplitude: polarisations, warp, grayscale stretch
    03_sentinel2.ipynb    optical: band combinations and RGB stretch

The benchmark notebook (04_benchmarks.ipynb) has its own generator,
``_build_benchmark_notebook.py``, because its cell text is far longer.

Every notebook opens with a papermill ``parameters`` cell, so CI can execute it
against the committed fixtures while an operator runs the same file against a
mounted archive.
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent


def md(*lines):
    return {"cell_type": "markdown", "metadata": {}, "source": _src(lines)}


def code(*lines, tags=None):
    metadata = {"tags": list(tags)} if tags else {}
    return {"cell_type": "code", "metadata": metadata, "execution_count": None,
            "outputs": [], "source": _src(lines)}


def _src(lines):
    text = lines[0] if len(lines) == 1 and "\n" in lines[0] else "\n".join(lines)
    parts = text.strip("\n").split("\n")
    return [p + "\n" for p in parts[:-1]] + [parts[-1]]


def notebook(cells):
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.x"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def write(name, cells):
    path = OUT / name
    path.write_text(json.dumps(notebook(cells), indent=1) + "\n")
    print(f"wrote {path.relative_to(OUT.parent.parent)} ({len(cells)} cells)")


# --------------------------------------------------------------------------- #
# Shared cells
# --------------------------------------------------------------------------- #
def parameters_cell(platform):
    return code(
        f'''
# --- papermill parameters -------------------------------------------------
# Leave everything as-is to run against the committed test fixtures (what CI
# does). Point any of these at real data for the full pipeline.
DATA_ROOT = ""      # mounted NBS archive, e.g. "/data/nbsArchive"
SAFE_PATH = ""      # explicit .SAFE / .zip product
IDENTIFIER = ""     # catalogue UUID, resolved via pysent.archive
ENDPOINT = None     # None -> NBS_SENTINEL_CSW_ENDPOINT / https://nbs.csw.met.no
OUTPUT_DIR = "_output"
PLATFORM = "{platform}"
''', tags=["parameters"])


SETUP = '''
from pathlib import Path

import numpy as np
import rasterio

import pysent
import nbtools

print("pysent", pysent.__version__, "from", Path(pysent.__file__).parent)

OUTPUT_DIR = Path(OUTPUT_DIR)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
'''

RESOLVE = '''
product = nbtools.resolve_input(
    PLATFORM,
    safe_path=SAFE_PATH or None,
    identifier=IDENTIFIER or None,
    data_root=DATA_ROOT or None,
    endpoint=ENDPOINT,
)
print(nbtools.describe(product))
'''


# --------------------------------------------------------------------------- #
# 01 - Quickstart
# --------------------------------------------------------------------------- #
QUICKSTART_INTRO = '''
# pysent - quickstart

**pysent** turns Sentinel-1 and Sentinel-2 SAFE products into display-ready
GeoTIFF quicklooks. It is the processing core of the NBS ingestion service,
packaged as a library so the service, these notebooks and any other tooling all
run the *same* code.

A quicklook is produced in three steps, and the library exposes each one:

| Step | Sentinel-1 | Sentinel-2 |
|---|---|---|
| **warp** to a target CRS/resolution | `_warp_sentinel_s1_safe_amplitude` (GCP/TPS) | `_warp_sentinel_s2_rgb` (stacked band VRT) |
| **stretch** to 8-bit | `stretch_sentinel_s1_grayscale` (percentile → gray + alpha) | `stretch_sentinel_s2_rgb` (per-band percentile → RGB) |
| **write** tiled, compressed, with overviews | `process_sentinel_s1_safe` | `process_sentinel_s2_safe` |

The `process_*` entry points do all three; the individual functions let you
inspect or tune a single stage, which is what these notebooks do.

> **Running this**: with no parameters set it uses the small committed test
> fixtures, so it works anywhere. Set `DATA_ROOT` to a mounted archive (or
> `SAFE_PATH` / `IDENTIFIER`) to run the full pipeline on a real product.
'''

QUICKSTART_MODULES = '''
| Module | What it is for |
|---|---|
| `pysent.s1` / `pysent.s2` | the processing itself |
| `pysent.profiles` | detect S1 vs S2 from a name, URL or identifier |
| `pysent.archive` | catalogue download URL → local archive path; UUID → SAFE |
| `pysent.csw` | catalogue record lookup (extra: `csw`) |
| `pysent.qa` | benchmarking and stretch-quality measurement (extra: `qa`) |

Top-level names load lazily, so importing `pysent.profiles` does not require the
GDAL bindings that `pysent.s1` needs.
'''


def build_quickstart():
    write("01_quickstart.ipynb", [
        md(QUICKSTART_INTRO),
        parameters_cell("S2"),
        md("## 1. Setup"),
        code(SETUP),
        md("## 2. What is in the library"), md(QUICKSTART_MODULES),
        code('''
for name in ("s1", "s2", "profiles", "archive", "csw", "qa"):
    module = getattr(pysent, name)
    first_line = (module.__doc__ or "").strip().split("\\n")[0]
    print(f"pysent.{name:9s} {first_line}")
'''),
        md("## 3. Platform detection\\n"
           "Detection is pure string matching over any hints you have, so it works on a "
           "filename, a download URL or a catalogue title alike."),
        code('''
from pysent.profiles import detect_nbs_sentinel_platform

for hint in [
    "S1A_IW_GRDH_1SDV_20260810T043022.zip",
    "S2C_MSIL2A_20260810T092031_N0512_R093_T35VPE.SAFE",
    "https://nbstds.met.no/.../nbsArchive/S1D/2026/08/10/IW/S1D_IW_GRDH_1SDV_....zip",
]:
    print(f"{detect_nbs_sentinel_platform(hint)}  <- {hint[:60]}")
'''),
        md("## 4. Find something to process"),
        code(RESOLVE),
        md("## 5. Stretch it\\n"
           "This is the step that turns raw radiometry into something you can look at. "
           "Sentinel-2 counts span a wide range with a long bright tail (cloud, sunglint), "
           "so a **percentile** clip is used rather than min/max."),
        code('''
from pysent.s2 import S2_STRETCH_PERCENTILES, stretch_sentinel_s2_rgb

with rasterio.open(product.path if product.is_fixture else product.path) as src:
    # Fixtures are already the 3 bands we want; a real product needs the warp
    # first (see 03_sentinel2.ipynb) - here we just read what is available.
    indexes = [1, 2, 3] if src.count >= 3 else [1]
    raw = src.read(indexes).astype(np.float32)
    nodata = src.nodata

print("raw:", raw.shape, raw.dtype, "range", raw.min(), "-", raw.max())

if raw.shape[0] == 3:
    stretched, stats = stretch_sentinel_s2_rgb(
        raw, nodata=nodata, percentiles=S2_STRETCH_PERCENTILES)
    print("percentiles used:", S2_STRETCH_PERCENTILES)
    for i, (lo, hi) in enumerate(zip(stats["p_low"], stats["p_high"])):
        print(f"  band {i + 1}: {lo:8.1f} -> 0   {hi:8.1f} -> 255")
'''),
        md("## 6. Look at it\\n"
           "Left is the raw data at full min/max (**no** stretch applied) so the effect of "
           "the stretch on the right is actually visible. Displaying both stretched - the "
           "obvious mistake - makes them look identical and tells you nothing."),
        code('''
import matplotlib.pyplot as plt

if raw.shape[0] == 3:
    fig, axes = plt.subplots(1, 2, figsize=(12, 6))
    rgb_raw = np.transpose(raw, (1, 2, 0))
    axes[0].imshow((rgb_raw - rgb_raw.min()) / max(np.ptp(rgb_raw), 1))
    axes[0].set_title("raw (full min/max, unstretched)")
    axes[1].imshow(np.transpose(stretched, (1, 2, 0)))
    axes[1].set_title(f"percentile stretch {S2_STRETCH_PERCENTILES}")
    for ax in axes:
        ax.set_xticks([]); ax.set_yticks([])
    plt.tight_layout()
'''),
        md("## 7. Where to go next\\n\\n"
           "- **[02_sentinel1.ipynb](02_sentinel1.ipynb)** - SAR: polarisation detection, the GCP warp, dB scaling.\\n"
           "- **[03_sentinel2.ipynb](03_sentinel2.ipynb)** - optical: band combinations, min/max vs percentile.\\n"
           "- **[04_benchmarks.ipynb](04_benchmarks.ipynb)** - time and memory per step; where the cost actually is."),
    ])


# --------------------------------------------------------------------------- #
# 02 - Sentinel-1
# --------------------------------------------------------------------------- #
S1_INTRO = '''
# Sentinel-1 - SAR amplitude quicklooks

Sentinel-1 carries a radar, so a "band" is a **polarisation**: the transmit and
receive orientation of the pulse. Which ones exist depends on the acquisition
mode, and this trips people up:

| Mode token | Polarisations present |
|---|---|
| `1SDV` (IW dual, vertical) | **VV + VH** |
| `1SDH` (EW dual, horizontal) | **HH + HV** |
| `1SSV` / `1SSH` (single) | one band only |

Assuming VV/VH is a real bug - it fails outright on the EW/`1SDH` products that
cover the Norwegian Arctic. `pysent.s1.detect_sentinel_s1_polarizations()` reads
what is actually in the product instead of guessing.

The pipeline is: **warp** the GCP-referenced measurement band to a polar
stereographic grid, **stretch** amplitude to 8-bit grayscale plus an alpha mask,
**write** a tiled GeoTIFF with overviews.
'''

S1_WARP_NOTE = '''
### Why the warp dominates the cost

The measurement raster is not a neat north-up grid: it is referenced by **ground
control points**, and rectifying it is most of the processing time (~27 s of a
~31 s S1 quicklook in our benchmarks).

`_warp_sentinel_s1_safe_amplitude` takes `use_tps`:

- `use_tps=True` (default) - thin-plate spline through the GCPs. Most accurate,
  and the dominant cost.
- `use_tps=False` - GDAL's polynomial GCP transform. Substantially faster, with
  some geometric accuracy given up.

A note that cost us time: the SAFE band is **GCP**-referenced, not a geolocation
array, so `geoloc=True` does *not* apply here and fails with
`Unable to compute a GEOLOC_ARRAY based transformation`. That option belongs to
the NetCDF path only.
'''


def build_sentinel1():
    write("02_sentinel1.ipynb", [
        md(S1_INTRO),
        parameters_cell("S1"),
        md("## 1. Setup"), code(SETUP),
        md("## 2. Find a product"), code(RESOLVE),
        md("## 3. Which polarisations are really in there?\\n"
           "Read from the SAFE manifest's per-band `POLARIZATION` metadata - the same field "
           "the warp matches on - so the answer is what the warp will actually find."),
        code('''
from pysent.s1 import S1_SUPPORTED_AMPLITUDE_VARIABLES, detect_sentinel_s1_polarizations

print("library supports:", S1_SUPPORTED_AMPLITUDE_VARIABLES)

if product.can_warp:
    present = detect_sentinel_s1_polarizations(str(product.path))
    print("present in this product:", present or "(could not read manifest)")
else:
    print("fixture mode: the extract came from", product.label)
    print("its mode token is", product.label.split("_")[3], "-> see the table above")
'''),
        md("## 4. The amplitude stretch\\n"
           "SAR amplitude is heavily skewed: mostly low backscatter with a long bright tail "
           "from urban areas and specular returns. A percentile clip keeps the bulk of the "
           "scene usable instead of letting a few bright pixels dominate."),
        code('''
from pysent.s1 import S1_STRETCH_PERCENTILES, stretch_sentinel_s1_grayscale

with rasterio.open(product.path) as src:
    amplitude = src.read(1).astype(np.float32)
    nodata = src.nodata

gray, alpha, stats = stretch_sentinel_s1_grayscale(
    amplitude, nodata=nodata, percentiles=S1_STRETCH_PERCENTILES)

valid = amplitude > 0
print(f"amplitude   : {amplitude[valid].min():.0f} - {amplitude[valid].max():.0f} "
      f"(mean {amplitude[valid].mean():.0f})")
print(f"clip points : {stats['p_low']:.1f} -> 0,  {stats['p_high']:.1f} -> 255")
print(f"valid pixels: {valid.mean():.1%}  (alpha marks the rest transparent)")
print(f"output      : {gray.dtype}, {(gray[valid] == 0).mean():.1%} black, "
      f"{(gray[valid] == 255).mean():.1%} white")
'''),
        md("## 5. Choosing the percentiles\\n"
           "Two failure modes pull in opposite directions. **Over-stretch** crushes detail "
           "into pure black/white; **under-stretch** wastes the 8-bit range and looks flat. "
           "Sweep the parameter and watch both at once."),
        code('''
print(f"{'percentiles':>14} | {'clipped':>8} | {'range used':>10} | verdict")
print("-" * 56)
for percentiles in [(0.0, 100.0), (1.0, 99.0), (2.0, 98.0), (5.0, 95.0), (20.0, 80.0)]:
    g, _, _ = stretch_sentinel_s1_grayscale(amplitude, nodata=nodata, percentiles=percentiles)
    values = g[valid]
    clipped = ((values == 0) | (values == 255)).mean()
    p2, p98 = np.percentile(values, [2, 98])
    used = (p98 - p2) / 255
    verdict = "over-stretched" if clipped > 0.15 else ("dull" if used < 0.6 else "good")
    print(f"{str(percentiles):>14} | {clipped:7.1%} | {used:9.1%} | {verdict}")
'''),
        md("Wide percentiles clip almost nothing but leave the image dull; narrow ones use "
           "the full range but destroy detail. The default `(2, 98)` sits where both numbers "
           "are acceptable."),
        md(S1_WARP_NOTE),
        code('''
if product.can_warp:
    from pysent.s1 import S1_TARGET_EPSG, S1_TARGET_RESOLUTION, process_sentinel_s1_safe

    variables = detect_sentinel_s1_polarizations(str(product.path))[:1]
    results = process_sentinel_s1_safe(
        input_dataset=str(product.path),
        output_dir=OUTPUT_DIR,
        output_names={v: f"quicklook_{v}.tif" for v in variables},
        processing_options={"target_epsg": S1_TARGET_EPSG,
                            "target_resolution": S1_TARGET_RESOLUTION,
                            "compression": "DEFLATE"},
    )
    for entry in results:
        print(entry)
else:
    print("Fixture mode - no SAFE to warp. Set DATA_ROOT/SAFE_PATH to run this cell.")
'''),
        md("## 7. Tuning notes\\n\\n"
           "- **dB scaling is the biggest quality win still on the table.** Backscatter spans "
           "orders of magnitude; `20*log10(amplitude)` before the percentile clip usually "
           "gives markedly better contrast than the current linear stretch.\\n"
           "- **`use_tps=False` is the biggest speed win**, since the warp is most of the cost.\\n"
           "- **Speckle filtering** (Lee / refined-Lee) before the stretch is worth trying.\\n\\n"
           "Measure any of these with [04_benchmarks.ipynb](04_benchmarks.ipynb)."),
    ])


# --------------------------------------------------------------------------- #
# 03 - Sentinel-2
# --------------------------------------------------------------------------- #
S2_INTRO = '''
# Sentinel-2 - optical band combinations

Sentinel-2 is a multispectral imager: 13 bands at 10/20/60 m. A quicklook picks
**three** of them and maps them to red, green and blue. Which three you choose
decides what the image shows.

| Product | Bands (R, G, B) | Shows |
|---|---|---|
| `true_color_vegetation` | B4, B3, B2 | natural colour, as the eye would see it |
| `false_color_vegetation` | B8A, B4, B3 | near-infrared → vegetation glows red |
| `false_color_glacier` | B12, B8A, B3 | SWIR → separates snow, ice and cloud |

Because the bands have different native resolutions, they are stacked into a
VRT at the finest resolution present and warped together, so the three arrive
co-registered on one grid.

The library reaches bands through GDAL's `SENTINEL2` subdataset abstraction, so
**L1C and L2A both work** despite their different granule layouts.
'''

S2_STRETCH_NOTE = '''
## 5. Min/max versus percentile - a real trade-off

The active ingestion path stretches each band by its **min/max**. It is fast and
needs no sorting, but it is decided by the two most extreme pixels in the scene:
a single sunlit cloud top compresses everything else into the bottom of the
range.

A **percentile** clip ignores the tails and is almost always the better picture.
`_write_stretched_sentinel_s2_rgb_percentile` implements it and is ready to be
wired in - the comparison below is exactly the evidence needed to make that call.
'''


def build_sentinel2():
    write("03_sentinel2.ipynb", [
        md(S2_INTRO),
        parameters_cell("S2"),
        md("## 1. Setup"), code(SETUP),
        md("## 2. Find a product"), code(RESOLVE),
        md("## 3. The shipped band combinations"),
        code('''
from pysent.s2 import S2_DEFAULT_PRODUCTS, S2_SUPPORTED_BANDS, normalize_sentinel_s2_product_map

for name, bands in S2_DEFAULT_PRODUCTS.items():
    print(f"{name:24s} R={bands[0]:4s} G={bands[1]:4s} B={bands[2]:4s}")

print("\\nvalidated against:", S2_SUPPORTED_BANDS)

# Defining your own combination - validated, so a typo fails loudly rather than
# producing a silently wrong image.
custom = normalize_sentinel_s2_product_map({"agriculture": ["B11", "B8A", "B2"]})
print("custom product:", custom)
try:
    normalize_sentinel_s2_product_map({"typo": ["B4", "B3", "B99"]})
except ValueError as exc:
    print("rejected as expected:", exc)
'''),
        md("## 4. Read and stretch"),
        code('''
from pysent.s2 import S2_STRETCH_PERCENTILES, stretch_sentinel_s2_rgb

with rasterio.open(product.path) as src:
    rgb = src.read([1, 2, 3]).astype(np.float32)
    nodata = src.nodata

stretched, stats = stretch_sentinel_s2_rgb(rgb, nodata=nodata, percentiles=S2_STRETCH_PERCENTILES)

print(f"{'band':>6} | {'raw min':>8} | {'raw max':>8} | {'p_low':>8} | {'p_high':>8}")
print("-" * 52)
for i, (lo, hi) in enumerate(zip(stats["p_low"], stats["p_high"])):
    band = rgb[i][rgb[i] > 0]
    print(f"{('RGB'[i]):>6} | {band.min():8.0f} | {band.max():8.0f} | {lo:8.0f} | {hi:8.0f}")
'''),
        md(S2_STRETCH_NOTE),
        code('''
valid = rgb[0] > 0

def _minmax(data):
    out = np.zeros_like(data, dtype=np.uint8)
    for i in range(data.shape[0]):
        band = data[i]
        lo, hi = band[band > 0].min(), band.max()
        out[i] = np.clip((band - lo) / max(hi - lo, 1) * 255, 0, 255).astype(np.uint8)
    return out

minmax = _minmax(rgb)
print(f"{'method':>12} | {'range used':>10} | {'clipped':>8}")
print("-" * 36)
for label, image in (("min/max", minmax), ("percentile", stretched)):
    values = image[0][valid]
    p2, p98 = np.percentile(values, [2, 98])
    clipped = ((values == 0) | (values == 255)).mean()
    print(f"{label:>12} | {(p98 - p2) / 255:9.1%} | {clipped:7.1%}")
print("\\nHigher 'range used' with low 'clipped' is better: the image fills the")
print("8-bit range without destroying detail at either end.")
'''),
        code('''
import matplotlib.pyplot as plt

fig, axes = plt.subplots(1, 3, figsize=(16, 5.5))
raw_display = np.transpose(rgb, (1, 2, 0))
axes[0].imshow((raw_display - raw_display.min()) / max(np.ptp(raw_display), 1))
axes[0].set_title("raw (unstretched)")
axes[1].imshow(np.transpose(minmax, (1, 2, 0)))
axes[1].set_title("min/max stretch (active in production)")
axes[2].imshow(np.transpose(stretched, (1, 2, 0)))
axes[2].set_title(f"percentile {S2_STRETCH_PERCENTILES}")
for ax in axes:
    ax.set_xticks([]); ax.set_yticks([])
plt.tight_layout()
'''),
        md("## 6. The full pipeline\\n"
           "`process_sentinel_s2_safe` does warp → stretch → write for every requested "
           "product, fanning out across products with a thread pool."),
        code('''
if product.can_warp:
    from pysent.s2 import process_sentinel_s2_safe

    name = "true_color_vegetation"
    results = process_sentinel_s2_safe(
        input_dataset=str(product.path),
        output_dir=OUTPUT_DIR,
        product_bands={name: S2_DEFAULT_PRODUCTS[name]},
        output_names={name: f"quicklook_{name}.tif"},
        processing_options={"histogram_stretch": True, "compression": "DEFLATE"},
    )
    for entry in results:
        print(entry)
else:
    print("Fixture mode - no SAFE to warp. Set DATA_ROOT/SAFE_PATH to run this cell.")
'''),
        md("## 7. Tuning notes\\n\\n"
           "- **Switching the active writer to percentile** is the main outstanding quality "
           "decision; the comparison above is the evidence for it.\\n"
           "- **The stretch is not free at full resolution** - `np.percentile` over "
           "3 × 10980² is seconds of work. Estimating the percentiles from a decimated read "
           "costs almost no accuracy.\\n"
           "- **Add band combinations** by extending `S2_DEFAULT_PRODUCTS`, or pass "
           "`product_bands` per call.\\n\\n"
           "Measure any of these with [04_benchmarks.ipynb](04_benchmarks.ipynb)."),
    ])


if __name__ == "__main__":
    build_quickstart()
    build_sentinel1()
    build_sentinel2()
