"""Process-level plumbing shared by the S1 and S2 pipelines.

CPU budget, GDAL runtime settings, scratch directories, atomic output writes
and the per-call product fan-out. Nothing here knows about Sentinel data.
"""
from __future__ import annotations

import math
import multiprocessing
import os
import secrets
import shutil
import tempfile
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from osgeo import gdal

from .errors import PartialFailure

SERIAL_MODES = frozenset({"none", "off", "serial"})


# --------------------------------------------------------------------------- #
# CPU budget
# --------------------------------------------------------------------------- #
def _cgroup_cpu_limit(
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    proc_cgroup: Path = Path("/proc/self/cgroup"),
) -> int | None:
    """Return the CPU quota (rounded up) of this process's cgroup, or ``None``.

    ``docker --cpus`` and Kubernetes CPU limits set a CFS quota but leave the
    affinity mask alone, so ``sched_getaffinity`` alone overcounts.
    """
    limits: list[int] = []

    def add(quota: float, period: float) -> None:
        if quota > 0 and period > 0:
            limits.append(max(1, math.ceil(quota / period)))

    # cgroup v2: "<quota|max> <period>" in cpu.max, on this cgroup or any ancestor.
    relative = ""
    try:
        for line in proc_cgroup.read_text().splitlines():
            if line.startswith("0::"):
                relative = line[3:].strip().strip("/")
    except OSError:
        pass
    parts = [part for part in relative.split("/") if part]
    for depth in range(len(parts), -1, -1):
        try:
            quota, period = (cgroup_root.joinpath(*parts[:depth]) / "cpu.max").read_text().split()[:2]
        except (OSError, ValueError):
            continue
        if quota != "max":
            add(float(quota), float(period))

    # cgroup v1, as mounted inside a container.
    for controller in ("cpu", "cpu,cpuacct"):
        try:
            quota = float((cgroup_root / controller / "cpu.cfs_quota_us").read_text())
            period = float((cgroup_root / controller / "cpu.cfs_period_us").read_text())
        except (OSError, ValueError):
            continue
        add(quota, period)

    return min(limits) if limits else None


def available_cpus() -> int:
    """CPUs this process may actually use: affinity mask and cgroup quota, not the host total."""
    if hasattr(os, "process_cpu_count"):  # Python 3.13+
        count = os.process_cpu_count()
    elif hasattr(os, "sched_getaffinity"):
        count = len(os.sched_getaffinity(0))
    else:  # pragma: no cover - macOS/Windows
        count = os.cpu_count()
    count = max(1, count or 1)
    quota = _cgroup_cpu_limit()
    return min(count, quota) if quota else count


def in_child_process() -> bool:
    """True inside any ``multiprocessing`` child: ``Pool`` and ``ProcessPoolExecutor`` workers alike."""
    return multiprocessing.parent_process() is not None


# --------------------------------------------------------------------------- #
# GDAL runtime
# --------------------------------------------------------------------------- #
def parse_cachemax_mb(value: object) -> float | None:
    """Parse a ``gdal_cachemax_mb`` option or a ``GDAL_CACHEMAX`` value into megabytes.

    Follows GDAL's own rules: ``"25%"`` is a share of usable RAM, a number
    below 100000 is megabytes, anything larger is bytes. Invalid values give ``None``.
    """
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        if text.endswith("%"):
            parsed = float(text[:-1]) / 100.0 * gdal.GetUsablePhysicalRAM() / 2**20
        else:
            parsed = float(text)
            if parsed >= 100000:
                parsed /= 2**20
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def resolve_work_dir(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def resolve_gdal_runtime(processing: dict[str, Any], requested_threads: object, gdal_num_threads: str) -> dict[str, Any]:
    """GDAL process settings applied while the products run (see ``pysent._runtime.gdal_runtime``)."""
    # GDAL_NUM_THREADS also drives JP2/GTiff decoding, not just the warp. Keep a
    # value the user configured unless gdal_num_threads was requested explicitly.
    user_configured = gdal.GetConfigOption("GDAL_NUM_THREADS") not in (None, "")
    num_threads = gdal_num_threads if requested_threads not in (None, "") or not user_configured else None
    requested_cache = processing.get("gdal_cachemax_mb")
    if requested_cache not in (None, ""):
        try:
            cachemax_mb = float(requested_cache)
        except (TypeError, ValueError):
            cachemax_mb = None
        cachemax_mb = cachemax_mb if cachemax_mb and cachemax_mb > 0 else None
    else:
        cachemax_mb = parse_cachemax_mb(os.environ.get("GDAL_CACHEMAX"))
    return {"num_threads": num_threads, "cachemax_mb": cachemax_mb}


@contextmanager
def gdal_runtime(*, num_threads: str | None = None, cachemax_mb: float | None = None) -> Iterator[None]:
    """Apply ``GDAL_NUM_THREADS`` and the block cache size for the duration of a call.

    Both are process-wide. ``GDAL_NUM_THREADS`` has to be: GDAL 3.8 does not
    pass a thread-local value to the I/O thread of a ``multithread=True`` warp,
    so JP2 decoding inside the warp would ignore it. The previous values are
    restored on exit. Concurrent calls in one process with different settings
    therefore interfere; give them the same settings, or use processes.
    """
    previous_threads = gdal.GetConfigOption("GDAL_NUM_THREADS")
    previous_cache = gdal.GetCacheMax()
    if num_threads is not None:
        gdal.SetConfigOption("GDAL_NUM_THREADS", str(num_threads))
    if cachemax_mb is not None:
        # SetConfigOption("GDAL_CACHEMAX") is ignored once the cache exists; SetCacheMax is not.
        gdal.SetCacheMax(int(cachemax_mb * 2**20))
    try:
        yield
    finally:
        if num_threads is not None:
            gdal.SetConfigOption("GDAL_NUM_THREADS", previous_threads)
        if cachemax_mb is not None:
            gdal.SetCacheMax(previous_cache)


@contextmanager
def gdal_errors() -> Iterator[None]:
    """Turn GDAL errors into exceptions that carry GDAL's own message.

    Without this a failed ``gdal.Warp`` just returns ``None`` and the cause
    (e.g. ``opj_get_decoded_tile() failed``) only reaches stderr.
    """
    manager = getattr(gdal, "ExceptionMgr", None)  # GDAL >= 3.7
    if manager is not None:
        with manager(useExceptions=True):
            yield
        return
    previous = gdal.GetUseExceptions()  # pragma: no cover - GDAL < 3.7
    gdal.UseExceptions()
    try:
        yield
    finally:
        if not previous:
            gdal.DontUseExceptions()


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #
@contextmanager
def scratch_dir(work_dir: Path) -> Iterator[Path]:
    """A private directory for one product's intermediates, removed on every exit path."""
    work_dir.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=".pysent-", dir=work_dir))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextmanager
def atomic_output(final_path: Path) -> Iterator[Path]:
    """Yield a temporary sibling of ``final_path``; move it into place only on success.

    A process killed mid-write leaves a hidden ``.<name>.<random>.partial.tif``
    but never a truncated file under the final name, so "skip if it exists"
    resume logic stays correct.
    """
    temporary = final_path.with_name(f".{final_path.stem}.{secrets.token_hex(4)}.partial{final_path.suffix or '.tif'}")
    try:
        yield temporary
        os.replace(temporary, final_path)
    finally:
        temporary.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Product fan-out
# --------------------------------------------------------------------------- #
def process_pool_context() -> multiprocessing.context.BaseContext:
    """``forkserver`` where available, else ``spawn``.

    Forking after GDAL or numba have started threads can deadlock the child.
    """
    method = "forkserver" if "forkserver" in multiprocessing.get_all_start_methods() else "spawn"
    return multiprocessing.get_context(method)


def _call_in_worker(func: Callable[..., dict[str, Any]], runtime: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    # A fresh worker process does not inherit the parent's GDAL settings.
    with gdal_runtime(**runtime):
        return func(**spec)


def run_product_jobs(
    func: Callable[..., dict[str, Any]],
    jobs: Sequence[tuple[str, dict[str, Any]]],
    *,
    parallel_mode: str,
    workers: int,
    runtime: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run ``func(**spec)`` for every ``(name, spec)`` job and collect the results in order.

    Every job runs even if another fails. One failed job of a single-job call
    re-raises its own exception; any failure in a multi-job call raises
    :class:`~pysent.errors.PartialFailure`.
    """
    results: dict[str, dict[str, Any]] = {}
    errors: dict[str, BaseException] = {}

    if workers <= 1 or parallel_mode in SERIAL_MODES:
        with gdal_runtime(**runtime):
            for name, spec in jobs:
                try:
                    results[name] = func(**spec)
                except Exception as exc:
                    errors[name] = exc
    else:
        futures: dict[str, Future] = {}
        if parallel_mode == "processes":
            with ProcessPoolExecutor(max_workers=workers, mp_context=process_pool_context()) as executor:
                for name, spec in jobs:
                    futures[name] = executor.submit(_call_in_worker, func, runtime, spec)
        else:
            with gdal_runtime(**runtime), ThreadPoolExecutor(max_workers=workers) as executor:
                for name, spec in jobs:
                    futures[name] = executor.submit(func, **spec)
        # Leaving the executor block waited for every job.
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as exc:
                errors[name] = exc

    if not errors:
        return [results[name] for name, _ in jobs]
    if len(jobs) == 1:
        raise next(iter(errors.values()))
    detail = "; ".join(f"{name}: {type(exc).__name__}: {exc}" for name, exc in errors.items())
    raise PartialFailure(
        f"{len(errors)} of {len(jobs)} products failed ({detail})",
        [results[name] for name, _ in jobs if name in results],
        errors,
    ) from next(iter(errors.values()))
