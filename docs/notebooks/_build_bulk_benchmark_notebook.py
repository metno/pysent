#!/usr/bin/env python3
"""Generate the bulk throughput notebook (05_bulk_benchmarks.ipynb).

    python docs/notebooks/_build_bulk_benchmark_notebook.py

As with the other notebooks, this file is the source of truth: edit the cell
text here and regenerate, rather than hand-editing the .ipynb JSON.

The notebook measures **throughput**, which is a different question from
``04_benchmarks.ipynb``: not how long one scene takes, but how many scenes an
hour a machine converts, and what that costs in memory. With an archive mounted
it runs a real sweep; without one it shows the recorded sweep and the sizing
arithmetic, so CI keeps executing it.
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


INTRO = '''
# Bulk throughput: how many scenes an hour, and at what cost in memory

`04_benchmarks.ipynb` asks how long **one** scene takes and where the time goes.
This notebook asks the question a production run actually has: with this many
cores and this much memory, **how many scenes an hour**, and how close to the
memory ceiling does that run?

The two answers differ. A single scene is fastest with every core on one product;
a batch is fastest with several scenes in flight, each on a couple of cores. The
sweep below finds that balance for a machine.

It drives [`examples/bulk_convert.py`](../../examples/bulk_convert.py) through
[`examples/bench_bulk.py`](../../examples/bench_bulk.py), so what is measured is
the runner that ships, not a copy of it.

**Without a mounted archive** - CI, or a laptop - the sweep has nothing to
convert, so the notebook falls back to the recorded results from the 16-core
machine the defaults were set on, and still runs the sizing arithmetic for
*this* machine. Point `ARCHIVE_DIR` at real products to measure your own.
'''

PARAMETERS = '''
# --- papermill parameters -------------------------------------------------
ARCHIVE_DIR = ""        # directory of .zip / .SAFE products, e.g. "/data/nbsArchive/S2C/2026/09"
SCENE_LIST = ""         # or a file of scene paths, one per line
GRID = "2x4,4x2"        # configurations as WORKERSxTHREADS
SCENES = 4              # scenes per configuration (the list repeats if shorter)
PRESET = "quicklook"    # "quicklook" keeps a demonstration sweep short; "full" for real numbers
GDAL_CACHEMAX_MB = 256
OUTPUT_DIR = "_bulk_bench"
'''

SETUP = '''
import json, os, sys
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt

# The examples are not an installed package; find them from here or from the
# docs image, whichever this notebook is running in.
here = Path.cwd().resolve()
for candidate in [Path("/opt/pysent/examples"), *(parent / "examples" for parent in [here, *here.parents])]:
    if (candidate / "bulk_convert.py").exists():
        sys.path.insert(0, str(candidate))
        break

import bulk_convert                     # noqa: E402
try:
    import bench_bulk                    # noqa: E402
    HARNESS = True
except ImportError:                      # examples not shipped alongside
    HARNESS = False

scenes = bulk_convert.discover_scenes(
    Path(ARCHIVE_DIR) if ARCHIVE_DIR else None,
    Path(SCENE_LIST) if SCENE_LIST else None,
)
if scenes and SCENES:
    scenes = [scenes[i % len(scenes)] for i in range(SCENES)]

LIVE = bool(scenes) and HARNESS
print(f"cpus this process may use : {bulk_convert.usable_cpus()}")
print(f"memory it may use         : {bulk_convert.usable_memory_gb():.1f} GB")
print(f"scenes found              : {len(scenes)}")
print(f"mode                      : {'measuring' if LIVE else 'recorded results (no archive mounted)'}")
'''

RECORDED_NOTE = '''
## 1. The recorded sweep

Eighteen configurations on a 16-core, 39 GB machine (and on 8 pinned cores of
it), converting real scenes end to end: Sentinel-2 at full resolution and as
quicklooks, and Sentinel-1 VV+VH. Sentinel-2 counts three products per scene.

These are the numbers behind the defaults in
[`examples/README.md`](../../examples/README.md); the cell below plots them, and
section 3 re-measures them on this machine when an archive is available.
'''

RECORDED = '''
RECORDED = [
    # workload,        cores, workers, threads, scenes/hour, peak GB
    ("S2 full",           16,       2,       8,         183,    2.82),
    ("S2 full",           16,       4,       4,         253,    4.81),
    ("S2 full",           16,       8,       2,         312,    7.57),
    ("S2 full",           16,      16,       1,         349,    9.47),
    ("S2 full",            8,       2,       4,         131,    2.56),
    ("S2 full",            8,       4,       2,         165,    3.99),
    ("S2 full",            8,       8,       1,         152,    4.48),
    ("S2 quicklook",      16,       4,       4,        1723,    1.76),
    ("S2 quicklook",      16,       8,       2,        2090,    2.61),
    ("S2 quicklook",      16,      16,       1,        1878,    4.23),
    ("S2 quicklook",       8,       4,       2,        1106,    1.26),
    ("S2 quicklook",       8,       8,       1,        1054,    2.13),
    ("S1 VV+VH",          16,       2,       8,         348,    2.40),
    ("S1 VV+VH",          16,       4,       4,         490,    4.58),
    ("S1 VV+VH",          16,       8,       2,         598,    8.50),
    ("S1 VV+VH",           8,       2,       4,         256,    2.34),
    ("S1 VV+VH",           8,       4,       2,         307,    4.58),
    ("S1 VV+VH",           8,       8,       1,         289,    8.80),
]

figure, axes = plt.subplots(1, 2, figsize=(12, 4.2))
for workload in ("S2 full", "S2 quicklook", "S1 VV+VH"):
    for cores, style in ((16, "-o"), (8, "--s")):
        rows = [r for r in RECORDED if r[0] == workload and r[1] == cores]
        if not rows:
            continue
        workers = [r[2] for r in rows]
        axes[0].plot(workers, [r[4] for r in rows], style, label=f"{workload}, {cores} cores")
        axes[1].plot(workers, [r[5] for r in rows], style, label=f"{workload}, {cores} cores")
axes[0].set(xlabel="worker processes", ylabel="scenes / hour", title="Throughput", yscale="log")
axes[1].set(xlabel="worker processes", ylabel="peak RAM (GB)", title="Memory")
for ax in axes:
    ax.grid(alpha=.3)
    ax.set_xticks([2, 4, 8, 16])
axes[0].legend(fontsize=8)
figure.tight_layout()

best = {}
for workload, cores, workers, threads, rate, _ in RECORDED:
    key = (workload, cores)
    if rate > best.get(key, (0, 0, 0))[0]:
        best[key] = (rate, workers, threads)
for (workload, cores), (rate, workers, threads) in sorted(best.items()):
    print(f"{workload:14s} {cores:2d} cores -> best {rate:5d} scenes/h at {workers:2d} workers x {threads} threads")
'''

SIZING_NOTE = '''
## 2. What the runner would choose here

`--workers auto` takes the smaller of the CPU budget and the memory budget:

```
CPUs / threads-per-worker        and        (RAM - 2 GB) / memory-per-worker
```

reading both from the Slurm allocation, the cgroup limit or the machine - so it
does the right thing inside a container, which `os.cpu_count()` does not. Two
threads per worker measured fastest or near-fastest everywhere, which is why it
is the default divisor.
'''

SIZING = '''
cpus = bulk_convert.usable_cpus()
memory = bulk_convert.usable_memory_gb()
print(f"{'threads/worker':>15} | {'workers':>7} | {'limited by':>12}")
print("-" * 40)
for threads in (1, 2, 4, 8):
    workers = bulk_convert.resolve_workers("auto", threads_per_worker=threads, mem_per_worker_gb=1.5)
    by_cpu = max(1, cpus // threads)
    limit = "cpu" if workers == by_cpu else "memory"
    marker = "  <- default" if threads == 2 else ""
    print(f"{threads:>15} | {workers:>7} | {limit:>12}{marker}")

print(f"\\nthis machine: {cpus} usable cpus, {memory:.1f} GB usable memory")
print("each scene in flight also needs about 1.2 GB of scratch for its warped stack")
'''

SWEEP_NOTE = '''
## 3. Measure this machine

With products to convert, the sweep below runs the real runner once per
configuration and reports what each costs. Without them it prints why it is
skipping, and the recorded table above stands in.

Note the **failed** column. A configuration that fails every scene finishes
quickly and looks fast: during the sweep that produced the table above, one run
reported 469 scenes/hour with every scene failed, because the disk had filled.
Throughput is only a number if the scenes actually converted.
'''

SWEEP = '''
if not LIVE:
    reason = "no scenes found (set ARCHIVE_DIR or SCENE_LIST)" if not scenes else "bench_bulk.py not importable"
    print(f"skipping the live sweep: {reason}")
    rows = []
else:
    import argparse

    args = bench_bulk.build_parser().parse_args([
        "--output-dir", OUTPUT_DIR,
        "--preset", PRESET,
        "--gdal-cachemax-mb", str(GDAL_CACHEMAX_MB),
    ])
    rows = []
    for workers, threads in bench_bulk.parse_grid(GRID):
        print(f"{workers} worker(s) x {threads} thread(s) ...", flush=True)
        row = bench_bulk.run_configuration(scenes, workers, threads, args)
        rows.append(row)
        print(f"   {row['scenes_per_hour']:5d} scenes/h | {row['peak_total_gb']:5.2f} GB | "
              f"{row['failed']} failed", flush=True)

    print()
    print(bench_bulk.format_table(rows))
'''

CLOSING = '''
## 4. Reading the result

- **More workers with fewer threads each** wins until memory runs out. Two
  threads per worker was fastest on 8 cores and within 11 % of the best on 16,
  using 20 % less memory than one thread per worker.
- **Budget 1.0-1.5 GB per worker** at full resolution (Sentinel-1 is the heavy
  one), about 0.4 GB for quicklooks, plus ~1.2 GB of scratch per scene in
  flight. Put `--scratch` on local disk when the outputs go to a shared
  filesystem.
- **The page cache flatters these numbers.** The scenes here were read warm; a
  cold archive or a network filesystem is slower, and the shape of the curve
  matters more than its height.
- **Check the failed column before believing a throughput number**, especially a
  surprisingly good one.

To sweep from a shell instead of a notebook:

```bash
python examples/bench_bulk.py --input-dir /archive/S2C/2026/09 \\
    --grid 4x4,8x2,16x1 --scenes 16 --results-log sweep.jsonl
```
'''


def build():
    cells = [
        md(INTRO),
        code(PARAMETERS, tags=["parameters"]),
        md("## 0. Setup"), code(SETUP),
        md(RECORDED_NOTE), code(RECORDED),
        md(SIZING_NOTE), code(SIZING),
        md(SWEEP_NOTE), code(SWEEP),
        md(CLOSING),
    ]
    path = OUT / "05_bulk_benchmarks.ipynb"
    path.write_text(json.dumps(notebook(cells), indent=1) + "\n")
    print(f"wrote {path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path} ({len(cells)} cells)")


if __name__ == "__main__":
    build()
