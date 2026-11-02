"""Measure what the committed tables cost *between* their own rows.

Run inside the generator image (``docker run --rm -v "$PWD:/work" -w /work
pysent-rt6s:1.1 python3 tools/rt6s/offgrid.py``). Nothing here is written to a
table: it prints 6SV truth at points chosen to be off the generated grid in
every coordinate, which is the only way to see the interpolation's error rather
than the fit's. The reference tuples it prints are the ones pinned in
``tests/test_atmosphere.py``.

Three questions, each of which the tables would otherwise only assert:

* **What does a 10-degree azimuth step cost?** The ``interp_error`` column in
  the aerosol table is a leave-one-out estimate, so it drops a node and
  predicts it from neighbours *20* degrees apart - an upper bound for the step
  a caller actually takes, not the step itself. ``azimuth_midpoints`` measures
  the real thing, at the midpoint, where linear interpolation is worst.
* **Does the library reproduce 6SV off-grid?** ``reference_points`` prints
  total path reflectance, transmittances and spherical albedo at points off
  the depth, zenith and azimuth axes together.
* **What does tabulating the aerosol at sea level only cost over terrain?**
  ``elevation_cost`` compares 6SV's own answer at altitude against the
  library's combination - molecular at the pixel's pressure plus the aerosol
  increment measured at sea level - which is the approximation the plan's open
  question is about.
"""
from __future__ import annotations

import argparse
import multiprocessing
import sys

import numpy as np

from generate_tables import (
    AEROSOL_AZIMUTHS,
    model,
    path_reflectance,
    total_path_reflectance,
)

# Deliberately between the stored nodes on every axis: depths sit between 0.05,
# 0.15 and 0.4, zeniths between the table's own, azimuths on the midpoints of
# the 10-degree grid. Band 17 is included because it carries the sharpest phase
# function of the four, so it is where interpolation is hardest.
REFERENCE_CASES = (
    # band, model, depth, solar zenith, view zenith, relative azimuth
    (4, "Continental", 0.08, 62.0, 28.0, 35.0),
    (6, "Maritime", 0.22, 71.5, 44.0, 95.0),
    (8, "Continental", 0.12, 45.0, 12.0, 155.0),
    (17, "Maritime", 0.09, 77.5, 52.0, 25.0),
)

# The archive's own envelope, measured in the audit: the loading runs near 0.03
# and reaches 0.15, the solar zenith 70 to 79, the view zenith out to 56. The
# midpoint sweep covers it and one step beyond, so the number quoted for the
# tables is the one a caller meets rather than the table's worst corner.
SWEEP_BANDS = (4, 6, 8, 17)
SWEEP_MODELS = ("Continental", "Maritime")
SWEEP_DEPTHS = (0.05, 0.15, 0.4)
SWEEP_SOLAR_ZENITHS = (40.0, 70.0, 79.0, 85.0)
SWEEP_VIEW_ZENITHS = (16.0, 40.0, 56.0, 60.0)
ELEVATIONS_KM = (1.0, 2.0, 3.0)


def _total(platform, band, model_name, depth, sza, vza, azimuth, altitude_km=None):
    out = model(
        platform, band, sza, vza, 0.0, 0.0,
        relative_azimuth=azimuth, rayleigh=True,
        aerosol_model=model_name, aerosol_depth=depth,
        altitude_km=altitude_km,
    )
    return (
        total_path_reflectance(out),
        out.transmittance_total_scattering.downward,
        out.transmittance_total_scattering.upward,
        out.spherical_albedo.total,
    )


def _molecular(platform, band, sza, vza, azimuth, altitude_km=None):
    out = model(
        platform, band, sza, vza, 0.0, 0.0,
        relative_azimuth=azimuth, rayleigh=True, altitude_km=altitude_km,
    )
    # No aerosol, so 6SV does not split the columns and either reading agrees.
    return (
        path_reflectance(out),
        out.transmittance_total_scattering.downward,
        out.transmittance_total_scattering.upward,
        out.spherical_albedo.total,
    )


def reference_job(case):
    band, model_name, depth, sza, vza, azimuth = case
    return case, _total("S3A", band, model_name, depth, sza, vza, azimuth)


def reference_points(pool):
    """6SV truth at points off every axis, for the library to be held to."""
    print("\n# off-grid aerosol reference points, for tests/test_atmosphere.py")
    print("# band, model, depth_550, sza, vza, azimuth, path, down, up, albedo")
    for case, (path, down, up, albedo) in pool.map(reference_job, REFERENCE_CASES):
        band, model_name, depth, sza, vza, azimuth = case
        print(
            f'    ({band}, "{model_name}", {depth}, {sza}, {vza}, {azimuth}, '
            f"{path:.5f}, {down:.5f}, {up:.5f}, {albedo:.5f}),"
        )


def midpoint_job(job):
    """The real cost of one azimuth step: truth at the midpoint, less the mean.

    Linear interpolation between two stored samples gives exactly their mean at
    the midpoint, and the midpoint is where it is furthest from the curve, so
    this is the step's worst error at that geometry - no leave-one-out
    pessimism, no fitted form.
    """
    band, model_name, depth, sza, vza = job
    nodes = [
        _total("S3A", band, model_name, depth, sza, vza, azimuth)[0]
        for azimuth in AEROSOL_AZIMUTHS
    ]
    worst, worst_at = 0.0, None
    for index, (lower, upper) in enumerate(zip(AEROSOL_AZIMUTHS, AEROSOL_AZIMUTHS[1:])):
        middle = 0.5 * (lower + upper)
        interpolated = 0.5 * (nodes[index] + nodes[index + 1])
        truth = _total("S3A", band, model_name, depth, sza, vza, middle)[0]
        if abs(truth - interpolated) > worst:
            worst, worst_at = abs(truth - interpolated), middle
    return job, worst, worst_at


def azimuth_midpoints(pool):
    """What the 10-degree azimuth step costs, measured where it is worst."""
    jobs = [
        (band, model_name, depth, sza, vza)
        for band in SWEEP_BANDS
        for model_name in SWEEP_MODELS
        for depth in SWEEP_DEPTHS
        for sza in SWEEP_SOLAR_ZENITHS
        for vza in SWEEP_VIEW_ZENITHS
    ]
    results = pool.map(midpoint_job, jobs)
    print("\n# azimuth midpoint error, by geometry (worst over the 18 steps)")
    print(f"{'depth':>6s} {'sza':>5s} {'vza':>5s} {'worst':>10s} {'at':>5s} {'band':>5s}")
    by_envelope = []
    for depth in SWEEP_DEPTHS:
        for sza in SWEEP_SOLAR_ZENITHS:
            for vza in SWEEP_VIEW_ZENITHS:
                picked = [
                    (worst, at, job[0])
                    for job, worst, at in results
                    if job[2] == depth and job[3] == sza and job[4] == vza
                ]
                worst, at, band = max(picked)
                print(f"{depth:6.2f} {sza:5.0f} {vza:5.0f} {worst:10.2e} {at:5.0f} {band:5d}")
                if depth <= 0.15 and sza <= 79.0 and vza <= 56.0:
                    by_envelope.append(worst)
    overall = [worst for _, worst, _ in results]
    print(f"\nworst anywhere in the sweep      {max(overall):.2e}")
    print(f"worst inside the archive envelope {max(by_envelope):.2e}  "
          f"(depth <= 0.15, sza <= 79, vza <= 56)")
    print(f"p99 inside the envelope           {np.percentile(by_envelope, 99):.2e}")


def elevation_job(job):
    """6SV at altitude against molecular-at-altitude plus a sea-level increment."""
    band, model_name, depth, sza, vza, azimuth, altitude = job
    truth = _total("S3A", band, model_name, depth, sza, vza, azimuth, altitude_km=altitude)[0]
    molecular_up = _molecular("S3A", band, sza, vza, azimuth, altitude_km=altitude)[0]
    hazy_sea = _total("S3A", band, model_name, depth, sza, vza, azimuth)[0]
    molecular_sea = _molecular("S3A", band, sza, vza, azimuth)[0]
    combined = molecular_up + (hazy_sea - molecular_sea)
    return job, truth, combined


def elevation_cost(pool):
    """What tabulating the aerosol at sea level only costs over terrain.

    The library adds the sea-level aerosol increment to the molecular term at
    the pixel's own pressure. That is right only to the extent that the air
    column above the aerosol does not change its scattering, and this is the
    measurement of how wrong it is - the open question about elevated terrain
    stated as a number instead of a caveat.
    """
    jobs = [
        (band, model_name, depth, 70.0, 40.0, 90.0, altitude)
        for band in (4, 17)
        for model_name in SWEEP_MODELS
        for depth in (0.05, 0.15, 0.4)
        for altitude in ELEVATIONS_KM
    ]
    print("\n# cost of the sea-level-only aerosol table over terrain (sza 70, vza 40, raa 90)")
    print(f"{'band':>5s} {'model':>12s} {'depth':>6s} {'km':>4s} "
          f"{'6SV':>9s} {'combined':>9s} {'error':>10s}")
    worst = 0.0
    for job, truth, combined in pool.map(elevation_job, jobs):
        band, model_name, depth, _, _, _, altitude = job
        print(f"{band:5d} {model_name:>12s} {depth:6.2f} {altitude:4.1f} "
              f"{truth:9.5f} {combined:9.5f} {truth - combined:10.2e}")
        worst = max(worst, abs(truth - combined))
    print(f"\nworst over terrain to {max(ELEVATIONS_KM):.0f} km: {worst:.2e}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=max(1, multiprocessing.cpu_count() // 2))
    parser.add_argument(
        "--what", nargs="+", default=["reference", "azimuth", "elevation"],
        choices=["reference", "azimuth", "elevation"],
    )
    args = parser.parse_args()
    with multiprocessing.Pool(args.jobs) as pool:
        if "reference" in args.what:
            reference_points(pool)
        if "azimuth" in args.what:
            azimuth_midpoints(pool)
        if "elevation" in args.what:
            elevation_cost(pool)
    return 0


if __name__ == "__main__":
    sys.exit(main())
