# pysent

Sentinel-1 and Sentinel-2 **SAFE → GeoTIFF quicklook** processing, packaged as a
library.

This is the processing core that was previously embedded in the
[FastAPI-mapserver](https://github.com/epifanio/FastAPI-mapserver) ingestion
service (`core/sentinel_s1.py`, `core/sentinel_s2.py`) and duplicated as a
`vendor/` copy inside the QA notebook bundle. It is now one installable package
that both consume, so the service, the notebooks and any new tooling all run
**the same code path** — no vendored copies to re-sync.

```python
from pathlib import Path
from pysent.s2 import S2_DEFAULT_PRODUCTS, process_sentinel_s2_safe

product = "true_color_vegetation"          # ("B4", "B3", "B2")

results = process_sentinel_s2_safe(
    input_dataset="/archive/S2A/2022/03/19/S2A_MSIL1C_20220319T110701.zip",
    output_dir=Path("/out"),
    product_bands={product: S2_DEFAULT_PRODUCTS[product]},
    output_names={product: "scene_true_color.tif"},
    processing_options={"histogram_stretch": True, "compression": "DEFLATE"},
)
```

## What's in it

| Module | Contents |
|---|---|
| `pysent.s1` | Sentinel-1 amplitude quicklooks from SAFE or NetCDF: GCP warp (TPS or polynomial), percentile grayscale stretch + alpha, tiled/compressed GeoTIFF with overviews. Handles **VV/VH, HH/HV and single-pol** products. |
| `pysent.s2` | Sentinel-2 RGB band combinations from SAFE: one stacked-VRT warp per scene, per-band percentile stretch with gamma (or min/max), tiled/compressed 8-bit RGB with overviews. |
| `pysent.profiles` | Platform detection (S1 vs S2) from any textual hint, plus per-platform profile defaults. |
| `pysent.archive` | Catalogue download URL → local archive path; UUID → local SAFE resolution. |
| `pysent.csw` | CSW record lookup (needs the `csw` extra). |
| `pysent.qa` | Benchmark/quality harness: step timing with peak RSS, raster stats, over/under-stretch reports, plotting (needs the `qa` extra). |

Top-level names resolve lazily, so `import pysent.profiles` works in an
environment without the GDAL bindings.

## Install

```bash
pip install pysent                # processing core
pip install "pysent[csw]"         # + CSW lookup (OWSLib)
pip install "pysent[qa]"          # + benchmark/quality harness (pandas, matplotlib)
pip install "pysent[all]"         # everything except the GDAL bindings
```

### Installing the geo stack

`pysent.s1` and `pysent.s2` need the **GDAL Python bindings** (`osgeo`), which
are deliberately *not* a hard dependency — there is no reliable wheel for them,
and every deployment already gets them from the platform. Provide them one of
these ways:

```bash
# Debian/Ubuntu (what the FastAPI-mapserver image does)
apt-get install python3-gdal python3-rasterio
pip install --no-deps pysent

# conda (recommended for a workstation)
conda install -c conda-forge gdal rasterio
pip install pysent
```

`pip install "pysent[gdal]"` exists if you really want pip to build the bindings,
but prefer the system or conda package.

Verified against GDAL 3.8, rasterio 1.5, numpy 1.26 on Python 3.12.

## Documentation

The docs are **executable notebooks**, all of which CI runs on every push:

| Notebook | Covers |
|---|---|
| [01_quickstart](docs/notebooks/01_quickstart.ipynb) | the three processing stages, first output |
| [02_sentinel1](docs/notebooks/02_sentinel1.ipynb) | polarisations, the GCP warp, stretch percentiles |
| [03_sentinel2](docs/notebooks/03_sentinel2.ipynb) | band combinations, percentile vs min/max stretch |
| [04_benchmarks](docs/notebooks/04_benchmarks.ipynb) | time and memory per step, stretch quality |

They run with no Sentinel data at all (using the small committed fixtures), or
against a mounted archive for the full pipeline:

```bash
docker run --rm -p 8888:8888 -v /path/to/nbsArchive:/data/nbsArchive:ro \
    ghcr.io/metno/pysent-docs:main
```

See [docs/README.md](docs/README.md) for details, and
[docs/tuning-and-roadmap.md](docs/tuning-and-roadmap.md) for the measured
baseline and what is worth changing next.

## Development

```bash
git clone https://github.com/metno/pysent && cd pysent
pip install -e ".[test]"
pytest
```

The suite runs without any SAFE product: processing tests build synthetic
rasters, real-scene tests use the committed fixtures under `tests/data/`, and
the GDAL-dependent ones skip cleanly when `osgeo` is absent.

To refresh the fixtures from a newer scene:

```bash
python scripts/make_test_data.py --days 7
```

This queries the NBS catalogue for recent Sentinel-1 GRD and Sentinel-2 products
over Norway and cuts a window from each **directly out of the remote archive**
using HTTP range requests, so a few hundred KB crosses the network rather than
the full 1–8 GB product. `tests/data/manifest.json` records the provenance.

## Bulk processing

For converting many products at once, [`examples/`](examples/README.md) holds a
reference runner and the measurements behind its defaults:

```bash
python examples/bulk_convert.py --input-dir /archive/S2C/2026/09 --output-dir /out
```

One worker process per scene, resumable (a scene is skipped once its sidecar
JSON and outputs are in place), failure-isolated (one bad scene or a worker
killed by the OOM killer does not stop the run) and sized from the CPU and
memory budget it is actually given. [`bulk_from_catalogue.py`](examples/bulk_from_catalogue.py)
starts from catalogue UUIDs or a search instead of a directory, and
[`slurm/bulk_array.sbatch`](examples/slurm/bulk_array.sbatch) runs it as a Slurm
array job.

## Configuration

Processing parameters are passed explicitly via `processing_options`; the
environment variables below only supply defaults when an option is omitted.

| Variable | Effect |
|---|---|
| `S1_PARALLEL_MODE`, `S2_PARALLEL_MODE` | `serial` (default) / `threads` / `processes` (S1 only) fan-out across products |
| `S1_PRODUCT_WORKERS`, `S2_PRODUCT_WORKERS` | Worker count for that fan-out |
| `S1_GDAL_NUM_THREADS`, `S2_GDAL_NUM_THREADS` | GDAL threads per product: warp `NUM_THREADS`, plus `GDAL_NUM_THREADS` (decoding, compression) when products run one at a time |
| `S1_WARP_MEMORY_LIMIT_MB`, `S2_WARP_MEMORY_LIMIT_MB` | GDAL `warpMemoryLimit` |
| `S1_USE_NUMBA` | Enable the numba fast path for the S1 stretch |
| `GDAL_CACHEMAX` | GDAL block cache size, applied even if set after GDAL started |
| `GDAL_NUM_THREADS` | Kept unless `gdal_num_threads` is given. Don't combine it with `parallel_mode="threads"`: GDAL 3.8 can segfault (a warning is raised) |
| `NBS_ARCHIVE_ROOT` | Local mount of the archive (`pysent.archive`) |
| `NBS_SENTINEL_CSW_ENDPOINT`, `CSW_ENDPOINT` | Catalogue endpoints (`pysent.csw`) |
| `NBS_SENTINEL_PLATFORM_PROFILES_JSON` | Profile overrides when the caller supplies none |

### Unattended and bulk runs

- **`work_dir`** (processing option): where intermediates go, default
  `output_dir`. Each call uses its own temporary directory there, removed
  whether it succeeds or fails. Outputs are written under a hidden temporary
  name and renamed when complete, so a killed worker never leaves a truncated
  file under the final name. For Sentinel-2, budget about 2 bytes per pixel per
  band the products need: roughly 1.2 GB for the three default products of a
  10 m scene. `intermediate_compression` (default none) trades time for space.
- **`gdal_cachemax_mb`** (processing option): GDAL block cache during the call.
  GDAL's default of 5 % of RAM applies *per process*.
- **`preset`** (processing option): `full` (default) or `quicklook`, which
  renders Sentinel-2 at 60 m and Sentinel-1 at 160 m. A three-product S2 scene
  takes 5 s instead of 39 s, because GDAL then reads the JP2 overviews.
- **CPU budget:** defaults come from the CPUs the process may use (affinity
  mask and cgroup quota, e.g. `docker --cpus` or Slurm), not the host total.
  Products run one at a time per process by default, so each gets every thread
  for its compression; `parallel_mode="threads"` overlaps them instead.
  Each product of a call may use all of them, so when running several calls
  side by side, set `gdal_num_threads` to each call's share.
  `parallel_mode="processes"` runs serially inside a worker process. Its pool
  uses `forkserver` or `spawn`, so guard scripts with `if __name__ == "__main__":`.
- **Errors** (`pysent.errors`): every product of a call is attempted. If some
  of several fail, `PartialFailure` carries `.results` (the finished products)
  and `.errors`. An S2 scene without valid pixels raises `EmptySceneError`.
  Both subclass `RuntimeError`, and GDAL's own message is included.
- **A ready-made runner** with these settings, resume and failure isolation is
  in [`examples/bulk_convert.py`](examples/bulk_convert.py).

## How Sentinel-2 products are rendered

Each band is stretched from its **0.5–99.5 percentile** range through a **gamma of
0.7** into `[1, 255]`, with **0 reserved for NoData** — so real fill stays
transparent and no valid pixel ever is. On a hazy scene that lifts the mean from
17/255 (plain min/max, which one bright cloud is enough to flatten) to 44/255,
clipping 0.6 % of pixels, nearly all of them cloud tops.

```python
processing_options={
    "histogram_stretch": True,
    "stretch_method": "percentile",     # or "minmax" for the full range
    "stretch_percentiles": (0.5, 99.5),
    "stretch_gamma": 0.7,               # 1.0 for a straight linear ramp
}
```

Sentinel-1 keeps its linear 2–98 percentile stretch and writes an explicit alpha
band, so its NoData was never ambiguous.

## Known tuning work

Carried over from the QA benchmarking; each is measurable with `pysent.qa`:

- **S1 is stretched linearly.** SAR backscatter spans orders of magnitude, so
  `20*log10(amplitude)` before the percentile clip should give better contrast.
- **The S1 warp dominates its cost** (~27 s of 31 s for an S1 GRD).
  `use_tps=False` swaps TPS for the much faster polynomial GCP transform, at the
  price of moving the output grid by a few pixels.
- **S1 warps to EPSG:32661 (UPS North) at 40 m** by default, which suits the
  Nordic archive; scenes further south want their own UTM zone via `target_epsg`.
- Percentiles from a decimated read, and COG output, are both unexplored wins.

## Licence

GNU General Public License v3.0 or later — see [LICENSE](LICENSE).
