# pysent documentation

The documentation is a set of **executable notebooks**. Everything in them runs:
no snippet is written out by hand and left to rot, and CI executes all four on
every push, so a change that breaks an example fails the build.

| Notebook | What it covers |
|---|---|
| [01_quickstart.ipynb](notebooks/01_quickstart.ipynb) | What the library is, the three processing stages, first output |
| [02_sentinel1.ipynb](notebooks/02_sentinel1.ipynb) | SAR amplitude: polarisation detection, the GCP warp, choosing stretch percentiles |
| [03_sentinel2.ipynb](notebooks/03_sentinel2.ipynb) | Optical: band combinations, min/max vs percentile stretch |
| [04_benchmarks.ipynb](notebooks/04_benchmarks.ipynb) | Time and memory per step, stretch-quality report, profile comparison |

## Two ways to run them

Every notebook resolves its input the same way (see
[`notebooks/nbtools.py`](notebooks/nbtools.py)), which is what lets one file
serve both cases:

**Fixture mode — works anywhere, no Sentinel data needed.** With no parameters
set, the notebooks use the small windows under `tests/data/`, cut from genuine
recent scenes over Norway. Enough to demonstrate and measure
`read → stretch → write`. The warp needs a real SAFE, so those sections
announce that they are skipped rather than failing. This is what CI runs.

**Archive mode — the full pipeline.** Point the notebooks at real data and the
warp runs too, which is the step worth benchmarking (it is roughly two-thirds of
the total cost).

## Running with Docker

The image carries the library, the geo stack and Jupyter:

```bash
docker build -f docs/Dockerfile -t pysent-docs .

# fixture mode - just open and run
docker run --rm -p 8888:8888 pysent-docs

# archive mode - mount a real archive read-only
docker run --rm -p 8888:8888 \
    -v /lustre/storeB/NBS2/sentinel/production/NorwAREA/nbsArchive:/data/nbsArchive:ro \
    pysent-docs
```

Then open <http://localhost:8888/> and run a notebook. With the archive mounted,
`DATA_ROOT` defaults to `/data/nbsArchive` and the notebooks find the newest
product themselves.

A published image is available if you would rather not build:

```bash
docker pull ghcr.io/metno/pysent-docs:main
```

## Running headlessly

The notebooks are parameterised with [papermill](https://papermill.readthedocs.io),
so they can be executed unattended — which is how CI checks them, and a
reasonable way to produce a benchmark report against a specific product:

```bash
papermill docs/notebooks/04_benchmarks.ipynb report.ipynb \
    -p DATA_ROOT /data/nbsArchive \
    -p PLATFORM S1

# or against one known product
papermill docs/notebooks/04_benchmarks.ipynb report.ipynb \
    -p IDENTIFIER 22d63164-a661-4fed-96c9-e8902ac8f527
```

| Parameter | Meaning |
|---|---|
| `DATA_ROOT` | Mounted NBS archive; the newest matching product is used |
| `SAFE_PATH` | An explicit `.SAFE` / `.zip` to process |
| `IDENTIFIER` | Catalogue UUID, resolved through `pysent.archive` |
| `ENDPOINT` | CSW endpoint override (default `https://nbs.csw.met.no`) |
| `PLATFORM` | Force `S1` / `S2` instead of detecting |
| `OUTPUT_DIR` | Where GeoTIFFs are written |
| `RUN_PROFILE_COMPARISON` | Benchmarks only; the profile sweep costs ~4× a single pass |

## Editing the notebooks

**Do not hand-edit the `.ipynb` JSON.** The notebooks are generated, so that
diffs stay reviewable and the four stay consistent with each other. Edit the
cell text in the generator and re-run it:

```bash
python docs/notebooks/_build_notebooks.py            # 01, 02, 03
python docs/notebooks/_build_benchmark_notebook.py   # 04
```

## Refreshing the test data

The fixtures are windows cut from real products, and the catalogue query that
found them is reproducible. To pull fresher scenes:

```bash
python scripts/make_test_data.py --days 7
```

This searches the NBS catalogue for recent Sentinel-1 GRD and Sentinel-2
products over Norway and extracts a window from each **directly out of the
remote archive** over HTTP range requests — a few hundred KB crosses the
network, not the 1–8 GB product. `tests/data/manifest.json` records exactly
which product each artifact came from.
