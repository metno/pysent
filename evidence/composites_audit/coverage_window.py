"""How much of a region a composite actually covers, against the window length.

Q1 asked whether a daily composite is the right product, and was answered
against A5's "a day fills 80 % of a Nordic box". That figure was measured at
``qa_min`` 0.5 - the default of the audit script's own flag - not at 0.75, which
is the mission's recommendation and the product default. This measures the
coverage curve at whichever threshold is asked for, over as many consecutive
days as the archive holds, so the default window can be chosen from a number.

Reads the granules from the mounted NBS archive, so it downloads nothing. Each
day is composited once through ``process_sentinel_composite``; the per-day
products are then combined arithmetically, which is exact for a plain mean
(``sum = mean x count``) and means every granule is read once rather than once
per window length.

    python3 coverage_window.py --qa-min 0.75
    python3 coverage_window.py --qa-min 0.5 --days 20261024 20261025
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from osgeo import gdal  # noqa: E402

gdal.UseExceptions()

from pysent.composite import process_sentinel_composite  # noqa: E402
from pysent.errors import EmptySceneError  # noqa: E402

ARCHIVE = "/lustre/storeA/NBS2/sentinel/production/NorwAREA/nbsArchive/S5P"
PRODUCT_DIR = "TR-L2-NO2"
# The same region as the rest of the audit: mainland Norway, its shelf and a
# slice of the Barents Sea.
REGION = {"bbox": [2.0, 57.0, 32.0, 72.0], "epsg": 4326, "res": 0.05}


def granules_for(archive: pathlib.Path, day: str) -> list[str]:
    """Every NO2 granule the archive holds for ``day`` (YYYYMMDD)."""
    directory = archive / day[:4] / day[4:6] / day[6:] / PRODUCT_DIR
    return sorted(str(path) for path in directory.glob("S5P_*NO2*.nc"))


def read_bands(path: str) -> tuple[np.ndarray, np.ndarray]:
    dataset = gdal.Open(path)
    values = dataset.GetRasterBand(1).ReadAsArray()
    counts = dataset.GetRasterBand(2).ReadAsArray()
    dataset = None
    return values, counts


def statistics(total: np.ndarray, counts: np.ndarray) -> dict[str, float]:
    filled = counts > 0
    if not filled.any():
        return {"coverage": 0.0, "mean_obs": 0.0, "max_obs": 0, "single": 0.0, "p50": float("nan")}
    mean = np.where(filled, total / np.maximum(counts, 1), np.nan)
    return {
        "coverage": 100.0 * np.count_nonzero(filled) / counts.size,
        "mean_obs": float(counts[filled].mean()),
        "max_obs": int(counts.max()),
        "single": 100.0 * float(np.mean(counts[filled] == 1)),
        "p50": float(np.nanpercentile(mean[filled], 50)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=pathlib.Path, default=pathlib.Path(ARCHIVE))
    parser.add_argument("--days", nargs="*", default=[
        "20261024", "20261025", "20261026", "20261027", "20261028", "20261029", "20261030",
    ])
    parser.add_argument("--qa-min", type=float, default=0.75)
    parser.add_argument("--res", type=float, default=REGION["res"])
    parser.add_argument("--outdir", type=pathlib.Path,
                        default=pathlib.Path("/mnt/data/pysent/composite-window"))
    args = parser.parse_args()

    region = {**REGION, "res": args.res}
    label = f"qa{int(round(args.qa_min * 100)):02d}"
    outdir = args.outdir / label
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"region {region['bbox']} at {region['res']} deg, qa_min {args.qa_min}")
    print(f"\n{'day':>10} {'granules':>9} {'used':>5} {'cover %':>8} {'obs':>5} {'max':>4} "
          f"{'single %':>9} {'time':>7}")

    per_day: list[tuple[str, np.ndarray, np.ndarray]] = []
    for day in args.days:
        granules = granules_for(args.archive, day)
        if not granules:
            print(f"{day:>10} {'-':>9}   no granules in the archive")
            continue
        started = time.time()
        try:
            result = process_sentinel_composite(
                input_datasets=granules,
                output_dir=outdir,
                region=region,
                products={"no2": {"qa_min": args.qa_min}},
                output_names={"no2": f"day_{day}_{label}.tif"},
                processing_options={"styled": False, "thumbnail": False},
            )[0]
        except EmptySceneError as exc:
            print(f"{day:>10} {len(granules):>9}     0   empty: {str(exc)[:40]}")
            continue
        values, counts = read_bands(result["path"])
        per_day.append((day, np.nan_to_num(values, nan=0.0) * counts, counts))
        statistics_of_day = statistics(per_day[-1][1], counts)
        print(f"{day:>10} {len(granules):>9} {len(result['inputs_used']):>5} "
              f"{statistics_of_day['coverage']:8.2f} {statistics_of_day['mean_obs']:5.2f} "
              f"{statistics_of_day['max_obs']:>4} {statistics_of_day['single']:9.1f} "
              f"{time.time() - started:6.1f}s")

    if not per_day:
        print("\nnothing to combine")
        return 1

    print(f"\n{'window':>7} {'days':>22} {'cover %':>8} {'obs':>5} {'max':>4} {'single %':>9} "
          f"{'p50':>10}")
    total = np.zeros_like(per_day[0][1])
    counts = np.zeros_like(per_day[0][2])
    for length, (day, day_total, day_counts) in enumerate(per_day, start=1):
        total = total + day_total
        counts = counts + day_counts
        window = statistics(total, counts)
        span = f"{per_day[0][0]}-{day}" if length > 1 else day
        print(f"{length:>7} {span:>22} {window['coverage']:8.2f} {window['mean_obs']:5.2f} "
              f"{window['max_obs']:>4} {window['single']:9.1f} {window['p50']:10.3e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
