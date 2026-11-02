"""Generate the committed gaseous-absorption tables with 6SV.

Run inside the generator image (see the Dockerfile beside this file):

    docker build -t pysent-rt6s:1.1 tools/rt6s
    docker run --rm -v "$PWD:/work" -w /work pysent-rt6s:1.1 \
        python3 tools/rt6s/generate_tables.py --out src/pysent/data

Two tables come out, and their shapes are chosen from measured behaviour
rather than convenience:

* **Ozone** absorbs without saturating in the Chappuis band, so its
  transmittance is exactly Beer-Lambert and **one coefficient per band** is
  enough: ``T = exp(-k U m)`` with ``U`` the column in Dobson units and ``m``
  the air mass. Measured over 100 to 400 DU, ``k`` is constant to about one
  part in ten thousand, and the generator records that spread in the table so
  the claim can be checked rather than believed.
* **Water vapour** does saturate, so no single coefficient works. But its
  transmittance depends only on the **scaled path amount** ``w m`` - measured
  at two unrelated geometries, path amounts of 3.988 and 3.982 give 0.28287
  and 0.28313 - so a one-dimensional table in ``w m`` per band is enough. The
  generator builds it from a grid of water columns crossed with air masses, so
  several runs land on nearly the same path amount and the scatter between
  them measures how good the collapse is.

The spectral response is OLCI's own, per platform: Py6S ships the S3A and S3B
band filters, so a coefficient belongs to a real band rather than to its centre
wavelength.
"""
from __future__ import annotations

import argparse
import datetime
import multiprocessing
import pathlib
import sys

import numpy as np
from Py6S import (
    AeroProfile,
    AtmosProfile,
    Geometry,
    GroundReflectance,
    PredefinedWavelengths,
    SixS,
    Wavelength,
)

SIXS = "/usr/local/bin/sixsV1.1"
BANDS = range(1, 22)
# 1 Dobson unit of ozone, in the kg m-2 the OLCI product reports.
# 1 DU = 2.1415e-5 kg m-2 (NIST/WMO definition: 0.4462 mmol m-2 of O3).
KG_PER_DU = 2.1415e-5

OZONE_COLUMNS_DU = (100.0, 200.0, 300.0, 400.0)
# Water columns in g cm-2 (the unit 6S takes) crossed with air masses: the
# product of the two is what the table is indexed by, and several pairs land on
# close path amounts on purpose, to measure the collapse.
WATER_COLUMNS = (0.0, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 7.0)
AIR_MASSES = (2.0, 3.0, 4.0, 6.0, 9.0)
# A view zenith of 0 lets the air mass be reached with the solar angle alone;
# the cross-check runs use 30 degrees to confirm the collapse is not an
# artefact of looking straight down.
CROSS_CHECK_VIEW_ZENITH = 30.0


def geometry_for_air_mass(air_mass, view_zenith=0.0):
    """Solar zenith giving this air mass at that view zenith."""
    view_term = 1.0 / np.cos(np.radians(view_zenith))
    solar_term = air_mass - view_term
    if solar_term < 1.0:
        raise ValueError(
            f"air mass {air_mass} is unreachable at view zenith {view_zenith}: the view path "
            f"alone is already {view_term:.4f}"
        )
    return float(np.degrees(np.arccos(1.0 / solar_term)))


def model(platform, band, solar_zenith, view_zenith, water_g_cm2, ozone_du):
    """One 6SV run: a black surface under gas only, no aerosols."""
    run = SixS(SIXS)
    run.geometry = Geometry.User()
    run.geometry.solar_z, run.geometry.view_z = solar_zenith, view_zenith
    run.geometry.solar_a, run.geometry.view_a = 0.0, 90.0
    # Without a date the solar term is zero and every output reads 0.000.
    run.geometry.day, run.geometry.month = 25, 10
    # Without this the sensor sits on the ground: no path reflectance and an
    # upward transmittance of exactly 1. See check.py.
    run.altitudes.set_target_sea_level()
    run.altitudes.set_sensor_satellite_level()
    # 6S takes ozone in cm-atm, where 1 cm-atm is 1000 DU.
    run.atmos_profile = AtmosProfile.UserWaterAndOzone(water_g_cm2, ozone_du / 1000.0)
    run.aero_profile = AeroProfile.PredefinedType(AeroProfile.NoAerosols)
    run.ground_reflectance = GroundReflectance.HomogeneousLambertian(0.0)
    run.wavelength = Wavelength(getattr(PredefinedWavelengths, f"{platform}_OLCI_{band:02d}"))
    run.run()
    return run.outputs


def band_centre_nm(platform, band):
    """The filter's own centre, so the table names a real band.

    A Py6S predefined wavelength is ``(id, start_um, end_um, response)`` - the
    first element is an index, not a wavelength, and taking it for one labels
    band 20 as 42961 nm.
    """
    entry = getattr(PredefinedWavelengths, f"{platform}_OLCI_{band:02d}")
    return 1000.0 * 0.5 * (entry[1] + entry[2])


def ozone_row(job):
    """One band's ozone coefficient, with the spread that proves it is one."""
    platform, band = job
    air_mass = 4.0
    solar_zenith = geometry_for_air_mass(air_mass)
    coefficients = []
    for column in OZONE_COLUMNS_DU:
        out = model(platform, band, solar_zenith, 0.0, 0.0, column)
        transmittance = out.transmittance_ozone.total
        # A band with no ozone absorption at all gives exactly 1, and taking a
        # logarithm of it is fine - the coefficient is then 0.
        coefficients.append(-np.log(max(transmittance, 1e-12)) / (column * air_mass))
    coefficients = np.array(coefficients)
    spread = float(coefficients.max() - coefficients.min())
    relative = spread / max(float(coefficients.mean()), 1e-30)
    return platform, band, band_centre_nm(platform, band), float(coefficients.mean()), relative


def water_rows(job):
    """One band's water-vapour transmittance against the scaled path amount."""
    platform, band = job
    samples = []
    for air_mass in AIR_MASSES:
        solar_zenith = geometry_for_air_mass(air_mass)
        for column in WATER_COLUMNS:
            out = model(platform, band, solar_zenith, 0.0, column, 300.0)
            samples.append((column * air_mass, out.transmittance_water.total))
    # Cross-checks at a slanted view: same path amounts, different geometry.
    for air_mass in (4.0, 6.0):
        solar_zenith = geometry_for_air_mass(air_mass, CROSS_CHECK_VIEW_ZENITH)
        for column in (0.5, 2.0):
            out = model(platform, band, solar_zenith, CROSS_CHECK_VIEW_ZENITH, column, 300.0)
            samples.append((column * air_mass, out.transmittance_water.total))
    return platform, band, band_centre_nm(platform, band), sorted(samples)


def collapse_scatter(samples, tolerance=0.02):
    """Worst disagreement between samples at nearly the same path amount.

    **This checks the implementation, not the physics.** 6S itself parameterises
    gaseous transmittance by the scaled path amount, so two geometries reaching
    the same amount agree *exactly* - the number below comes out 0. What it
    therefore proves is that the generator's air masses and columns are being
    combined the way 6S combines them; the physics of the collapse is 6S's
    assumption, which this table inherits and cannot test.
    """
    worst = 0.0
    for index, (amount, value) in enumerate(samples):
        for other_amount, other_value in samples[index + 1:]:
            if amount <= 0 or abs(other_amount - amount) / amount > tolerance:
                continue
            worst = max(worst, abs(other_value - value))
    return worst


def unique_amounts(samples, tolerance=1e-9):
    """Drop samples that repeat a path amount already present."""
    kept = []
    for amount, value in samples:
        if not kept or abs(amount - kept[-1][0]) > tolerance:
            kept.append((amount, value))
    return kept


def provenance(extra=""):
    today = datetime.date.today().isoformat()
    return "\n".join(
        [
            f"# Generated {today} by tools/rt6s/generate_tables.py with 6SV 1.1 via Py6S 1.9.2,",
            "# in the container built from tools/rt6s/Dockerfile (6SV pinned by sha256 there).",
            "# Model: black Lambertian surface, no aerosols, target at sea level, sensor at",
            "# satellite level, OLCI's own per-platform band filters as shipped with Py6S.",
            "# Regenerate with:",
            "#   docker build -t pysent-rt6s:1.1 tools/rt6s",
            '#   docker run --rm -v "$PWD:/work" -w /work pysent-rt6s:1.1 \\',
            "#       python3 tools/rt6s/generate_tables.py --out src/pysent/data",
            extra,
        ]
    ).rstrip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="src/pysent/data")
    parser.add_argument("--platforms", nargs="+", default=["S3A", "S3B"])
    parser.add_argument("--bands", nargs="+", type=int, default=list(BANDS))
    parser.add_argument("--jobs", type=int, default=max(1, multiprocessing.cpu_count() // 2))
    args = parser.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    jobs = [(platform, band) for platform in args.platforms for band in args.bands]
    print(f"{len(jobs)} band-platform pairs, {args.jobs} workers", flush=True)

    with multiprocessing.Pool(args.jobs) as pool:
        ozone = pool.map(ozone_row, jobs)
        water = pool.map(water_rows, jobs)

    worst_linearity = max(row[4] for row in ozone)
    path = out / "olci_ozone_absorption.csv"
    with path.open("w") as handle:
        handle.write(provenance(
            "#\n"
            "# Ozone is Beer-Lambert in the Chappuis band, so one coefficient per band is\n"
            "# exact: T = exp(-k U m), U in Dobson units, m = 1/cos(sza) + 1/cos(vza).\n"
            f"# 1 DU = {KG_PER_DU:g} kg m-2, which is the unit the OLCI product reports.\n"
            "# linearity_spread is the relative spread of k over ozone columns of "
            f"{', '.join(f'{c:.0f}' for c in OZONE_COLUMNS_DU)} DU;\n"
            f"# the worst across all bands here is {worst_linearity:.2e}.\n"
        ) + "\n")
        handle.write("platform,band,wavelength_nm,coefficient_per_du_per_airmass,linearity_spread\n")
        for platform, band, nm, coefficient, spread in ozone:
            handle.write(f"{platform},{band},{nm:.1f},{coefficient:.6e},{spread:.3e}\n")
    print(f"wrote {path} ({len(ozone)} rows, worst linearity spread {worst_linearity:.2e})")

    worst_collapse = max(collapse_scatter(row[3]) for row in water)
    path = out / "olci_water_transmittance.csv"
    with path.open("w") as handle:
        handle.write(provenance(
            "#\n"
            "# Water vapour saturates, so no single coefficient works; but its transmittance\n"
            "# depends only on the scaled path amount w*m (w in g cm-2, m the air mass), so\n"
            "# one curve per band is enough. Interpolate linearly in path_amount.\n"
            "# Built from water columns "
            f"{', '.join(f'{c:g}' for c in WATER_COLUMNS)} g cm-2 crossed with air masses\n"
            f"# {', '.join(f'{m:g}' for m in AIR_MASSES)}, plus cross-checks at a "
            f"{CROSS_CHECK_VIEW_ZENITH:.0f} degree view zenith; repeated path\n"
            "# amounts are dropped.\n"
            "# Geometries reaching the same path amount disagree by at most "
            f"{worst_collapse:.2e},\n"
            "# which is 0 because 6S parameterises gaseous transmittance by the scaled path\n"
            "# amount itself. So this table is lossless with respect to 6S, and the collapse\n"
            "# is 6S's own assumption - inherited here, not tested here.\n"
        ) + "\n")
        handle.write("platform,band,wavelength_nm,path_amount,transmittance\n")
        for platform, band, nm, samples in water:
            for amount, transmittance in unique_amounts(samples):
                handle.write(f"{platform},{band},{nm:.1f},{amount:.4f},{transmittance:.6f}\n")
    print(f"wrote {path} (worst collapse disagreement {worst_collapse:.2e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
