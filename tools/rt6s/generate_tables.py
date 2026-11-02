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

**The Rayleigh table's pressure axis is driven by altitude**, because
``set_target_pressure`` is accepted and then ignored by this 6SV build - the
Rayleigh optical depth stays at its sea-level value, the same at 1013, 850 and
700 hPa. A table generated through it has a pressure axis holding one value six
times over, which looks like a table until something asks it how scattering
varies with elevation. ``tools/rt6s/check.py`` asserts the no-op, so a future
6SV that fixes it will say so rather than silently changing the tables.
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

# The Rayleigh grid. Denser towards the horizon, where the path reflectance
# changes fastest and where this archive actually lives (every matched frame is
# at solar zenith 63 to 79 degrees).
RAYLEIGH_SOLAR_ZENITHS = (
    0.0, 10.0, 20.0, 30.0, 40.0, 45.0, 50.0, 55.0, 60.0, 65.0, 70.0, 73.0, 76.0, 79.0, 82.0, 85.0,
)
# OLCI's own across-track extent reaches about 56 degrees; 60 is headroom.
RAYLEIGH_VIEW_ZENITHS = (0.0, 8.0, 16.0, 24.0, 32.0, 40.0, 48.0, 56.0, 60.0)
# Fitted away into three Fourier terms rather than stored, see rayleigh_rows.
RAYLEIGH_AZIMUTHS = tuple(float(a) for a in range(0, 181, 15))
# The table's pressure axis is driven by **altitude**, because
# ``set_target_pressure`` is a silent no-op in this 6SV build: it leaves the
# Rayleigh optical depth at its sea-level value, identical at 1013, 850 and
# 700 hPa, while ``set_target_custom_altitude`` varies it correctly. The axis
# is still labelled in pressure, since that is what a pixel's own sea-level
# pressure and elevation give, and these altitudes are converted with the same
# barometric formula the library uses.
#
# Sea level is the top: Py6S encodes a target altitude as a negative number in
# the one field it shares with pressure, so a *negative* altitude - which is
# what a pressure above 1013.25 hPa would need - is indistinguishable from a
# positive pressure, and silently ignored. Pressures above sea-level standard
# are handled by extrapolating along the pressure axis in the library, which is
# safe because the dependence is nearly linear; see `_axis_weights`.
RAYLEIGH_ALTITUDES_KM = (0.0, 0.5, 1.0, 2.0, 3.0, 5.0)


def pressure_at_altitude(altitude_km):
    """Standard-atmosphere pressure in hPa, the library's own formula."""
    return 1013.25 * (1.0 - 2.25577e-5 * altitude_km * 1000.0) ** 5.25588
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


def model(
    platform,
    band,
    solar_zenith,
    view_zenith,
    water_g_cm2,
    ozone_du,
    *,
    relative_azimuth=90.0,
    altitude_km=None,
    rayleigh=False,
    aerosol_model=None,
    aerosol_depth=0.0,
):
    """One 6SV run: a black surface, no aerosols.

    ``rayleigh=True`` switches the gaseous absorption off entirely, so the only
    thing left in the path reflectance is molecular scattering - which is what
    the Rayleigh table has to hold, because the gas term is applied separately
    and multiplying the two corrections together would count absorption twice.
    """
    run = SixS(SIXS)
    run.geometry = Geometry.User()
    run.geometry.solar_z, run.geometry.view_z = solar_zenith, view_zenith
    # The solar azimuth is the origin, so the view azimuth *is* the relative
    # azimuth; the library looks up with the same difference, OAA minus SAA.
    run.geometry.solar_a, run.geometry.view_a = 0.0, relative_azimuth
    # Without a date the solar term is zero and every output reads 0.000.
    run.geometry.day, run.geometry.month = 25, 10
    # Without this the sensor sits on the ground: no path reflectance and an
    # upward transmittance of exactly 1. See check.py.
    # Altitude, never set_target_pressure: that call is accepted and ignored.
    if not altitude_km:
        run.altitudes.set_target_sea_level()
    elif altitude_km < 0:
        raise ValueError(
            f"a target altitude of {altitude_km} km cannot be asked for: Py6S encodes it as a "
            "positive number in the field it shares with pressure, where 6SV ignores it"
        )
    else:
        run.altitudes.set_target_custom_altitude(altitude_km)
    run.altitudes.set_sensor_satellite_level()
    if rayleigh:
        run.atmos_profile = AtmosProfile.PredefinedType(AtmosProfile.NoGaseousAbsorption)
    else:
        # 6S takes ozone in cm-atm, where 1 cm-atm is 1000 DU.
        run.atmos_profile = AtmosProfile.UserWaterAndOzone(water_g_cm2, ozone_du / 1000.0)
    if aerosol_model is None or not aerosol_depth:
        run.aero_profile = AeroProfile.PredefinedType(AeroProfile.NoAerosols)
    else:
        run.aero_profile = AeroProfile.PredefinedType(getattr(AeroProfile, aerosol_model))
        # 6S takes the loading as optical depth at 550 nm, separately from the
        # model that gives it a shape.
        run.aot550 = aerosol_depth
    run.ground_reflectance = GroundReflectance.HomogeneousLambertian(0.0)
    run.wavelength = Wavelength(getattr(PredefinedWavelengths, f"{platform}_OLCI_{band:02d}"))
    run.run()
    return run.outputs


def path_reflectance(outputs):
    """The atmosphere's own contribution at satellite level.

    Py6S exposes no accessor for it, so it comes from the first column of the
    ``reflectance I`` row of the satellite-level block.
    """
    row = next(line for line in outputs.fulltext.splitlines() if "reflectance I" in line)
    return float(row.split(":")[1].split()[0])


def total_path_reflectance(outputs):
    """The **whole** atmospheric path reflectance, molecular and aerosol.

    Not ``path_reflectance``. With an aerosol present, 6SV splits the satellite
    level reflectance across the columns of its own table: the one labelled
    ``atm. intrin. ref.`` holds only the molecular part, the next column holds
    the aerosol part, and the third is their sum. Reading the first column with
    an aerosol loaded therefore returns a number that does not move when the
    optical depth or the aerosol model changes - it stays at the Rayleigh
    value, which looks like a table axis having no effect.

    With a black Lambertian ground the apparent reflectance *is* that sum, so
    it is what the tables store. For a purely molecular run the two agree
    exactly, which is why the Rayleigh table generated from the first column is
    still correct.
    """
    return outputs.apparent_reflectance


def fit_azimuth_terms(azimuths_deg, values):
    """Three Fourier terms in the relative azimuth, and the worst fit error.

    ``rho(dphi) = a0 + 2 a1 cos(dphi) + 2 a2 cos(2 dphi)`` is the standard form
    for a Rayleigh path reflectance, and collapsing thirteen azimuths into
    three numbers is what keeps the committed table small enough to read. The
    truncation error is **measured here and stored per row**, rather than
    asserted: three terms is a claim about the atmosphere, not an identity.
    """
    radians = np.radians(np.asarray(azimuths_deg, dtype=np.float64))
    design = np.column_stack(
        [np.ones_like(radians), 2 * np.cos(radians), 2 * np.cos(2 * radians)]
    )
    coefficients, *_ = np.linalg.lstsq(design, np.asarray(values, dtype=np.float64), rcond=None)
    worst = float(np.max(np.abs(design @ coefficients - values)))
    return coefficients, worst


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


# The aerosol table. Two models rather than one, because the measured Angstrom
# exponent decides between them: over the land frame the aerosol product's own
# optical depths give about 1.18, which is fine-mode continental, while marine
# air runs nearer 0.1 to 0.5. The library interpolates between the two on the
# measured exponent rather than picking one by land mask.
AEROSOL_MODELS = ("Continental", "Maritime")
# Optical depth at 550 nm. Zero is included so the aerosol increment is defined
# against the same geometry, and 0.4 covers a hazy day; the measured loading in
# this archive is nearer 0.03, so the useful accuracy is at the bottom.
# Zero is absent on purpose: the aerosol-free case at sea level is already the
# Rayleigh table's 1013.25 hPa row, so repeating it here would be a second
# copy of the same numbers to keep in step.
AEROSOL_DEPTHS = (0.05, 0.15, 0.4)
# Seven azimuths rather than thirteen. An aerosol phase function is more
# forward-peaked than Rayleigh's, so this was measured rather than assumed:
# fitting the three Fourier terms from seven azimuths reproduces all thirteen
# to 4.5e-6, against 3.4e-6 from the full set. An aerosol run costs 0.65 s
# against 0.04 s for a clear one, so halving the count is worth 4e-6.
AEROSOL_AZIMUTHS = tuple(float(a) for a in range(0, 181, 30))


def aerosol_rows(job):
    """One band's aerosol table: total path, transmittances and albedo.

    Held at **sea level only**. The aerosol sits in the boundary layer, so the
    air column above it changes its scattering far less than it changes the
    Rayleigh term, and carrying a pressure axis here would multiply the table
    by six for a second-order effect. The library adds this increment to the
    pressure-interpolated Rayleigh value; ``check.py`` measures what that
    approximation costs.

    The aerosol optical depth at the band's own wavelength is stored with each
    row, because the Angstrom exponent the library interpolates on has to come
    from the table rather than from a remembered value for the model.
    """
    platform, model_name = job[0], job[2]
    band = job[1]
    rows = []
    for depth in AEROSOL_DEPTHS:
        for solar_zenith in RAYLEIGH_SOLAR_ZENITHS:
            for view_zenith in RAYLEIGH_VIEW_ZENITHS:
                reflectances, others = [], []
                for azimuth in AEROSOL_AZIMUTHS:
                    out = model(
                        platform, band, solar_zenith, view_zenith, 0.0, 0.0,
                        relative_azimuth=azimuth, rayleigh=True,
                        aerosol_model=model_name, aerosol_depth=depth,
                    )
                    # The total, not the molecular column - see the docstring.
                    reflectances.append(total_path_reflectance(out))
                    others.append(
                        (
                            out.transmittance_total_scattering.downward,
                            out.transmittance_total_scattering.upward,
                            out.spherical_albedo.total,
                            out.optical_depth_total.aerosol,
                        )
                    )
                coefficients, fit_error = fit_azimuth_terms(AEROSOL_AZIMUTHS, reflectances)
                down, up, albedo, band_depth = others[0]
                rows.append(
                    (platform, band, model_name, depth, solar_zenith, view_zenith,
                     *coefficients, fit_error, down, up, albedo, band_depth)
                )
    return rows


def write_aerosol(out, results):
    """The aerosol table, one row per grid point."""
    rows = [row for chunk in results for row in chunk]
    worst_fit = max(row[9] for row in rows)
    path = out / "olci_aerosol.csv"
    with path.open("w") as handle:
        handle.write(provenance(
            "#\n"
            "# Total path reflectance - molecular plus aerosol - with gaseous absorption off,\n"
            "# at sea level only, for two 6SV aerosol models against optical depth at 550 nm.\n"
            "# The azimuth dependence is three Fourier terms as in the Rayleigh table;\n"
            f"# the worst fit error here is {worst_fit:.2e}.\n"
            "# band_optical_depth is the aerosol optical depth at that band's own wavelength,\n"
            "# so the Angstrom exponent can be derived from the table rather than assumed.\n"
            "# The aerosol increment (this row minus its depth-zero row) is added to the\n"
            "# pressure-interpolated Rayleigh value; see check.py for what that costs.\n"
            "# Interpolate multilinearly in (optical depth, solar_zenith, view_zenith) and\n"
            "# between the two models on the measured Angstrom exponent.\n"
        ) + "\n")
        handle.write(
            "platform,band,model,depth_550,solar_zenith,view_zenith,a0,a1,a2,"
            "azimuth_fit_error,transmittance_down,transmittance_up,spherical_albedo,"
            "band_optical_depth\n"
        )
        for row in rows:
            platform, band, model_name, depth, sza, vza = row[:6]
            a0, a1, a2, fit, down, up, albedo, band_depth = row[6:]
            handle.write(
                f"{platform},{band},{model_name},{depth:g},{sza:g},{vza:g},"
                f"{a0:.6f},{a1:.6f},{a2:.6f},{fit:.2e},"
                f"{down:.6f},{up:.6f},{albedo:.6f},{band_depth:.6f}\n"
            )
    print(f"wrote {path} ({len(rows)} rows, worst azimuth fit {worst_fit:.2e})", flush=True)


def rayleigh_rows(job):
    """One band's Rayleigh table: path reflectance, transmittances, albedo.

    The transmittances and the spherical albedo do not depend on the relative
    azimuth, so they are taken from one of the azimuth runs - and the spread
    across the others is returned, so that independence is reported rather than
    assumed.
    """
    platform, band = job
    rows = []
    worst_azimuth_independence = 0.0
    for altitude in RAYLEIGH_ALTITUDES_KM:
        pressure = pressure_at_altitude(altitude)
        for solar_zenith in RAYLEIGH_SOLAR_ZENITHS:
            for view_zenith in RAYLEIGH_VIEW_ZENITHS:
                reflectances, transmittances = [], []
                for azimuth in RAYLEIGH_AZIMUTHS:
                    out = model(
                        platform, band, solar_zenith, view_zenith, 0.0, 0.0,
                        relative_azimuth=azimuth, altitude_km=altitude, rayleigh=True,
                    )
                    reflectances.append(path_reflectance(out))
                    transmittances.append(
                        (
                            out.transmittance_rayleigh_scattering.downward,
                            out.transmittance_rayleigh_scattering.upward,
                            out.spherical_albedo.total,
                        )
                    )
                coefficients, fit_error = fit_azimuth_terms(RAYLEIGH_AZIMUTHS, reflectances)
                spread = np.ptp(np.asarray(transmittances, dtype=np.float64), axis=0)
                worst_azimuth_independence = max(worst_azimuth_independence, float(spread.max()))
                down, up, albedo = transmittances[0]
                rows.append(
                    (platform, band, pressure, solar_zenith, view_zenith,
                     *coefficients, fit_error, down, up, albedo)
                )
    return rows, worst_azimuth_independence


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


def write_rayleigh(out, results):
    """The Rayleigh table, one row per grid point."""
    rows = [row for chunk, _ in results for row in chunk]
    worst_fit = max(row[8] for row in rows)
    worst_independence = max(spread for _, spread in results)
    path = out / "olci_rayleigh.csv"
    with path.open("w") as handle:
        handle.write(provenance(
            "#\n"
            "# Molecular scattering only: the gaseous absorption is switched off in these runs,\n"
            "# because the gas correction is applied separately and a table holding both would\n"
            "# count absorption twice.\n"
            "# The path reflectance against relative azimuth dphi (= OAA - SAA, degrees, folded\n"
            "# into 0..180) is held as three Fourier terms:\n"
            "#     rho = a0 + 2*a1*cos(dphi) + 2*a2*cos(2*dphi)\n"
            f"# Fitted to {len(RAYLEIGH_AZIMUTHS)} azimuths; azimuth_fit_error is the worst\n"
            "# disagreement for that row, and the worst over the whole table is "
            f"{worst_fit:.2e}.\n"
            "# The transmittances and the spherical albedo do not depend on the azimuth; the\n"
            f"# worst spread across azimuths is {worst_independence:.2e}, which is what makes\n"
            "# storing one value per row legitimate.\n"
            "# Interpolate multilinearly in (pressure, solar_zenith, view_zenith).\n"
        ) + "\n")
        handle.write(
            "platform,band,pressure_hpa,solar_zenith,view_zenith,a0,a1,a2,"
            "azimuth_fit_error,transmittance_down,transmittance_up,spherical_albedo\n"
        )
        for row in rows:
            platform, band, pressure, sza, vza = row[:5]
            a0, a1, a2, fit, down, up, albedo = row[5:]
            handle.write(
                f"{platform},{band},{pressure:g},{sza:g},{vza:g},"
                f"{a0:.6f},{a1:.6f},{a2:.6f},{fit:.2e},"
                f"{down:.6f},{up:.6f},{albedo:.6f}\n"
            )
    print(f"wrote {path} ({len(rows)} rows, worst azimuth fit {worst_fit:.2e}, "
          f"worst azimuth dependence of the transmittances {worst_independence:.2e})", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="src/pysent/data")
    parser.add_argument("--platforms", nargs="+", default=["S3A", "S3B"])
    parser.add_argument("--bands", nargs="+", type=int, default=list(BANDS))
    parser.add_argument("--jobs", type=int, default=max(1, multiprocessing.cpu_count() // 2))
    parser.add_argument(
        "--tables", nargs="+", default=["gas", "rayleigh"],
        choices=["gas", "rayleigh", "aerosol"],
    )
    args = parser.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    jobs = [(platform, band) for platform in args.platforms for band in args.bands]
    print(f"{len(jobs)} band-platform pairs, {args.jobs} workers", flush=True)

    with multiprocessing.Pool(args.jobs) as pool:
        if "aerosol" in args.tables:
            aerosol_jobs = [
                (platform, band, name)
                for platform, band in jobs
                for name in AEROSOL_MODELS
            ]
            write_aerosol(out, pool.map(aerosol_rows, aerosol_jobs))
        if "rayleigh" in args.tables:
            write_rayleigh(out, pool.map(rayleigh_rows, jobs))
        if "gas" not in args.tables:
            return 0
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
