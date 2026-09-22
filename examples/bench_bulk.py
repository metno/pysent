#!/usr/bin/env python3
"""Sweep worker and thread counts for bulk conversion, and report what each costs.

    python examples/bench_bulk.py --input-dir /archive/S2C/2026/09 --grid 8x2,16x1

Each configuration runs the real runner from ``bulk_convert.py`` over the same
scenes, so what is measured is what ships. For every configuration it reports
scenes per hour, the peak memory of all workers together and of the heaviest
single worker, and - beside them, always - how many scenes failed.

That last column is not decoration. A sweep of mine once reported its best
number ever, 469 scenes/hour, from a run where every scene had failed because
the disk was full: throughput with no successful scenes looks like a win.

Results go to stdout as a markdown table and, with --results-log, as JSONL.
Standard library only, besides pysent. Python 3.10+.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bulk_convert  # noqa: E402  - a sibling example, not an installed module


# --------------------------------------------------------------------------- #
# Memory sampling
# --------------------------------------------------------------------------- #
def _process_tree_rss() -> tuple[int, int]:
    """(summed RSS, largest single RSS) of this process's descendants, in bytes.

    Read from /proc rather than a library so the harness keeps the same
    dependencies as the runner it measures. The workers belong to the runner's
    own pool, so they are found by walking the process tree, not by pid list.
    """
    page = os.sysconf("SC_PAGE_SIZE")
    children: dict[int, list[int]] = {}
    resident: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            parent = int((entry / "stat").read_text().rsplit(") ", 1)[1].split()[1])
            resident[pid] = int((entry / "statm").read_text().split()[1]) * page
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(parent, []).append(pid)

    total = biggest = 0
    queue = list(children.get(os.getpid(), []))
    while queue:
        pid = queue.pop()
        size = resident.get(pid, 0)
        total += size
        biggest = max(biggest, size)
        queue.extend(children.get(pid, []))
    return total, biggest


class MemorySampler:
    """Peak memory of the worker processes while a configuration runs."""

    def __init__(self, interval: float = 0.1):
        self.interval = interval
        self.peak_total = 0
        self.peak_worker = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            total, biggest = _process_tree_rss()
            self.peak_total = max(self.peak_total, total)
            self.peak_worker = max(self.peak_worker, biggest)
            self._stop.wait(self.interval)

    def __enter__(self) -> "MemorySampler":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #
def parse_grid(text: str) -> list[tuple[int, int]]:
    """``"8x2,16x1"`` -> ``[(8, 2), (16, 1)]`` (workers x threads per worker)."""
    grid: list[tuple[int, int]] = []
    for item in text.split(","):
        workers, _, threads = item.strip().lower().partition("x")
        if not workers.isdigit() or not threads.isdigit():
            raise argparse.ArgumentTypeError(f"expected WORKERSxTHREADS, got {item!r}")
        grid.append((int(workers), int(threads)))
    return grid


def run_configuration(
    scenes: Sequence[str],
    workers: int,
    threads: int,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """One configuration: process every scene, measure time, memory and failures."""
    output_dir = Path(args.output_dir) / f"w{workers}_t{threads}"
    shutil.rmtree(output_dir, ignore_errors=True)
    run_args = argparse.Namespace(**vars(args))
    run_args.workers = workers
    run_args.threads_per_worker = threads
    run_args.gdal_cachemax_mb = args.gdal_cachemax_mb
    run_args.force = True
    run_args.results_log = None

    started = time.time()
    with MemorySampler() as sampler:
        summary = bulk_convert.run_bulk(
            scenes, output_dir=output_dir, args=run_args, log=lambda message: None
        )
    elapsed = time.time() - started
    if not args.keep_outputs:
        shutil.rmtree(output_dir, ignore_errors=True)

    done = summary.counts[bulk_convert.OK] + summary.counts[bulk_convert.EMPTY]
    return {
        "workers": workers,
        "threads_per_worker": threads,
        "preset": args.preset,
        "gdal_cachemax_mb": args.gdal_cachemax_mb,
        "scenes": len(scenes),
        "ok": summary.counts[bulk_convert.OK],
        "failed": summary.failed,
        "wall_s": round(elapsed, 1),
        "scenes_per_hour": round(done / elapsed * 3600) if elapsed and done else 0,
        "mean_scene_s": round(elapsed * workers / max(1, done), 1),
        "peak_total_gb": round(sampler.peak_total / 2**30, 2),
        "peak_worker_gb": round(sampler.peak_worker / 2**30, 2),
    }


def format_table(rows: Sequence[dict[str, Any]]) -> str:
    """The results as markdown, ready to paste into examples/README.md."""
    header = ("| Workers × threads | Scenes/hour | Peak RAM | Per worker | Failed |\n"
              "|---|---:|---:|---:|---:|")
    lines = [header]
    best = max((row["scenes_per_hour"] for row in rows if not row["failed"]), default=0)
    for row in rows:
        rate = f"{row['scenes_per_hour']}"
        if row["scenes_per_hour"] == best and not row["failed"]:
            rate = f"**{rate}**"
        failed = f"**{row['failed']}**" if row["failed"] else "0"
        lines.append(
            f"| {row['workers']} × {row['threads_per_worker']} | {rate} | "
            f"{row['peak_total_gb']} GB | {row['peak_worker_gb']} GB | {failed} |"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Recorded results live in examples/README.md and docs/notebooks/05_bulk_benchmarks.ipynb.",
    )
    parser.add_argument("--input-dir", type=Path, help="directory searched recursively for *.zip and *.SAFE")
    parser.add_argument("--input-list", type=Path, help="file of scene paths, one per line")
    parser.add_argument("--output-dir", type=Path, default=Path("bench_output"),
                        help="where each configuration writes (cleared between runs)")
    parser.add_argument("--grid", type=parse_grid, default="8x2,16x1",
                        help="configurations as WORKERSxTHREADS, comma separated (default: 8x2,16x1)")
    parser.add_argument("--scenes", type=int, default=0, metavar="N",
                        help="use only the first N scenes, repeating the list if it is shorter")
    parser.add_argument("--preset", choices=bulk_convert.PRESETS, default="full")
    parser.add_argument("--gdal-cachemax-mb", type=int, default=256, metavar="MB")
    parser.add_argument("--scratch", type=Path, help="directory for intermediates")
    parser.add_argument("--results-log", type=Path, help="write one JSON object per configuration here")
    parser.add_argument("--keep-outputs", action="store_true", help="do not delete each run's products")
    # Accepted so a Namespace can be handed straight to run_bulk().
    parser.add_argument("--products", nargs="+", metavar="NAME")
    parser.add_argument("--polarisations", nargs="+", metavar="POL")
    parser.add_argument("--option", action="append", metavar="KEY=VALUE")
    parser.add_argument("--retries", type=int, default=0)
    parser.add_argument("--max-tasks-per-child", type=int, default=0)
    parser.add_argument("--mem-per-worker-gb", type=float, default=1.5)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if isinstance(args.grid, str):  # argparse only converts non-default values
        args.grid = parse_grid(args.grid)
    if not args.input_dir and not args.input_list:
        build_parser().error("give --input-dir and/or --input-list")

    scenes = bulk_convert.discover_scenes(args.input_dir, args.input_list)
    if not scenes:
        print("no scenes found", file=sys.stderr)
        return 1
    if args.scenes:
        scenes = [scenes[index % len(scenes)] for index in range(args.scenes)]

    print(f"{len(scenes)} scene(s), {len(args.grid)} configuration(s), "
          f"{bulk_convert.usable_cpus()} usable CPUs, preset {args.preset}", file=sys.stderr)

    rows: list[dict[str, Any]] = []
    for workers, threads in args.grid:
        print(f"  {workers} worker(s) x {threads} thread(s) ...", file=sys.stderr, flush=True)
        row = run_configuration(scenes, workers, threads, args)
        rows.append(row)
        print(f"    {row['scenes_per_hour']} scenes/h, {row['peak_total_gb']} GB, "
              f"{row['failed']} failed", file=sys.stderr, flush=True)
        if args.results_log:
            with Path(args.results_log).open("a") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")

    print(format_table(rows))
    return 1 if any(row["failed"] for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
