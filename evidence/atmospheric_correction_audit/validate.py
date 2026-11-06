"""Phase 6: the correction across the holding, against ESA, water first.

Three things separate this from the per-phase measurements that came before it.

**It runs the shipped code.** Phases 2 to 4 measured through ``sweep.py``, which
re-implements the correction in the audit. That was right while the library was
still changing shape, and it is wrong now: what a user gets is
``pysent._olci_correction.correct_frame``, so that is what has to be validated.
The comparison still happens on the instrument grid rather than through the map
warp, because both references share that grid (A1) and warping would mix the
correction's error with the resampler's.

**It reports water and land separately, and leads with water** (B2), because
that is the owner's priority. Within water it splits open water from bright
surfaces, since the Arctic frames arrived and sea ice is the surface the group
actually works on.

**It asks whether the uncertainty band is telling the truth.** Under the
owner's answer to Q5 the product ships a value on every clear pixel and
publishes an error beside it, and that band is the only thing standing between
a user and a misreading. So the headline number here is not the residual - it
is **coverage**: the share of pixels whose actual disagreement with ESA falls
within the uncertainty the product published for them. A band that covers 50 %
of pixels is not an uncertainty, it is a decoration.
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import shutil
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

from osgeo import gdal  # noqa: E402

gdal.UseExceptions()

from budget import BANDS, level2_reflectance, scratch_dir  # noqa: E402
from pairs import collect  # noqa: E402

from pysent._olci_correction import QUALITY_BITS, correct_frame  # noqa: E402
from pysent._olci_l1 import quality_mask, radiance_member, solar_zenith  # noqa: E402
from pysent._sen3 import (  # noqa: E402
    extract_member,
    find_member,
    open_sen3_variable,
    sen3_root,
)

#: Below this many pixels in a class, a median says nothing about it.
MINIMUM_PIXELS = 2_000

def reference_member(kind: str, band: int) -> tuple[str, str]:
    """Where each Level-2 keeps the quantity we compare against.

    The Synergy variable is **not** named after its member: the file is
    ``Syn_Oa08_reflectance.nc`` and the variable inside it is ``SDR_Oa08``.
    Deriving one from the other silently reads nothing.
    """
    if kind == "wfr":
        return f"Oa{band:02d}_reflectance.nc", f"Oa{band:02d}_reflectance"
    return f"Syn_Oa{band:02d}_reflectance.nc", f"SDR_Oa{band:02d}"

SOLAR_ZENITH_BINS = ((0, 68), (68, 72), (72, 76), (76, 80), (80, 90))


def frame_shape(root, scratch, decimate):
    member, variable = radiance_member(BANDS[0])
    probe = open_sen3_variable(extract_member(root, find_member(root, member), scratch), variable)
    columns, rows = probe.RasterXSize, probe.RasterYSize
    probe = None
    return (max(1, rows // decimate), max(1, columns // decimate))


def one_frame(l1_path, reference_path, kind, decimate):
    """Residuals, uncertainties and classes for one acquisition.

    ``kind`` is ``"wfr"`` (water-leaving reflectance, over water) or ``"syn"``
    (surface directional reflectance, over land). Each reference is only
    meaningful over its own surface, which is why the split is not optional.
    """
    scratch = scratch_dir("ac-validate-")
    try:
        root = sen3_root(str(l1_path))
        shape = frame_shape(root, scratch, decimate)
        zenith = solar_zenith(root, scratch, shape)
        rejected, _missing, bright = quality_mask(
            root, scratch, shape, ["invalid", "saturated"]
        )
        land, _, _ = quality_mask(root, scratch, shape, ["land"])

        out = correct_frame(
            str(l1_path), root, scratch,
            bands=list(BANDS), shape=shape, decimate=decimate,
            solar_zenith_for_toa=zenith,
            land=land, bright=bright, rejected=rejected,
            tier="full", aerosol="retrieve",
        )

        reference_root = sen3_root(str(reference_path))
        rows, skipped = [], []
        clear = ~rejected & ~bright
        classes = {
            # Water first, and bright split out: ice is the surface that
            # matters here and it is the one no reference covers well.
            "water": clear & ~land,
            "land": clear & land,
            "bright": ~rejected & bright,
        }
        for band in BANDS:
            member, variable = reference_member(kind, band)
            try:
                truth = level2_reflectance(
                    reference_root, member, variable, shape, scratch
                )
            except (ValueError, RuntimeError) as error:
                # Say so. Swallowing this is how a run reports "nothing
                # measurable" and leaves you guessing whether the data or the
                # code is at fault.
                skipped.append(f"band {band}: {type(error).__name__}: {str(error)[:60]}")
                continue
            ours = out["surfaces"][band].astype(np.float64)
            error = out["uncertainties"][band].astype(np.float64)
            residual = np.abs(ours - truth)
            for name, mask in classes.items():
                keep = mask & np.isfinite(residual)
                if int(keep.sum()) < MINIMUM_PIXELS:
                    continue
                covered = np.isfinite(error) & keep
                rows.append({
                    "band": band,
                    "class": name,
                    "pixels": int(keep.sum()),
                    "residual": float(np.median(residual[keep])),
                    # p68, because that is what the published band has to be.
                    # A *median* absolute residual covers 50 % of pixels by
                    # construction, so publishing one and calling it an
                    # uncertainty guarantees it is exceeded half the time.
                    "p68": float(np.percentile(residual[keep], 68)),
                    "p90": float(np.percentile(residual[keep], 90)),
                    "reference": float(np.median(np.abs(truth[keep]))),
                    "uncertainty": (
                        float(np.median(error[covered])) if covered.any() else float("nan")
                    ),
                    # The question Q5 makes load-bearing: does the published
                    # error actually contain the error that occurred?
                    "coverage": (
                        float(np.mean(residual[covered] <= error[covered]))
                        if covered.any() else float("nan")
                    ),
                    "has_uncertainty": float(covered.sum()) / float(keep.sum()),
                    "solar_zenith": float(np.median(zenith[keep])),
                })
        quality = out["quality"]
        return rows, {
            "skipped": skipped,
            "classes": {name: int((mask).sum()) for name, mask in classes.items()},
            "bright_fraction": float(np.mean(bright[~rejected])) if (~rejected).any() else 0.0,
            "retrieved_fraction": float(np.mean(
                (quality & (1 << QUALITY_BITS["aerosol_retrieved"])) != 0
            )),
            "solar_zenith": float(np.nanmedian(zenith)),
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def summarise(rows, by_zenith=False):
    buckets = collections.defaultdict(list)
    for row in rows:
        if by_zenith:
            low, high = next(
                (lo, hi) for lo, hi in SOLAR_ZENITH_BINS
                if lo <= row["solar_zenith"] < hi
            )
            key = (row["class"], f"{low}-{high}", row["band"])
        else:
            key = (row["class"], "all", row["band"])
        buckets[key].append(row)
    return buckets


def report(buckets, title):
    print(f"\n{title}")
    header = (f"{'surface':<8} {'sza':>7} {'band':>5} {'frames':>7} {'residual':>9} "
              f"{'p68':>8} {'p90':>8} {'ESA':>8} {'stated':>8} {'covered':>8}")
    print(header)
    print("-" * len(header))
    order = {"water": 0, "bright": 1, "land": 2}
    for key in sorted(buckets, key=lambda k: (order.get(k[0], 9), k[1], -k[2])):
        rows = buckets[key]
        surface, zenith, band = key
        label = {8: "665", 6: "560", 4: "490"}[band]
        print(
            f"{surface:<8} {zenith:>7} {label:>5} {len(rows):>7} "
            f"{np.median([r['residual'] for r in rows]):9.4f} "
            f"{np.median([r['p68'] for r in rows]):8.4f} "
            f"{np.median([r['p90'] for r in rows]):8.4f} "
            f"{np.median([r['reference'] for r in rows]):8.4f} "
            f"{np.nanmedian([r['uncertainty'] for r in rows]):8.4f} "
            f"{np.nanmedian([r['coverage'] for r in rows]):7.1%}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", nargs="+", default=["S3A"])
    parser.add_argument("--months", nargs="+", default=["2026/10", "2026/11"])
    parser.add_argument("--decimate", type=int, default=8)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--arctic", default="", help="a directory of fetched Arctic scenes")
    arguments = parser.parse_args()

    jobs = []
    for platform in arguments.platform:
        found = collect(platform, arguments.months)
        for key in sorted(found):
            entry = found[key]
            if "l1" not in entry:
                continue
            for kind in ("wfr", "syn"):
                if kind in entry:
                    jobs.append((entry["l1"], entry[kind], kind))
    if arguments.arctic:
        directory = pathlib.Path(arguments.arctic)
        l1s = {p.name[16:31]: p for p in directory.glob("*OL_1_EFR*.zip")}
        for stamp, wfr in ((p.name[16:31], p) for p in directory.glob("*OL_2_WFR*.zip")):
            if stamp in l1s:
                jobs.append((l1s[stamp], wfr, "wfr"))
    jobs = jobs[: arguments.count]
    if not jobs:
        print("no matched acquisitions")
        return 1

    print(f"{len(jobs)} comparison(s), decimate {arguments.decimate}, "
          f"through the shipped correct_frame()\n")
    rows, failures = [], 0
    for index, (l1, reference, kind) in enumerate(jobs, 1):
        name = pathlib.Path(l1).name[16:31]
        try:
            got, summary = one_frame(l1, reference, kind, arguments.decimate)
        except Exception as error:                        # noqa: BLE001
            print(f"  {index:>3}/{len(jobs)} {name} {kind}: "
                  f"{type(error).__name__}: {str(error)[:60]}")
            failures += 1
            continue
        rows.extend(got)
        note = ""
        if not got:
            note = ("  <- " + (summary["skipped"][0] if summary["skipped"]
                    else f"too few pixels: {summary['classes']}"))
        print(f"  {index:>3}/{len(jobs)} {name} {kind}: {len(got):>2} class-bands, "
              f"bright {summary['bright_fraction']:5.1%}, "
              f"retrieved {summary['retrieved_fraction']:5.1%}, "
              f"sza {summary['solar_zenith']:.1f}{note}")
    if not rows:
        print("\nnothing measurable")
        return 1

    report(summarise(rows), "Across the holding - water first (B2)")
    report(summarise(rows, by_zenith=True), "By solar zenith")
    print(f"\n{len(rows)} class-band measurements from {len(jobs) - failures} "
          f"comparison(s); {failures} failed")
    print("\n'covered' is the share of pixels whose actual disagreement with ESA fell")
    print("within the uncertainty the product published for them. Under Q5 that is the")
    print("number the product's honesty rests on, not the residual.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
