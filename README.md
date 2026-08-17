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
| `pysent.s2` | Sentinel-2 RGB band combinations from SAFE: stacked-VRT warp, per-band stretch (min/max or percentile), tiled/compressed 8-bit RGB with overviews. |
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
| [03_sentinel2](docs/notebooks/03_sentinel2.ipynb) | band combinations, min/max vs percentile |
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

## Configuration

Processing parameters are passed explicitly via `processing_options`; the
environment variables below only supply defaults when an option is omitted.

| Variable | Effect |
|---|---|
| `S1_PARALLEL_MODE`, `S2_PARALLEL_MODE` | `threads` / `processes` / `serial` fan-out across products |
| `S1_PRODUCT_WORKERS`, `S2_PRODUCT_WORKERS` | Worker count for that fan-out |
| `S1_GDAL_NUM_THREADS`, `S2_GDAL_NUM_THREADS` | GDAL warp `NUM_THREADS` |
| `S1_WARP_MEMORY_LIMIT_MB`, `S2_WARP_MEMORY_LIMIT_MB` | GDAL `warpMemoryLimit` |
| `S1_USE_NUMBA` | Enable the numba fast path for the S1 stretch |
| `GDAL_CACHEMAX` | GDAL block cache size |
| `NBS_ARCHIVE_ROOT` | Local mount of the archive (`pysent.archive`) |
| `NBS_SENTINEL_CSW_ENDPOINT`, `CSW_ENDPOINT` | Catalogue endpoints (`pysent.csw`) |
| `NBS_SENTINEL_PLATFORM_PROFILES_JSON` | Profile overrides when the caller supplies none |

## Known tuning work

Carried over from the QA benchmarking; each is measurable with `pysent.qa` and
none is shipped as the default yet:

- **S2 stretch is min/max**, which blows out on clouds/sunglint.
  `_write_stretched_sentinel_s2_rgb_percentile` is the outlier-robust
  alternative, pending A/B validation.
- **S1 is stretched linearly.** SAR backscatter spans orders of magnitude, so
  `20*log10(amplitude)` before the percentile clip should give better contrast.
- **The warp dominates cost** (~20 s of 42 s for a 10980² S2 scene; ~27 s of 31 s
  for an S1 GRD). `use_tps=False` on the S1 warp swaps TPS for the much faster
  polynomial GCP transform.
- Percentiles from a decimated read, and COG output, are both unexplored wins.

## Licence

GNU General Public License v3.0 or later — see [LICENSE](LICENSE).
