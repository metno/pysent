# Bulk processing examples

Reference scripts for converting many SAFE products to GeoTIFF. They are plain
standard-library Python on top of pysent, meant to be copied and adapted rather
than installed.

| Script | Use it when |
|---|---|
| [`bulk_convert.py`](bulk_convert.py) | you have the products on disk (or a list of paths/URLs) |
| [`bulk_from_catalogue.py`](bulk_from_catalogue.py) | you have catalogue UUIDs or a search, and want the archive copy resolved for you (needs the `csw` extra) |
| [`slurm/bulk_array.sbatch`](slurm/bulk_array.sbatch) | you are running on a Slurm cluster and want an array job |

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
processed **one after another** (`parallel_mode="serial"`). Parallelism comes
from the number of workers. This is both the best throughput per gigabyte of
RAM and the arrangement that avoids concurrent GeoTIFF writes inside one
process, which can crash GDAL 3.8 (see the note at the end).

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
the machine, in that order. Measured per worker on the benchmark scenes (three
S2 products each, GDAL 3.8): **0.5 GB with 1 thread, 1.1 GB with 2, 1.5 GB with
4**. Each scene in flight also needs about **1.2 GB of scratch** for its warped
stack, so point `--scratch` at local disk when the output directory is on a
shared filesystem.

Throughput on a 16-core, 39 GB machine, 16 Sentinel-2 scenes of three products
each, reading from page cache:

| Workers × threads | Cache | Scenes/hour | Peak RAM |
|---|---:|---:|---:|
| 8 × 2 | 256 MB | **282** | 7.6 GB |
| 16 × 1 | 128 MB | 262 | 6.6 GB |
| 4 × 4 | 256 MB | 222 | 5.4 GB |
| 1 × 16 | 256 MB | 92 | 2.7 GB |

More workers with fewer threads each wins until memory runs out; the defaults
(2 threads, 256 MB of GDAL cache) sit at that sweet spot. The `quicklook`
preset (60 m for S2, 160 m for S1) is roughly 4× faster again: the three real
benchmark scenes take 24 s instead of 99 s.

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
