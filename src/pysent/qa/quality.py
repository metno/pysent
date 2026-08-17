"""Shared helpers for the Sentinel GeoTIFF quality-evaluation notebooks.

These utilities keep the S1/S2 notebooks thin and let both share:

* ``Bench`` - a tiny step timer that also records peak RSS and output file size,
  so every processing stage produces a comparable benchmark row.
* ``resolve_safe_archive_from_uuid`` - resolve a CSW UUID (identifier) to the
  local ``.SAFE`` / ``.zip`` archive path (re-exported from :mod:`pysent.archive`,
  which is also what the ingestion service uses).
* small raster inspection / plotting helpers used to eyeball quality.

The actual raster maths (warp, percentile stretch) are NOT reimplemented here -
the notebooks import them from :mod:`pysent.s1` / :mod:`pysent.s2`, so what you
benchmark is exactly the code the ingestion pipeline runs.

Needs the ``qa`` extra for the tabular/plotting helpers: ``pip install pysent[qa]``.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import tracemalloc
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import rasterio

__all__ = [
    # benchmarking
    "Bench",
    # product resolution
    "DEFAULT_ARCHIVE_MARKER",
    "SafeResolution",
    "resolve_safe_archive_from_uuid",
    "detect_platform",
    "list_s1_polarizations",
    # stretch variants under evaluation
    "stretch_rgb_minmax",
    "stretch_s1_grayscale_db",
    # inspection
    "raster_stats",
    "stretch_clip_report",
    # plotting
    "show_before_after",
    "plot_histograms",
    "show_profiles",
]


# --------------------------------------------------------------------------- #
# Benchmarking
# --------------------------------------------------------------------------- #
def _read_process_rss_bytes() -> int | None:
    """Current resident set size (RSS) of this process, in bytes, or None.

    Reads ``/proc/self/statm`` on Linux (cheap, no deps); falls back to
    ``resource.getrusage`` peak elsewhere. RSS includes GDAL's C-side
    allocations, which Python's ``tracemalloc`` cannot see.
    """
    try:
        with open("/proc/self/statm") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        try:
            import resource

            maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # ru_maxrss is kilobytes on Linux, bytes on macOS.
            return maxrss if sys.platform == "darwin" else maxrss * 1024
        except Exception:
            return None


class _RSSSampler:
    """Background poller that tracks the peak process RSS while a step runs.

    A high-water mark sampled on a thread catches transient spikes (e.g. GDAL's
    warp buffers) that a simple before/after delta would miss. Process-wide, so
    it assumes one timed step runs at a time (the notebooks run serially).
    """

    def __init__(self, interval: float = 0.05) -> None:
        self.interval = interval
        self.start_rss = _read_process_rss_bytes()
        self.peak_rss = self.start_rss or 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            rss = _read_process_rss_bytes()
            if rss and rss > self.peak_rss:
                self.peak_rss = rss

    def __enter__(self) -> "_RSSSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        rss = _read_process_rss_bytes()
        if rss and rss > self.peak_rss:
            self.peak_rss = rss



@dataclass
class Bench:
    """Collect per-step wall-clock + memory + output-size measurements.

    Memory is captured two ways, because they answer different questions:

    * ``py_peak_mb`` - peak *Python-side* allocation (``tracemalloc``). Accurate
      for ``read``/``stretch`` (numpy arrays), but blind to GDAL's C library, so
      it under-reports the ``warp_raw`` step.
    * ``rss_peak_mb`` / ``rss_delta_mb`` - peak *process* resident memory during
      the step, and the rise over the step, sampled on a background thread. This
      includes GDAL's C-side buffers, so it reflects what the warp actually costs.

    Usage::

        bench = Bench()
        with bench("warp", product="VV"):
            ...do work...
        bench.to_dataframe()
    """

    rows: list[dict[str, Any]] = field(default_factory=list)

    @contextmanager
    def __call__(self, step: str, **tags: Any) -> Iterator[dict[str, Any]]:
        row: dict[str, Any] = {"step": step, **tags}
        tracemalloc.start()
        sampler = _RSSSampler()
        start = time.perf_counter()
        with sampler:
            try:
                yield row
            finally:
                elapsed = time.perf_counter() - start
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        row["seconds"] = round(elapsed, 4)
        row["py_peak_mb"] = round(peak / (1024 * 1024), 1)
        if sampler.peak_rss:
            row["rss_peak_mb"] = round(sampler.peak_rss / (1024 * 1024), 1)
            if sampler.start_rss is not None:
                row["rss_delta_mb"] = round((sampler.peak_rss - sampler.start_rss) / (1024 * 1024), 1)
        # ``out_path`` may be filled in by the caller inside the block.
        out_path = row.get("out_path")
        if out_path and Path(out_path).exists():
            row["out_size_mb"] = round(Path(out_path).stat().st_size / (1024 * 1024), 2)
        self.rows.append(row)

    def to_dataframe(self):
        import pandas as pd

        df = pd.DataFrame(self.rows)
        preferred = ("product", "step", "seconds", "rss_peak_mb", "rss_delta_mb", "py_peak_mb", "out_size_mb", "out_path")
        cols = [c for c in preferred if c in df.columns]
        other = [c for c in df.columns if c not in cols]
        return df[cols + other]

    def summary(self):
        """Total wall-clock per product plus a grand total."""
        import pandas as pd

        df = self.to_dataframe()
        if "product" not in df.columns:
            return pd.DataFrame({"seconds": [df["seconds"].sum()]}, index=["total"])
        per = df.groupby("product")["seconds"].sum()
        per.loc["TOTAL"] = per.sum()
        return per.round(3).to_frame("seconds")


# --------------------------------------------------------------------------- #
# UUID -> local SAFE archive resolution
# --------------------------------------------------------------------------- #
# Re-exported from pysent.archive so the notebooks keep importing these from
# `squ`, while the service and the harness share one implementation.
from ..archive import (  # noqa: E402  (kept next to its section for readability)
    DEFAULT_ARCHIVE_MARKER,
    SafeResolution,
    extract_download_url as _extract_download_url,
    map_archive_download_to_local as _map_nbs_archive_download_to_local,
    resolve_safe_archive_from_uuid,
)


def detect_platform(*hints: object) -> str:
    """Return ``"S1"`` or ``"S2"`` by sniffing the product name / path / URL.

    Wraps :func:`pysent.profiles.detect_nbs_sentinel_platform` (the same logic the
    ingestion code uses) and feeds it both the basename and the full string of
    every hint, so it works on a SAFE path, a download URL, or a title alike
    (e.g. ``"S2A_MSIL1C_..."`` -> ``"S2"``). Raises ``ValueError`` if neither
    family is recognised - set the platform manually in that case.
    """
    from ..profiles import detect_nbs_sentinel_platform

    parts: list[str] = []
    for hint in hints:
        if not hint:
            continue
        text = str(hint)
        parts.append(Path(text).name)
        parts.append(text)
    family = detect_nbs_sentinel_platform(*parts)["family"]
    if family not in ("S1", "S2"):
        raise ValueError(
            f"Could not detect a Sentinel platform (S1/S2) from {hints!r}. "
            "Set PLATFORM = 'S1' or 'S2' manually."
        )
    return family


# Polarisation mode token (in the SAFE filename, e.g. ``..._1SDV_...``) ->
# the amplitude polarisations present. S/D = single/dual; V/H = primary channel.
_S1_POLARIZATION_MODES: dict[str, tuple[str, ...]] = {
    "SDV": ("VV", "VH"),
    "SDH": ("HH", "HV"),
    "SSV": ("VV",),
    "SSH": ("HH",),
}


def list_s1_polarizations(safe_input: str | Path) -> list[str]:
    """Amplitude variables actually present in an S1 SAFE product.

    S1 products differ in polarisation: an IW SDV product has **VV + VH**, an
    EW SDH product has **HH + HV**, single-pol products have one band. Hardcoding
    VV/VH (as the ingestion default does) fails on HH/HV products, so detect what
    is really there:

    1. read the per-band ``POLARIZATION`` metadata from the SAFE manifest (the
       same field the warp matches on), else
    2. fall back to the mode token in the filename (``..._1SDH_...`` -> HH/HV).

    Returns e.g. ``["Amplitude_VV", "Amplitude_VH"]``; raises if neither source
    yields a polarisation.
    """
    import re

    from ..s1 import detect_sentinel_s1_polarizations

    # Primary: read the SAFE manifest POLARIZATION metadata (the production
    # routine - the same one the warp matches on).
    try:
        variables = detect_sentinel_s1_polarizations(str(safe_input))
    except Exception:
        variables = []

    # Fallback: parse the polarisation mode token from the filename, e.g.
    # S1A_EW_GRDM_1SDH_... -> SDH -> Amplitude_HH/HV (useful when metadata is
    # unavailable or the manifest can't be opened offline).
    if not variables:
        match = re.search(r"_1(S[SD][VH])_", Path(str(safe_input)).name.upper())
        if match:
            variables = [f"Amplitude_{pol}" for pol in _S1_POLARIZATION_MODES.get(match.group(1), ())]

    if not variables:
        raise RuntimeError(
            f"Could not determine S1 polarisations for {safe_input!r}. "
            "Set VARIABLES manually, e.g. ['Amplitude_HH', 'Amplitude_HV']."
        )
    return variables


# --------------------------------------------------------------------------- #
# Stretch variants for tuning experiments (notebook A/B levers)
# --------------------------------------------------------------------------- #
def stretch_rgb_minmax(
    data: np.ndarray,
    *,
    nodata: float | None = 0.0,
) -> tuple[np.ndarray, dict[str, list[float]]]:
    """Per-band MIN/MAX stretch to 8-bit RGB - mirrors the *active production* S2
    writer (GDAL auto min/max). Outlier-sensitive (clouds/sunglint blow out the
    scene); provided so you can A/B it against ``stretch_sentinel_s2_rgb``
    (percentile) and quantify the difference via the stretch-quality report.

    Same signature/return as ``stretch_sentinel_s2_rgb``.
    """
    valid = np.all(np.isfinite(data), axis=0)
    if nodata is not None:
        valid &= np.any(data != float(nodata), axis=0)
    else:
        valid &= np.any(data != 0, axis=0)

    stretched = np.zeros(data.shape, dtype=np.uint8)
    stats: dict[str, list[float]] = {"p_low": [], "p_high": []}
    if not np.any(valid):
        return stretched, stats

    for band_index in range(data.shape[0]):
        band = data[band_index]
        band_valid = np.isfinite(band)
        if nodata is not None:
            band_valid &= band != float(nodata)
        values = band[band_valid].astype(np.float32, copy=False)
        if values.size == 0:
            stats["p_low"].append(0.0)
            stats["p_high"].append(1.0)
            continue
        low = float(values.min())
        high = float(values.max())
        if high <= low:
            high = low + 1.0
        scaled = np.clip((band.astype(np.float32, copy=False) - low) / (high - low), 0.0, 1.0)
        stretched[band_index][valid] = np.round(scaled[valid] * 255.0).astype(np.uint8)
        stats["p_low"].append(low)
        stats["p_high"].append(high)
    return stretched, stats


def stretch_s1_grayscale_db(
    data: np.ndarray,
    *,
    nodata: float | None = 0.0,
    percentiles: tuple[float, float] = (2.0, 98.0),
) -> tuple[np.ndarray, np.ndarray, dict[str, float]]:
    """dB-domain percentile stretch for S1 amplitude -> gray + alpha uint8.

    SAR backscatter spans orders of magnitude, so a *linear* percentile clip
    (``stretch_sentinel_s1_grayscale``) crushes most of the scene into a narrow
    band. Converting to dB (``20*log10(amplitude)``) before the clip uses the
    0-255 range far better. Same return shape as the linear routine, with
    ``p_low``/``p_high`` reported in dB and ``unit="dB"``.
    """
    low_pct, high_pct = percentiles
    amplitude = data.astype(np.float32, copy=False)
    valid = np.isfinite(amplitude) & (amplitude > 0)
    if nodata is not None and np.isfinite(float(nodata)):
        valid &= amplitude != float(nodata)

    gray = np.zeros(amplitude.shape, dtype=np.uint8)
    alpha = np.zeros(amplitude.shape, dtype=np.uint8)
    if not np.any(valid):
        return gray, alpha, {"min": 0.0, "max": 0.0, "p_low": 0.0, "p_high": 0.0, "unit": "dB"}

    decibels = np.full(amplitude.shape, np.nan, dtype=np.float32)
    decibels[valid] = 20.0 * np.log10(amplitude[valid])
    valid_db = decibels[valid]
    p_low, p_high = np.percentile(valid_db, [low_pct, high_pct])
    if not np.isfinite(p_low):
        p_low = float(np.min(valid_db))
    if not np.isfinite(p_high):
        p_high = float(np.max(valid_db))
    if p_high <= p_low:
        p_high = p_low + 1.0

    scaled = np.clip((decibels - float(p_low)) / float(p_high - p_low), 0.0, 1.0)
    gray[valid] = np.round(scaled[valid] * 255.0).astype(np.uint8)
    alpha[valid] = 255
    return gray, alpha, {
        "min": float(np.min(valid_db)),
        "max": float(np.max(valid_db)),
        "p_low": float(p_low),
        "p_high": float(p_high),
        "unit": "dB",
    }


# --------------------------------------------------------------------------- #
# Raster inspection / plotting
# --------------------------------------------------------------------------- #
def raster_stats(path: str | Path, band: int = 1, *, sample: int = 4_000_000) -> dict[str, Any]:
    """Quick valid-pixel statistics for one band, ignoring nodata / zeros."""
    with rasterio.open(path) as src:
        data = src.read(band).astype(np.float64, copy=False)
        nodata = src.nodata
        info = {
            "dtype": str(src.dtypes[band - 1]),
            "width": src.width,
            "height": src.height,
            "count": src.count,
            "crs": str(src.crs),
            "res": tuple(round(v, 3) for v in src.res),
            "overviews": src.overviews(band),
        }
    valid = np.isfinite(data)
    if nodata is not None:
        valid &= data != nodata
    valid &= data > 0
    vals = data[valid]
    if vals.size > sample:
        vals = np.random.default_rng(0).choice(vals, size=sample, replace=False)
    if vals.size:
        info.update(
            valid_frac=round(float(valid.mean()), 4),
            min=round(float(vals.min()), 4),
            max=round(float(vals.max()), 4),
            mean=round(float(vals.mean()), 4),
            p2=round(float(np.percentile(vals, 2)), 4),
            p98=round(float(np.percentile(vals, 98)), 4),
        )
    return info


def _resolve_display_percentiles(raw_display: object) -> tuple[tuple[float, float] | None, str]:
    """Map a ``raw_display`` argument to (percentiles | None, label).

    ``None`` percentiles means a min/max stretch (no clipping) - the neutral
    reference you want when judging whether the *final* stretch over-clips.
    """
    if raw_display in (None, "minmax", "min-max", "raw"):
        return None, "min/max, unclipped"
    if raw_display in ("p2_98", "percentile", "2_98"):
        return (2.0, 98.0), "2-98% display stretch"
    lo, hi = float(raw_display[0]), float(raw_display[1])
    return (lo, hi), f"{lo:g}-{hi:g}% display stretch"


def _normalize_for_display(band: np.ndarray, pct: tuple[float, float] | None) -> tuple[np.ndarray, float, float]:
    """Scale one band to 0..1 using ``pct`` percentiles, or min/max when ``pct`` is None."""
    valid = band > 0
    if not valid.any():
        return np.zeros_like(band, dtype=np.float32), 0.0, 1.0
    if pct is None:
        lo, hi = float(band[valid].min()), float(band[valid].max())
    else:
        lo, hi = (float(v) for v in np.percentile(band[valid], list(pct)))
    if hi <= lo:
        hi = lo + 1.0
    return np.clip((band.astype(np.float32) - lo) / (hi - lo), 0.0, 1.0), lo, hi


def _read_rgb_for_display(
    path: str | Path,
    bands=(1, 2, 3),
    *,
    pct: tuple[float, float] | None = None,
) -> np.ndarray:
    """Read a 3-band raster as an (H, W, 3) float array normalised to 0..1.

    ``pct=None`` -> min/max (full dynamic range, no clipping). Pass e.g.
    ``pct=(2, 98)`` for a light percentile display stretch instead.
    """
    with rasterio.open(path) as src:
        arr = src.read(list(bands)).astype(np.float32)
    out = np.zeros((arr.shape[1], arr.shape[2], 3), dtype=np.float32)
    for i in range(3):
        out[..., i], _, _ = _normalize_for_display(arr[i], pct)
    return out


def stretch_clip_report(
    raw_path: str | Path,
    final_path: str | Path,
    *,
    mode: str = "rgb",
    band_labels: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Per-band over- AND under-stretch metrics for a stretched output.

    Two complementary signals you read *together* while tuning ``STRETCH_PERCENTILES``:

    * **Over-stretch** - ``clip_low_%`` / ``clip_high_%`` / ``clip_total_%``:
      fraction of *valid* pixels driven to pure black (0) / white (255). These
      climb as you narrow the percentiles. A few percent each side is healthy
      contrast; double-digit ``clip_total_%`` means shadows are crushed /
      highlights blown and detail is lost.
    * **Under-stretch** - ``range_used_%``: how much of the 0-255 range the
      *central 96%* of pixels (``out_p2``..``out_p98``) actually span. Because the
      stretch maps p_low->0 / p_high->255, the raw min/max is uninformative; this
      central span is the real "is the range being used?" number. **Low**
      (e.g. < ~60%) means wasted dynamic range - a flat / dull image.

    The sweet spot is **high ``range_used_%`` with low ``clip_total_%``**.

    Validity is taken from the S1 alpha band when present, else from raw > 0.
    Returns one dict per band: ``band, valid_px, clip_low_%, clip_high_%,
    clip_total_%, out_p2, out_p98, range_used_%`` - drop the list into
    ``pandas.DataFrame`` for a table.
    """
    with rasterio.open(raw_path) as rsrc:
        raw = rsrc.read().astype(np.float32, copy=False)
    with rasterio.open(final_path) as fsrc:
        final = fsrc.read()

    if mode == "gray":
        valid = (final[1] > 0) if final.shape[0] > 1 else (raw[0] > 0)
        bands = [("amplitude", final[0], valid)]
    else:
        labels = band_labels or [f"band{i + 1}" for i in range(min(3, final.shape[0]))]
        bands = [(labels[i], final[i], raw[i] > 0) for i in range(len(labels))]

    rows: list[dict[str, Any]] = []
    for label, values, valid in bands:
        n = int(valid.sum())
        if not n:
            rows.append({
                "band": label, "valid_px": 0,
                "clip_low_%": None, "clip_high_%": None, "clip_total_%": None,
                "out_p2": None, "out_p98": None, "range_used_%": None,
            })
            continue
        vals = values[valid]
        low = float((vals == 0).sum()) / n * 100.0
        high = float((vals == 255).sum()) / n * 100.0
        out_p2, out_p98 = (float(v) for v in np.percentile(vals, [2, 98]))
        range_used = (out_p98 - out_p2) / 255.0 * 100.0
        rows.append({
            "band": label,
            "valid_px": n,
            "clip_low_%": round(low, 2),
            "clip_high_%": round(high, 2),
            "clip_total_%": round(low + high, 2),
            "out_p2": int(round(out_p2)),
            "out_p98": int(round(out_p98)),
            "range_used_%": round(range_used, 1),
        })
    return rows


def show_before_after(
    raw_path: str | Path,
    final_path: str | Path,
    *,
    title: str = "",
    mode: str = "rgb",
    downscale: int = 4,
    raw_display: object = "minmax",
):
    """Plot the raw warped product next to the histogram-stretched output.

    ``mode='rgb'`` for S2 3-band products, ``mode='gray'`` for S1 amplitude.

    ``raw_display`` controls how the *left* (raw) panel is shown:
      * ``"minmax"`` (default) - full min/max, **no clipping**. Use this for
        stretch tuning: the stretch's effect (and any over-clipping) is then
        plainly visible against the un-stretched reference.
      * ``"p2_98"`` - a light 2-98% display stretch (the old behaviour); the two
        panels then look alike because both clip at 2-98.
      * ``(lo, hi)`` - any display percentiles.

    The right panel is the actual 8-bit GeoTIFF on disk; its title is annotated
    with the per-band clip percentages (see :func:`stretch_clip_report`).
    """
    import matplotlib.pyplot as plt

    raw_pct, raw_label = _resolve_display_percentiles(raw_display)
    clip = stretch_clip_report(raw_path, final_path, mode=("gray" if mode != "rgb" else "rgb"))
    clip_txt = " | ".join(
        f"{r['band']} {r['clip_total_%']}%" for r in clip if r.get("clip_total_%") is not None
    )

    fig, axes = plt.subplots(1, 2, figsize=(13, 6.5))
    if mode == "rgb":
        raw = _read_rgb_for_display(raw_path, pct=raw_pct)[::downscale, ::downscale]
        with rasterio.open(final_path) as src:
            fin = np.moveaxis(src.read([1, 2, 3]), 0, -1)[::downscale, ::downscale] / 255.0
        axes[0].imshow(raw)
        axes[1].imshow(fin)
    else:
        with rasterio.open(raw_path) as src:
            raw = src.read(1).astype(np.float32)
        raw_disp, _, _ = _normalize_for_display(raw, raw_pct)
        with rasterio.open(final_path) as src:
            fin = src.read(1).astype(np.float32)[::downscale, ::downscale] / 255.0
        axes[0].imshow(raw_disp[::downscale, ::downscale], cmap="gray")
        axes[1].imshow(fin, cmap="gray")

    axes[0].set_title(f"{title}\nraw warped product ({raw_label})")
    axes[1].set_title(f"{title}\nstretched GeoTIFF" + (f"\nclipped: {clip_txt}" if clip_txt else ""))
    for ax in axes:
        ax.set_xticks([])
        ax.set_yticks([])
    fig.tight_layout()
    return fig


def plot_histograms(
    raw_path,
    final_path,
    *,
    band: int = 1,
    mode: str = "gray",
    p_low: float | None = None,
    p_high: float | None = None,
):
    """Overlay the raw-band and stretched-band histograms to see the remap.

    Pass ``p_low``/``p_high`` (from the stretch ``stats``) to draw the clip
    window on the raw histogram and annotate how much of the data falls outside
    it - i.e. how much each tail is being clipped. On the stretched side, a tall
    spike at 255 (and the ``clip`` annotation) is the visual cue for
    over-stretching the highlights.
    """
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(13, 4))
    with rasterio.open(raw_path) as src:
        raw = src.read(band).astype(np.float32)
    raw = raw[raw > 0]
    axes[0].hist(raw, bins=256, color="#4477aa")
    axes[0].set_title("raw values (valid pixels)")
    axes[0].set_yscale("log")
    for cut, colour, name in ((p_low, "#ee6677", "p_low"), (p_high, "#228833", "p_high")):
        if cut is not None:
            axes[0].axvline(float(cut), color=colour, ls="--", lw=1.5, label=f"{name}={float(cut):.1f}")
    if (p_low is not None or p_high is not None) and raw.size:
        below = float((raw < float(p_low)).mean()) * 100 if p_low is not None else 0.0
        above = float((raw > float(p_high)).mean()) * 100 if p_high is not None else 0.0
        axes[0].set_xlabel(f"clipped tails: {below:.1f}% < p_low, {above:.1f}% > p_high")
        axes[0].legend()

    with rasterio.open(final_path) as src:
        fin = src.read(band).astype(np.float32)
    fin = fin[fin > 0]
    axes[1].hist(fin, bins=256, range=(0, 255), color="#ee6677")
    axes[1].set_title("stretched values (0-255) - spike at 255 = blown highlights")
    axes[1].set_yscale("log")
    fig.tight_layout()
    return fig


def show_profiles(final_paths, *, title: str = "", mode: str = "rgb", downscale: int = 4):
    """Plot the final stretched output of several profiles side by side, for one product.

    ``final_paths`` is an ordered ``{profile_name: path}`` mapping (e.g. raw /
    best-quality / fastest-results). Use it after the profile comparison to eyeball
    the quality difference next to the timing/quality table.
    """
    import matplotlib.pyplot as plt

    items = list(final_paths.items())
    n = max(1, len(items))
    fig, axes = plt.subplots(1, n, figsize=(6 * n, 6))
    if n == 1:
        axes = [axes]
    for ax, (name, path) in zip(axes, items):
        with rasterio.open(path) as src:
            if mode == "rgb":
                img = np.moveaxis(src.read([1, 2, 3]), 0, -1)[::downscale, ::downscale] / 255.0
                ax.imshow(img)
            else:
                img = src.read(1).astype(np.float32)[::downscale, ::downscale] / 255.0
                ax.imshow(img, cmap="gray")
        ax.set_title(f"{title}\n{name}")
        ax.set_xticks([])
        ax.set_yticks([])
    fig.tight_layout()
    return fig
