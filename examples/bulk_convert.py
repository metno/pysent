#!/usr/bin/env python3
"""Convert many Sentinel-1/2 SAFE products to GeoTIFF, one process per scene.

    python examples/bulk_convert.py --input-dir /archive/S2C/2026/09 --output-dir /out

Each scene is processed by one worker process, and each worker asks pysent to
run that scene's products one after another (``parallel_mode="serial"``). That
is both the fastest arrangement per gigabyte of RAM and the one that keeps GDAL
off the code path where concurrent writes in a single process can crash it. See
``examples/README.md`` for the measurements behind the defaults.

The runner is idempotent: a scene whose sidecar JSON and outputs are all present
is skipped, so an interrupted run can simply be started again.

Importable as well:

    from bulk_convert import run_bulk
    summary = run_bulk(scenes=[...], output_dir=Path("/out"))

Standard library only, besides pysent itself. Python 3.10+.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import sys
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass, field
from multiprocessing import get_all_start_methods, get_context
from pathlib import Path
from typing import Any, Callable, Sequence

from pysent.profiles import detect_nbs_sentinel_platform

try:  # the exception a dead worker raises
    from concurrent.futures.process import BrokenProcessPool as BrokenProcessPoolError
except ImportError:  # pragma: no cover
    from concurrent.futures import BrokenExecutor as BrokenProcessPoolError  # type: ignore[assignment]

# Status values written to the results log.
OK, SKIPPED, EMPTY, FAILED = "ok", "skipped", "empty", "failed"

S2_PRODUCT_NAMES = ("true_color_vegetation", "false_color_glacier", "false_color_vegetation")

# The presets themselves live in pysent (pysent.s1.S1_PRESETS, pysent.s2.S2_PRESETS);
# the runner only passes the name through, so the two cannot drift apart.
PRESETS = ("full", "quicklook")

# A scene that failed on one of these is worth retrying: the archive was briefly
# unreachable rather than the product being broken.
_RETRYABLE = re.compile(
    r"vsicurl|curl|HTTP\s*(error|response)?\s*(4\d\d|5\d\d)|timed?\s*out|timeout|"
    r"connection\s*(reset|refused|closed)|temporarily unavailable|network",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Scene discovery
# --------------------------------------------------------------------------- #
def discover_scenes(input_dir: Path | None = None, input_list: Path | None = None) -> list[str]:
    """Collect SAFE products from a directory tree and/or a list file.

    A list may hold local paths or GDAL virtual paths such as
    ``/vsizip//vsicurl/https://host/S2C_....zip``. Blank lines and ``#``
    comments are ignored.
    """
    scenes: list[str] = []
    if input_dir is not None:
        for path in sorted(input_dir.rglob("*")):
            if path.name.startswith("."):
                continue
            if path.is_file() and path.suffix.lower() == ".zip":
                scenes.append(str(path))
            elif path.is_dir() and path.name.upper().endswith(".SAFE"):
                scenes.append(str(path))
    if input_list is not None:
        for line in input_list.read_text().splitlines():
            entry = line.strip()
            if entry and not entry.startswith("#"):
                scenes.append(entry)
    # Keep the first occurrence of each scene, in order.
    return list(dict.fromkeys(scenes))


def scene_name(scene: str) -> str:
    """The product name of a scene reference, without directories or suffixes."""
    name = _basename(scene)
    for suffix in (".zip", ".SAFE", ".safe"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name or "scene"


def _basename(scene: str) -> str:
    # Works for local paths and for /vsizip//vsicurl/https://... references alike.
    return scene.rstrip("/").split("/")[-1]


def scene_family(scene: str) -> str:
    """``S1``, ``S2`` or ``unknown``, from the product name."""
    return detect_nbs_sentinel_platform(scene_name(scene))["family"]


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #
def usable_cpus() -> int:
    """CPUs this process may use: Slurm allocation, affinity mask or cgroup quota."""
    limits = []
    slurm = os.environ.get("SLURM_CPUS_PER_TASK") or os.environ.get("SLURM_CPUS_ON_NODE")
    if slurm and slurm.isdigit():
        limits.append(int(slurm))
    if hasattr(os, "process_cpu_count"):  # Python 3.13+
        limits.append(os.process_cpu_count() or 1)
    elif hasattr(os, "sched_getaffinity"):
        limits.append(len(os.sched_getaffinity(0)))
    else:  # pragma: no cover - macOS/Windows
        limits.append(os.cpu_count() or 1)
    quota = _cgroup_cpu_quota()
    if quota:
        limits.append(quota)
    return max(1, min(limits))


def _cgroup_cpu_quota() -> int | None:
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            return max(1, math.ceil(float(quota) / float(period)))
    except (OSError, ValueError):
        pass
    try:
        quota = float(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = float(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if quota > 0 and period > 0:
            return max(1, math.ceil(quota / period))
    except (OSError, ValueError):
        pass
    return None


def usable_memory_gb() -> float:
    """Memory this process may use, from Slurm, the cgroup limit or /proc/meminfo."""
    limits: list[float] = []
    for name in ("SLURM_MEM_PER_NODE", "SLURM_MEM_PER_CPU"):
        value = os.environ.get(name)
        if value and value.isdigit():  # megabytes
            total = float(value) / 1024.0
            if name == "SLURM_MEM_PER_CPU":
                total *= usable_cpus()
            limits.append(total)
    try:
        limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if limit.isdigit():
            limits.append(float(limit) / 2**30)
    except OSError:
        pass
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                limits.append(float(line.split()[1]) / 2**20)
                break
    except (OSError, IndexError, ValueError):
        pass
    return min(limits) if limits else 8.0


def resolve_workers(
    requested: str | int,
    *,
    threads_per_worker: int,
    mem_per_worker_gb: float,
    cpus: int | None = None,
    memory_gb: float | None = None,
    reserve_gb: float = 2.0,
) -> int:
    """Worker count from the CPU and memory budget, unless a number was given.

    Two GDAL threads per worker measured fastest or near-fastest everywhere
    (see the table in ``examples/README.md``), and a worker peaks at 1.0-1.5 GB
    on full-resolution scenes.
    """
    if str(requested).strip().lower() not in {"auto", ""}:
        return max(1, int(requested))
    cpus = usable_cpus() if cpus is None else cpus
    memory_gb = usable_memory_gb() if memory_gb is None else memory_gb
    by_cpu = max(1, cpus // max(1, threads_per_worker))
    by_memory = max(1, int((memory_gb - reserve_gb) / max(0.1, mem_per_worker_gb)))
    return max(1, min(by_cpu, by_memory))


# --------------------------------------------------------------------------- #
# One scene, in a worker process
# --------------------------------------------------------------------------- #
def init_worker(env: dict[str, str]) -> None:
    """Pin every threading layer in the worker and let the parent own Ctrl-C."""
    os.environ.update(env)
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def scene_output_dir(output_dir: Path, scene: str) -> Path:
    return Path(output_dir) / scene_family(scene) / scene_name(scene)


def sidecar_path(output_dir: Path, scene: str) -> Path:
    return scene_output_dir(output_dir, scene) / f"{scene_name(scene)}.json"


def scene_is_done(output_dir: Path, scene: str) -> bool:
    """True when the sidecar and every output it records are on disk.

    The sidecar is written after the outputs, so its presence means the scene
    finished; a killed worker leaves no sidecar and the scene runs again.
    """
    sidecar = sidecar_path(output_dir, scene)
    try:
        record = json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return False
    if record.get("status") == EMPTY:
        return True  # no valid pixels: nothing to write, and nothing to retry
    outputs = record.get("outputs") or []
    return bool(outputs) and all(Path(path).exists() for path in outputs)


def process_scene(job: dict[str, Any]) -> dict[str, Any]:
    """Convert one SAFE product. Runs in a worker process; never raises."""
    started = time.time()
    scene = job["scene"]
    out_dir = Path(job["output_dir"])
    record: dict[str, Any] = {
        "scene": scene,
        "name": scene_name(scene),
        "family": job["family"],
        "output_dir": str(out_dir),
        "status": FAILED,
        "outputs": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started)),
    }
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        if job["family"] == "S2":
            results = _run_s2(job, out_dir)
        elif job["family"] == "S1":
            results = _run_s1(job, out_dir)
        else:
            raise ValueError(f"Cannot tell whether {scene_name(scene)} is Sentinel-1 or Sentinel-2")
        record["status"] = OK
        record["results"] = results
        record["outputs"] = [item["path"] for item in results]
    except BaseException as exc:  # noqa: BLE001 - the status is the product of this call
        from pysent.errors import EmptySceneError, PartialFailure

        record["error_type"] = type(exc).__name__
        record["error"] = str(exc)
        record["traceback"] = traceback.format_exc(limit=12)
        if isinstance(exc, EmptySceneError):
            record["status"] = EMPTY
        elif isinstance(exc, PartialFailure):
            # Products that did finish are on disk; the scene still counts as failed.
            record["results"] = exc.results
            record["outputs"] = [item["path"] for item in exc.results]
            record["errors"] = {name: f"{type(err).__name__}: {err}" for name, err in exc.errors.items()}
    record["elapsed_s"] = round(time.time() - started, 1)
    record["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    record["versions"] = _versions()
    record["options"] = job["options"]
    if record["status"] in (OK, EMPTY):
        # Written last: the sidecar is what marks the scene as done.
        sidecar = out_dir / f"{scene_name(scene)}.json"
        sidecar.write_text(json.dumps(record, indent=2, sort_keys=True))
        record["sidecar"] = str(sidecar)
    return record


def _run_s2(job: dict[str, Any], out_dir: Path) -> list[dict[str, Any]]:
    from pysent.s2 import S2_DEFAULT_PRODUCTS, build_sentinel_s2_output_filename, process_sentinel_s2_safe

    names = job["products"] or list(S2_DEFAULT_PRODUCTS)
    unknown = [name for name in names if name not in S2_DEFAULT_PRODUCTS]
    if unknown:
        raise ValueError(f"Unknown Sentinel-2 products {unknown}; available: {sorted(S2_DEFAULT_PRODUCTS)}")
    identifier = scene_name(job["scene"])
    return process_sentinel_s2_safe(
        input_dataset=job["scene"],
        output_dir=out_dir,
        product_bands={name: S2_DEFAULT_PRODUCTS[name] for name in names},
        output_names={
            name: build_sentinel_s2_output_filename(product_name=name, identifier=identifier) for name in names
        },
        processing_options=job["options"],
    )


def _run_s1(job: dict[str, Any], out_dir: Path) -> list[dict[str, Any]]:
    from pysent.s1 import (
        S1_SUPPORTED_AMPLITUDE_VARIABLES,
        build_sentinel_s1_output_filename,
        detect_sentinel_s1_polarizations,
        process_sentinel_s1_safe,
    )

    variables = [f"Amplitude_{pol.upper()}" for pol in job["polarisations"]]
    if not variables:
        variables = detect_sentinel_s1_polarizations(job["scene"]) or list(S1_SUPPORTED_AMPLITUDE_VARIABLES)
    identifier = scene_name(job["scene"])
    return process_sentinel_s1_safe(
        input_dataset=job["scene"],
        output_dir=out_dir,
        output_names={
            variable: build_sentinel_s1_output_filename(variable=variable, identifier=identifier)
            for variable in variables
        },
        processing_options=job["options"],
    )


def _versions() -> dict[str, str]:
    versions = {"python": sys.version.split()[0]}
    try:
        import pysent

        versions["pysent"] = pysent.__version__
    except Exception:  # pragma: no cover - pysent is always importable in a worker
        pass
    try:
        from osgeo import gdal

        versions["gdal"] = gdal.__version__
    except Exception:  # pragma: no cover - only if the geo stack is missing
        pass
    return versions


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #
@dataclass
class Summary:
    """What became of every scene."""

    total: int = 0
    counts: dict[str, int] = field(default_factory=lambda: {OK: 0, SKIPPED: 0, EMPTY: 0, FAILED: 0})
    records: list[dict[str, Any]] = field(default_factory=list)
    elapsed_s: float = 0.0
    interrupted: bool = False

    @property
    def failed(self) -> int:
        return self.counts[FAILED]

    @property
    def scenes_per_hour(self) -> float:
        done = self.counts[OK] + self.counts[EMPTY]
        return round(done / self.elapsed_s * 3600, 1) if self.elapsed_s > 0 and done else 0.0

    def line(self) -> str:
        counts = " ".join(f"{name}={self.counts[name]}" for name in (OK, SKIPPED, EMPTY, FAILED))
        return f"{counts} in {self.elapsed_s:.0f}s ({self.scenes_per_hour} scenes/h)"


def build_job(scene: str, *, output_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    family = scene_family(scene)
    options: dict[str, Any] = {
        # One product at a time per worker: parallelism comes from the workers.
        "parallel_mode": "serial",
        "gdal_num_threads": args.threads_per_worker,
        "gdal_cachemax_mb": args.gdal_cachemax_mb,
    }
    if family == "S2":
        options["histogram_stretch"] = True
    if args.scratch:
        options["work_dir"] = str(args.scratch)
    if args.preset:
        options["preset"] = args.preset
    for item in args.option or []:
        key, _, value = item.partition("=")
        options[key.strip()] = _coerce(value.strip())
    return {
        "scene": scene,
        "family": family,
        "output_dir": str(scene_output_dir(output_dir, scene)),
        "products": list(args.products or []),
        "polarisations": list(args.polarisations or []),
        "options": options,
    }


def _coerce(value: str) -> Any:
    if "," in value:  # e.g. stretch_percentiles=0.5,99.5
        return tuple(_coerce(part.strip()) for part in value.split(","))
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    return value


def run_bulk(
    scenes: Sequence[str],
    *,
    output_dir: Path,
    args: argparse.Namespace,
    process: Callable[[dict[str, Any]], dict[str, Any]] = process_scene,
    log: Callable[[str], None] = lambda message: print(message, file=sys.stderr, flush=True),
) -> Summary:
    """Process every scene, one worker process each, and return what happened."""
    summary = Summary(total=len(scenes))
    output_dir.mkdir(parents=True, exist_ok=True)
    results_log = Path(args.results_log) if args.results_log else output_dir / "bulk_results.jsonl"
    results_log.parent.mkdir(parents=True, exist_ok=True)

    pending: list[str] = []
    for scene in scenes:
        if not args.force and scene_is_done(output_dir, scene):
            record = {"scene": scene, "name": scene_name(scene), "status": SKIPPED}
            _record(summary, record, results_log)
            log(f"  skipped {scene_name(scene)} (already done)")
        else:
            pending.append(scene)

    if not pending:
        return summary

    workers = min(args.workers, len(pending))
    log(f"{len(pending)} scene(s) to process, {workers} worker(s), {args.threads_per_worker} GDAL thread(s) each")
    env = {
        "GDAL_NUM_THREADS": str(args.threads_per_worker),
        "GDAL_CACHEMAX": str(args.gdal_cachemax_mb),
        "NUMBA_NUM_THREADS": str(args.threads_per_worker),
        "OMP_NUM_THREADS": str(args.threads_per_worker),
        "OPENBLAS_NUM_THREADS": str(args.threads_per_worker),
    }
    if args.scratch:
        env["NUMBA_CACHE_DIR"] = str(args.scratch)

    attempts: dict[str, int] = {}
    queue = list(pending)
    started = time.time()
    interrupted = False

    previous_sigint = signal.getsignal(signal.SIGINT)

    def on_sigint(signum, frame):  # pragma: no cover - exercised interactively
        nonlocal interrupted
        if interrupted:  # second Ctrl-C: let the default handler tear everything down
            signal.signal(signal.SIGINT, previous_sigint)
            raise KeyboardInterrupt
        interrupted = True
        log("interrupted: finishing the scenes already running, cancelling the rest")

    try:
        signal.signal(signal.SIGINT, on_sigint)
    except ValueError:  # pragma: no cover - not the main thread
        pass

    try:
        while queue and not interrupted:
            executor = _make_executor(workers, env, args)
            running: dict[Future, str] = {}
            collecting: str | None = None  # the scene whose result is being read
            try:
                while running or (queue and not interrupted):
                    while queue and not interrupted and len(running) < workers:
                        scene = queue.pop(0)
                        attempts[scene] = attempts.get(scene, 0) + 1
                        running[executor.submit(process, build_job(scene, output_dir=output_dir, args=args))] = scene
                    if not running:
                        break
                    done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                    for future in done:
                        scene = running.pop(future)
                        collecting = scene
                        record = _result_of(future, scene)
                        collecting = None
                        if (
                            record["status"] == FAILED
                            and attempts[scene] <= args.retries
                            and _RETRYABLE.search(f"{record.get('error_type', '')} {record.get('error', '')}")
                        ):
                            delay = 2 ** (attempts[scene] - 1)
                            log(f"  retrying {scene_name(scene)} in {delay}s ({record.get('error', '')[:80]})")
                            time.sleep(delay)
                            queue.append(scene)
                            continue
                        _record(summary, record, results_log)
                        summary.elapsed_s = time.time() - started
                        log(_progress(summary, record))
            except BrokenProcessPoolError:
                # A worker died outright, usually the OOM killer. Only the scenes
                # that were in flight are lost; the rest keep their place. The
                # scene being collected died with it, so count it too.
                lost = ([collecting] if collecting else []) + list(running.values())
                for scene in lost:
                    record = {
                        "scene": scene,
                        "name": scene_name(scene),
                        "status": FAILED,
                        "error_type": "BrokenProcessPool",
                        "error": "worker process died (out of memory?); lower --workers or --threads-per-worker",
                    }
                    _record(summary, record, results_log)
                    log(_progress(summary, record))
                running.clear()
                log("  worker pool died, restarting it")
                continue
            finally:
                executor.shutdown(wait=not interrupted, cancel_futures=True)
    finally:
        try:
            signal.signal(signal.SIGINT, previous_sigint)
        except ValueError:  # pragma: no cover - not the main thread
            pass

    for scene in queue:
        _record(summary, {"scene": scene, "name": scene_name(scene), "status": FAILED,
                          "error_type": "Cancelled", "error": "not processed (interrupted)"}, results_log)
    summary.elapsed_s = time.time() - started
    summary.interrupted = interrupted
    return summary


def _make_executor(workers: int, env: dict[str, str], args: argparse.Namespace) -> ProcessPoolExecutor:
    # forkserver/spawn: forking after GDAL or numba have started threads can hang
    # the child. max_tasks_per_child keeps a slow leak from growing over a long run.
    method = "forkserver" if "forkserver" in get_all_start_methods() else "spawn"
    kwargs: dict[str, Any] = {
        "max_workers": workers,
        "mp_context": get_context(method),
        "initializer": init_worker,
        "initargs": (env,),
    }
    if args.max_tasks_per_child and sys.version_info >= (3, 11):
        kwargs["max_tasks_per_child"] = args.max_tasks_per_child
    return ProcessPoolExecutor(**kwargs)


def _result_of(future: Future, scene: str) -> dict[str, Any]:
    try:
        return future.result()
    except BrokenProcessPoolError:
        raise
    except BaseException as exc:  # noqa: BLE001 - a worker should never do this
        return {
            "scene": scene,
            "name": scene_name(scene),
            "status": FAILED,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(limit=12),
        }


def _record(summary: Summary, record: dict[str, Any], results_log: Path) -> None:
    summary.counts[record["status"]] = summary.counts.get(record["status"], 0) + 1
    summary.records.append(record)
    with results_log.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _progress(summary: Summary, record: dict[str, Any]) -> str:
    done = sum(summary.counts.values())
    rate = summary.scenes_per_hour
    remaining = summary.total - done
    eta = f", ETA {remaining / rate * 60:.0f} min" if rate and remaining else ""
    detail = f" ({record.get('error_type')}: {str(record.get('error', ''))[:80]})" if record["status"] == FAILED else ""
    return f"[{done}/{summary.total}] {record['status']:7s} {record['name']} {rate} scenes/h{eta}{detail}"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See examples/README.md for sizing guidance and failure modes.",
    )
    source = parser.add_argument_group("inputs")
    source.add_argument("--input-dir", type=Path, help="directory searched recursively for *.zip and *.SAFE")
    source.add_argument("--input-list", type=Path, help="file of scene paths or /vsizip//vsicurl/ URLs, one per line")
    parser.add_argument("--output-dir", type=Path, required=True, help="outputs go to <output-dir>/<S1|S2>/<scene>/")
    parser.add_argument("--scratch", type=Path, help="directory for intermediates (default: alongside the outputs)")

    products = parser.add_argument_group("what to produce")
    products.add_argument("--preset", choices=PRESETS, default="full",
                          help="full: the library defaults; quicklook: 60 m (S2) / 160 m (S1)")
    products.add_argument("--products", nargs="+", metavar="NAME",
                          help=f"Sentinel-2 products (default: all of {', '.join(S2_PRODUCT_NAMES)})")
    products.add_argument("--polarisations", nargs="+", metavar="POL",
                          help="Sentinel-1 polarisations, e.g. VV VH (default: those the product has)")
    products.add_argument("--option", action="append", metavar="KEY=VALUE",
                          help="extra pysent processing option, repeatable")

    execution = parser.add_argument_group("execution")
    execution.add_argument("--workers", default="auto",
                           help="worker processes, or auto from the CPU and memory budget (default: auto)")
    execution.add_argument("--threads-per-worker", type=int, default=2, metavar="N",
                           help="GDAL threads per worker (default: 2)")
    execution.add_argument("--mem-per-worker-gb", type=float, default=1.5, metavar="GB",
                           help="memory budgeted per worker when sizing --workers auto "
                                "(default: 1.5, measured worst case; quicklook needs ~0.5)")
    execution.add_argument("--gdal-cachemax-mb", type=int, default=256, metavar="MB",
                           help="GDAL block cache per worker (default: 256)")
    execution.add_argument("--max-tasks-per-child", type=int, default=20, metavar="N",
                           help="restart a worker after N scenes, 0 to never (default: 20, needs Python 3.11+)")
    execution.add_argument("--retries", type=int, default=2, metavar="N",
                           help="retries per scene for network-like failures (default: 2)")
    execution.add_argument("--force", action="store_true", help="reprocess scenes that are already done")
    execution.add_argument("--dry-run", action="store_true", help="list what would happen and exit")
    execution.add_argument("--results-log", type=Path, help="JSONL results (default: <output-dir>/bulk_results.jsonl)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.input_dir and not args.input_list:
        build_parser().error("give --input-dir and/or --input-list")

    scenes = discover_scenes(args.input_dir, args.input_list)
    if not scenes:
        print("no scenes found", file=sys.stderr)
        return 1

    unknown = [scene for scene in scenes if scene_family(scene) == "unknown"]
    args.workers = resolve_workers(
        args.workers,
        threads_per_worker=args.threads_per_worker,
        mem_per_worker_gb=args.mem_per_worker_gb,
    )

    if args.dry_run:
        todo = [scene for scene in scenes if args.force or not scene_is_done(args.output_dir, scene)]
        print(f"{len(scenes)} scene(s): {len(todo)} to process, {len(scenes) - len(todo)} already done")
        print(f"{args.workers} worker(s) x {args.threads_per_worker} GDAL thread(s), preset {args.preset}")
        if unknown:
            print(f"{len(unknown)} scene(s) are neither S1 nor S2 and would fail, e.g. {scene_name(unknown[0])}")
        for scene in todo[:20]:
            print(f"  {scene_family(scene)} {scene_name(scene)} -> {scene_output_dir(args.output_dir, scene)}")
        if len(todo) > 20:
            print(f"  ... and {len(todo) - 20} more")
        return 0

    summary = run_bulk(scenes, output_dir=args.output_dir, args=args)
    print(summary.line(), file=sys.stderr)
    if summary.failed:
        log_path = args.results_log or args.output_dir / "bulk_results.jsonl"
        print(f"{summary.failed} scene(s) failed; see {log_path}", file=sys.stderr)
    return 1 if summary.failed or summary.interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
