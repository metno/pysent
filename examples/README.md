# Bulk processing examples

Reference scripts for converting many SAFE products to GeoTIFF. They are plain
standard-library Python on top of pysent, meant to be copied and adapted rather
than installed.

| Script | Use it when |
|---|---|
| [`bulk_convert.py`](bulk_convert.py) | you have the products on disk (or a list of paths/URLs) |
| [`bulk_from_catalogue.py`](bulk_from_catalogue.py) | you have catalogue UUIDs or a search, and want the archive copy resolved for you (needs the `csw` extra) |
| [`slurm/bulk_array.sbatch`](slurm/bulk_array.sbatch) | you are running on a Slurm cluster and want an array job |
| [`bench_bulk.py`](bench_bulk.py) | you want to measure the table below on your own machine |

```bash
# everything under a directory, sized automatically for the machine
python examples/bulk_convert.py --input-dir /archive/S2C/2026/09 --output-dir /out

# a list of scenes, 60 m quicklooks, node-local scratch
python examples/bulk_convert.py --input-list scenes.txt --output-dir /out \
    --preset quicklook --scratch /tmp/pysent

# what would happen, without doing it
python examples/bulk_convert.py --input-dir /archive --output-dir /out --dry-run
```

## How a run is arranged

One worker process per scene, and inside each worker the scene's products are
processed **one after another** (`parallel_mode="serial"`, now also the library
default). Parallelism comes from the number of workers. This is the best
throughput per gigabyte of RAM, it lets each write use every thread it is given
for compression, and it avoids concurrent GeoTIFF writes inside one process,
which can crash GDAL 3.8 (see the note at the end).

Outputs are laid out per platform and scene, with a sidecar JSON written last:

```
<output-dir>/S2/<product name>/<product name>_true_color_vegetation.tif
                              /<product name>_false_color_glacier.tif
                              /<product name>.json          <- options, stats, versions, timings
<output-dir>/bulk_results.jsonl                             <- one line per scene
```

## Sizing

`--workers auto` (the default) takes the smaller of

```
CPUs / --threads-per-worker          and          (RAM - 2 GB) / --mem-per-worker-gb
```

reading the CPU count and memory from the Slurm allocation, the cgroup limit or
the machine, in that order.

**Two GDAL threads per worker is the sweet spot**, which is why `--workers auto`
divides by that. Measured on a 16-core / 39 GB machine and on 8 pinned cores of
it, processing real scenes end to end (Sentinel-2 = three products per scene):

| Scenes/hour | 8 cores | 16 cores | peak RAM (16 cores) |
|---|---:|---:|---:|
| **S2 full, 2 threads/worker** | **165** | 312 | 7.6 GB |
| S2 full, 1 thread/worker | 152 | **349** | 9.5 GB |
| S2 full, 4 threads/worker | 131 | 253 | 4.8 GB |
| **S2 quicklook, 2 threads/worker** | **1106** | **2090** | 2.6 GB |
| S2 quicklook, 1 thread/worker | 1054 | 1878 | 4.2 GB |
| **S1, 2 threads/worker** | **307** | **598** | 8.5 GB |
| S1, 4 threads/worker | 256 | 490 | 4.6 GB |

One thread per worker edges ahead on 16 cores for full S2 (+12 %) but costs 25 %
more memory; everywhere else two threads wins outright. Per worker, budget
**1.0–1.5 GB** (S1 is the heavy one at ~1.4 GB, quicklook needs only ~0.4 GB),
plus about **1.2 GB of scratch** per scene in flight — point `--scratch` at local
disk when the outputs live on a shared filesystem.

Scenes come from a warm page cache here, so a cold archive or a network
filesystem will be slower; the shape of the table is what matters.

To measure your own machine, sweep the grid with the harness that produced this
table — or run [`05_bulk_benchmarks.ipynb`](../docs/notebooks/05_bulk_benchmarks.ipynb),
which does the same and plots it:

```bash
python examples/bench_bulk.py --input-dir /archive/S2C/2026/09 \
    --grid 4x4,8x2,16x1 --scenes 16 --results-log sweep.jsonl
```

**Sentinel-1 with `--option speckle_filter=lee` costs about 9 s more per
polarisation** (a VV+VH scene goes from 20 s to 38 s) and about 0.2 GB more per
worker. It is off by default; with the `quicklook` preset it is much cheaper
(23.5 s), because the coarser grid has already averaged most speckle away.

## Changing how the images look

The runner passes `histogram_stretch` on, so Sentinel-2 products get the library
default: percentile 0.5–99.5 through a gamma of 0.7, into `[1,255]` with 0 kept
for NoData. Any processing option can be overridden from the command line:

```bash
--option stretch_method=minmax        # the full range instead of percentiles
--option stretch_gamma=1.0            # a straight linear ramp
--option stretch_percentiles=1,99     # a wider or narrower clip
--option compression=JPEG             # smaller, lossy output
```

## Resuming, and what counts as done

A scene is skipped when its sidecar JSON exists and every output it lists is
present. The sidecar is written **after** the GeoTIFFs, and pysent writes each
GeoTIFF under a temporary name and renames it when complete, so a worker killed
mid-scene leaves nothing that looks finished. Re-running the same command picks
up where it stopped; `--force` reprocesses regardless.

A scene with no valid pixels is recorded as `empty` and not retried.

## Failure modes

Each scene ends as `ok`, `skipped`, `empty` or `failed` in
`<output-dir>/bulk_results.jsonl`, with the error and traceback for failures.
The exit code is non-zero if anything failed or the run was interrupted.

| What happened | What the runner does |
|---|---|
| One product of a scene fails | The others still finish; the scene is `failed` and lists what was written |
| The archive is briefly unreachable | Retried with backoff, `--retries` times (default 2) |
| A worker is killed (out of memory) | Only the scene it held is marked failed, the pool is rebuilt and the run continues |
| Ctrl-C | Scenes already running are finished, the rest are cancelled, the exit code is non-zero |
| A scene is neither S1 nor S2 | Recorded as failed with that reason; `--dry-run` reports such inputs up front |

If workers keep dying, lower `--workers` or `--threads-per-worker`, or raise
`--mem-per-worker-gb` so `auto` picks fewer.

## Environment

The runner sets these in each worker, from its own options:
`GDAL_NUM_THREADS`, `GDAL_CACHEMAX`, `NUMBA_NUM_THREADS`, `OMP_NUM_THREADS`,
`OPENBLAS_NUM_THREADS`, and `NUMBA_CACHE_DIR` when `--scratch` is given.

`bulk_from_catalogue.py` also reads `NBS_ARCHIVE_ROOT` (local mount of the
archive) and `NBS_SENTINEL_CSW_ENDPOINT` (catalogue), or takes
`--archive-root` and `--endpoint`.

Products that are not in the local archive are read straight from the
catalogue's download URL through `/vsizip//vsicurl/`, over HTTP range requests
rather than a full download.

## A word on GDAL threading

`GDAL_NUM_THREADS` also switches on multi-threaded GeoTIFF compression. With
several products written from threads of a single process, GDAL 3.8.4 can
segfault in `GDALRasterBlock::Internalize()` — reproduced in a couple of scenes
of a two-worker batch. pysent therefore leaves the variable alone whenever it
runs products in parallel threads, and these scripts use `serial` products per
worker, where setting it is safe and worth about 20 % in wall time.
