"""Surface reflectance from top-of-atmosphere reflectance.

The correction has three parts - gaseous absorption, Rayleigh scattering and
aerosol - and this module holds the frame all three fit into, plus the gas
part. Rayleigh and aerosol arrive in later phases; until then their terms
default to "no atmosphere" and the inversion reduces to an identity, which is
the first thing the tests check.

**The correction is not a subtraction.** Writing ``rho_surface = rho_toa -
rho_path`` leaves out three terms and gets the answer wrong by tens of per
cent at the air masses this library sees:

    rho_toa / T_gas  =  rho_path  +  T_down * T_up * rho_s / (1 - S * rho_s)

- ``T_gas`` is the two-way gaseous transmittance. Dividing by it *raises* the
  measured reflectance, because absorption removed signal on the way through.
- ``T_down * T_up`` is the scattering transmittance along the two legs.
  Measured with 6SV at 490 nm, solar zenith 70 and view zenith 20 it is
  **0.75**, so dividing by it is a 33 % lift - the single largest term after
  the path reflectance itself, and the one a subtraction silently drops.
- ``S`` is the atmosphere's spherical albedo seen from below, which couples a
  bright surface to its own illumination. A per-cent-level term over land,
  negligible over dark water.

Inverting the right-hand side for ``rho_s`` is exact, not iterative: with
``x = (rho_toa / T_gas - rho_path) / (T_down * T_up)``, the surface
reflectance is ``x / (1 + S * x)``.

**The gas part.** Ozone and water vapour are the two absorbers that matter
across OLCI's range, and they behave differently enough to need different
treatments, both measured with 6SV rather than assumed:

- **Ozone does not saturate** in the Chappuis band, so Beer-Lambert is exact
  and one coefficient per band suffices. Over columns of 100 to 400 Dobson
  units the coefficient is constant to about one part in ten thousand.
- **Water vapour does saturate**, so it needs a curve. But its transmittance
  depends only on the *scaled path amount* - the column times the air mass -
  so the curve is one-dimensional per band.

Both tables are generated offline by 6SV and committed under ``data/``; see
``tools/rt6s/`` for the container that regenerates them. Nothing here calls a
radiative-transfer code at processing time.

The OLCI product reports both columns in ``kg m-2``, which is neither unit the
tables use, and getting that conversion wrong is a silent factor of tens of
thousands - so it happens in exactly one place, here.
"""
from __future__ import annotations

import functools
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parent / "data"
OZONE_TABLE = "olci_ozone_absorption.csv"
WATER_TABLE = "olci_water_transmittance.csv"
RAYLEIGH_TABLE = "olci_rayleigh.csv"
AEROSOL_TABLE = "olci_aerosol.csv"

#: Wavelengths the aerosol product reports optical depth at, used to derive the
#: Angstrom exponent from the measurement rather than assuming a model.
ANGSTROM_PAIR_NM = (440.0, 670.0)

#: Standard sea-level pressure, the reference the barometric formula uses.
STANDARD_PRESSURE_HPA = 1013.25

#: One Dobson unit of ozone in kg m-2 (0.4462 mmol m-2 of O3).
KG_M2_PER_DOBSON = 2.1415e-5
#: The OLCI product reports water vapour in kg m-2; 6S takes g cm-2.
G_CM2_PER_KG_M2 = 0.1

#: Platforms the tables cover. OLCI band filters differ between them.
PLATFORMS = ("S3A", "S3B")


def platform_of(name: str) -> str:
    """The platform a Sentinel-3 product name belongs to.

    The band filters differ between S3A and S3B, so a coefficient belongs to a
    platform as much as to a band. A name that is neither raises rather than
    guessing, because picking the wrong platform's table is a silent error of
    the size this module exists to remove.
    """
    stem = Path(str(name)).name.upper()
    for platform in PLATFORMS:
        if stem.startswith(platform + "_") or f"/{platform}_" in str(name).upper():
            return platform
    raise ValueError(
        f"cannot tell which platform {stem!r} is from; the gas tables are per platform, so "
        f"pass one of {', '.join(PLATFORMS)} explicitly"
    )


@functools.lru_cache(maxsize=4)
def _ozone_coefficients() -> dict[tuple[str, int], float]:
    """``{(platform, band): coefficient}`` per Dobson unit per air mass."""
    rows = {}
    for line in (DATA_DIR / OZONE_TABLE).read_text().splitlines():
        if not line or line.startswith("#") or line.startswith("platform,"):
            continue
        platform, band, _wavelength, coefficient, _spread = line.split(",")
        rows[(platform, int(band))] = float(coefficient)
    if not rows:
        raise ValueError(f"{DATA_DIR / OZONE_TABLE} holds no coefficients")
    return rows


@functools.lru_cache(maxsize=4)
def _water_curves() -> dict[tuple[str, int], tuple[np.ndarray, np.ndarray]]:
    """``{(platform, band): (path amounts, transmittances)}``, amounts ascending."""
    gathered: dict[tuple[str, int], list[tuple[float, float]]] = {}
    for line in (DATA_DIR / WATER_TABLE).read_text().splitlines():
        if not line or line.startswith("#") or line.startswith("platform,"):
            continue
        platform, band, _wavelength, amount, transmittance = line.split(",")
        gathered.setdefault((platform, int(band)), []).append(
            (float(amount), float(transmittance))
        )
    curves = {}
    for key, samples in gathered.items():
        samples.sort()
        curves[key] = (
            np.array([amount for amount, _ in samples]),
            np.array([value for _, value in samples]),
        )
    if not curves:
        raise ValueError(f"{DATA_DIR / WATER_TABLE} holds no curves")
    return curves


def _table_key(platform: str, band: int, available) -> tuple[str, int]:
    key = (platform, int(band))
    if key not in available:
        raise ValueError(
            f"no gas table row for {platform} band {band}; the tables cover bands "
            f"{min(b for _, b in available)} to {max(b for _, b in available)} of "
            f"{', '.join(sorted({p for p, _ in available}))}"
        )
    return key


def water_path_amount_limit(platform: str, band: int) -> float:
    """The largest scaled path amount the water curve was generated for.

    Beyond it the transmittance is held at the curve's last value rather than
    extrapolated, because extrapolating a saturating absorption downwards
    produces transmittances that fall through zero.
    """
    curves = _water_curves()
    amounts, _ = curves[_table_key(platform, band, curves)]
    return float(amounts[-1])


def air_mass(solar_zenith_deg, view_zenith_deg):
    """Two-way air mass, ``1/cos(sza) + 1/cos(vza)``.

    NaN where either angle is at or past the horizon: a path that never
    reaches the surface has no air mass, and returning a huge number instead
    would quietly produce a transmittance of zero and a reflectance of
    infinity.
    """
    solar_angle = np.asarray(solar_zenith_deg, dtype=np.float64)
    view_angle = np.asarray(view_zenith_deg, dtype=np.float64)
    solar = np.cos(np.radians(solar_angle))
    view = np.cos(np.radians(view_angle))
    with np.errstate(divide="ignore", invalid="ignore"):
        total = 1.0 / solar + 1.0 / view
    # The angle decides, not its cosine: cos(90) is 6e-17 rather than 0 in
    # floating point, so testing the cosine lets a 90 degree angle through as
    # an air mass of 1.6e16.
    usable = (solar_angle < 90.0) & (view_angle < 90.0) & (solar > 0) & (view > 0)
    return np.where(usable, total, np.nan)


def ozone_transmittance(platform: str, band: int, ozone_kg_m2, mass):
    """Two-way ozone transmittance, exact Beer-Lambert.

    ``ozone_kg_m2`` is the column as the OLCI product reports it; the
    conversion to Dobson units happens here.
    """
    coefficients = _ozone_coefficients()
    coefficient = coefficients[_table_key(platform, band, coefficients)]
    dobson = np.asarray(ozone_kg_m2, dtype=np.float64) / KG_M2_PER_DOBSON
    return np.exp(-coefficient * dobson * np.asarray(mass, dtype=np.float64))


def water_transmittance(platform: str, band: int, water_kg_m2, mass):
    """Two-way water-vapour transmittance, interpolated on the scaled amount.

    ``water_kg_m2`` is the column as the OLCI product reports it. Amounts past
    the curve's end are held at its last value - see
    :func:`water_path_amount_limit`.
    """
    curves = _water_curves()
    amounts, values = curves[_table_key(platform, band, curves)]
    scaled = (
        np.asarray(water_kg_m2, dtype=np.float64)
        * G_CM2_PER_KG_M2
        * np.asarray(mass, dtype=np.float64)
    )
    # np.interp holds the end values outside the range, which is the clamping
    # this wants, but it does not propagate NaN - so that is done explicitly.
    interpolated = np.interp(scaled, amounts, values)
    return np.where(np.isfinite(scaled), interpolated, np.nan)


def gas_transmittance(
    platform: str,
    band: int,
    *,
    ozone_kg_m2,
    water_kg_m2,
    solar_zenith_deg,
    view_zenith_deg,
):
    """Two-way transmittance of both absorbers together."""
    mass = air_mass(solar_zenith_deg, view_zenith_deg)
    return ozone_transmittance(platform, band, ozone_kg_m2, mass) * water_transmittance(
        platform, band, water_kg_m2, mass
    )


@functools.lru_cache(maxsize=4)
def _rayleigh_grids():
    """``{(platform, band): (axes, cubes)}`` read from the committed table.

    ``axes`` is ``(pressures, solar zeniths, view zeniths)``, each ascending;
    ``cubes`` maps a quantity name to an array of that shape.
    """
    gathered: dict[tuple[str, int], dict[tuple[float, float, float], tuple]] = {}
    for line in (DATA_DIR / RAYLEIGH_TABLE).read_text().splitlines():
        if not line or line.startswith("#") or line.startswith("platform,"):
            continue
        fields = line.split(",")
        key = (fields[0], int(fields[1]))
        pressure, solar, view = (float(fields[2]), float(fields[3]), float(fields[4]))
        a0, a1, a2 = (float(fields[5]), float(fields[6]), float(fields[7]))
        down, up, albedo = (float(fields[9]), float(fields[10]), float(fields[11]))
        gathered.setdefault(key, {})[(pressure, solar, view)] = (a0, a1, a2, down, up, albedo)

    grids = {}
    for key, points in gathered.items():
        pressures = np.array(sorted({p for p, _, _ in points}))
        solars = np.array(sorted({s for _, s, _ in points}))
        views = np.array(sorted({v for _, _, v in points}))
        shape = (pressures.size, solars.size, views.size)
        if len(points) != shape[0] * shape[1] * shape[2]:
            raise ValueError(
                f"the Rayleigh table is not a full grid for {key[0]} band {key[1]}: "
                f"{len(points)} rows for a {shape} grid"
            )
        names = ("a0", "a1", "a2", "transmittance_down", "transmittance_up", "spherical_albedo")
        cubes = {name: np.empty(shape) for name in names}
        for (pressure, solar, view), values in points.items():
            index = (
                int(np.searchsorted(pressures, pressure)),
                int(np.searchsorted(solars, solar)),
                int(np.searchsorted(views, view)),
            )
            for name, value in zip(names, values):
                cubes[name][index] = value
        grids[key] = ((pressures, solars, views), cubes)
    if not grids:
        raise ValueError(f"{DATA_DIR / RAYLEIGH_TABLE} holds no grid")
    return grids


#: How far above the Rayleigh table's top pressure a value may be
#: extrapolated, in hPa. The table stops at sea-level standard because 6SV
#: cannot be asked for a negative target altitude, but real sea-level pressure
#: reaches about 1040 hPa in a winter anticyclone.
PRESSURE_EXTRAPOLATION_HPA = 60.0


def _secant(degrees):
    """``1/cos`` of an angle in degrees - the axis the path reflectance is linear in.

    Rayleigh path reflectance goes as ``tau * P(theta) / (4 mu_s mu_v)``, so it
    is close to linear in the *secant* of each zenith angle and markedly curved
    in the angle itself. Interpolating in secant space is therefore not a
    refinement but most of the accuracy: on the steepest corner of the grid -
    solar zenith 78.5, view zenith 52 - interpolating in degrees misses 6SV by
    0.0041 in reflectance, and in secant space by about a tenth of that.
    """
    return 1.0 / np.cos(np.radians(np.asarray(degrees, dtype=np.float64)))


def _axis_weights(nodes, values, extrapolate_above=0.0, transform=None):
    """Bracketing indices and the upper weight.

    Clamped to the nodes by default, which is deliberate for the angles: past
    the last solar zenith the path reflectance rises steeply, and a linear
    extrapolation of a steep curve invents brightness.

    ``extrapolate_above`` opens the top end by that much, for the pressure
    axis only. That is safe where clamping an angle would not be, because the
    path reflectance is nearly linear in pressure: measured with 6SV at 490 nm
    and solar zenith 70, a straight line through the top two nodes predicts the
    third to 5e-5 in reflectance. Clamping instead would cost about 0.0025 at
    1040 hPa, a quarter of this phase's whole error budget.
    """
    values = np.asarray(values, dtype=np.float64)
    # Clamp before transforming: the secant of 90 degrees is infinite, and the
    # bracketing has to happen on finite numbers.
    bounded = np.clip(values, nodes[0], nodes[-1] + extrapolate_above)
    upper = np.clip(np.searchsorted(nodes, bounded, side="left"), 1, nodes.size - 1)
    lower = upper - 1
    if transform is not None:
        scale = transform(nodes)
        bounded = transform(bounded)
    else:
        scale = nodes
    weight = (bounded - scale[lower]) / (scale[upper] - scale[lower])
    return lower, upper, weight


#: How each axis of a table is interpolated past its last node. The two zenith
#: axes interpolate in their secant, which is what the path reflectance is
#: linear in, and neither extrapolates.
#:
#: **The first axis differs between the two tables, which is why the caller has
#: to say.** For Rayleigh it is pressure, which may extrapolate a little
#: because the physics is nearly linear in it. For aerosol it is optical depth,
#: which may not: the table stops at 0.4, and extrapolating from there drives
#: the transmittance *negative* past about 1.5 - a smoke plume would come back
#: with a sign-flipped surface reflectance stated confidently. This used to be
#: one hardwired tuple naming pressure, inherited by the aerosol grid when it
#: reused this function.
RAYLEIGH_AXES = ((PRESSURE_EXTRAPOLATION_HPA, 0.0, 0.0), (None, _secant, _secant))
AEROSOL_AXES = ((0.0, 0.0, 0.0), (None, _secant, _secant))


def _trilinear(cube, axes, coordinates, interpolation):
    """Multilinear interpolation on a regular but unevenly spaced grid.

    numpy only - scipy is not a dependency of this library, and a
    three-dimensional multilinear interpolation is eight weighted corners.

    ``interpolation`` is ``(allowances, transforms)`` for the three axes -
    :data:`RAYLEIGH_AXES` or :data:`AEROSOL_AXES`. It is required rather than
    defaulted because the right answer differs per table; see those two.
    """
    allowances, transforms = interpolation
    brackets = [
        _axis_weights(nodes, value, allowance, transform)
        for nodes, value, allowance, transform in zip(
            axes, coordinates, allowances, transforms
        )
    ]
    total = 0.0
    for first in (0, 1):
        for second in (0, 1):
            for third in (0, 1):
                picks = []
                weight = 1.0
                for choice, (lower, upper, fraction) in zip((first, second, third), brackets):
                    picks.append(upper if choice else lower)
                    weight = weight * (fraction if choice else 1.0 - fraction)
                total = total + weight * cube[tuple(picks)]
    return total


def rayleigh_grid_limits(platform: str, band: int):
    """``{"pressure_hpa": (lo, hi), ...}`` - the table's own coverage."""
    grids = _rayleigh_grids()
    (pressures, solars, views), _ = grids[_table_key(platform, band, grids)]
    return {
        "pressure_hpa": (float(pressures[0]), float(pressures[-1])),
        "solar_zenith": (float(solars[0]), float(solars[-1])),
        "view_zenith": (float(views[0]), float(views[-1])),
    }


def aerosol_grid_limits(platform: str = "S3A", band: int = 4):
    """``{"optical_depth_550": (lo, hi), ...}`` - the aerosol table's coverage.

    Neither end extrapolates, so a caller outside these limits gets the nearest
    edge: an optical depth of 1.5 is corrected as though it were 0.4. There is
    no way to tell that from the returned terms, which is why the limits are
    published - a per-pixel quality band has to be able to say "this pixel was
    hazier than the table goes". The one exception is below the lightest stored
    depth, where the terms scale to the exact zero at no aerosol at all rather
    than being held; see :func:`aerosol_terms`.
    """
    grids = _aerosol_grids()
    keys = [key for key in grids if key[0] == platform and key[1] == int(band)]
    if not keys:
        raise ValueError(
            f"the aerosol table has no rows for {platform} band {band}; it covers "
            + ", ".join(
                f"{p} band {b}" for p, b, _ in sorted({(p, b, "") for p, b, _ in grids})
            )
        )
    depths, solars, views = grids[keys[0]][0]
    return {
        "optical_depth_550": (float(depths[0]), float(depths[-1])),
        "solar_zenith": (float(solars[0]), float(solars[-1])),
        "view_zenith": (float(views[0]), float(views[-1])),
    }


def pixel_pressure(sea_level_pressure_hpa, altitude_m):
    """Air pressure at the surface, from the product's own two fields.

    The barometric formula of the standard atmosphere. Elevation matters more
    than it looks: Rayleigh optical depth is proportional to pressure, so
    ground at 2 km scatters about a fifth less than the same scene at sea
    level, and treating a mountain as sea level over-corrects it.

    Negative elevations (a few hundred metres below sea level exist in the
    products as fill or as genuine depressions) are kept rather than clipped;
    the formula is smooth through zero.
    """
    pressure = np.asarray(sea_level_pressure_hpa, dtype=np.float64)
    altitude = np.asarray(altitude_m, dtype=np.float64)
    return pressure * (1.0 - 2.25577e-5 * altitude) ** 5.25588


def relative_azimuth(solar_azimuth_deg, view_azimuth_deg):
    """The azimuth difference the Rayleigh table is indexed by, folded to 0-180.

    Scattering depends on the angle between the two planes and not on its
    sign, so the difference is folded: the table holds 0 to 180 only.
    """
    difference = np.abs(
        np.asarray(view_azimuth_deg, dtype=np.float64)
        - np.asarray(solar_azimuth_deg, dtype=np.float64)
    ) % 360.0
    return np.where(difference > 180.0, 360.0 - difference, difference)


def rayleigh_terms(
    platform: str,
    band: int,
    *,
    pressure_hpa,
    solar_zenith_deg,
    view_zenith_deg,
    relative_azimuth_deg,
):
    """The four molecular-scattering terms the inversion needs.

    The path reflectance is held as three Fourier terms in the relative
    azimuth, ``rho = a0 + 2 a1 cos(dphi) + 2 a2 cos(2 dphi)``, which is not an
    approximation worth worrying about: fitted to thirteen azimuths, the worst
    disagreement anywhere in the table is about 7e-6 in reflectance.

    These are **molecular scattering only** - the generated runs have gaseous
    absorption switched off, because the gas term is applied separately and a
    table holding both would count absorption twice.
    """
    grids = _rayleigh_grids()
    axes, cubes = grids[_table_key(platform, band, grids)]
    coordinates = (pressure_hpa, solar_zenith_deg, view_zenith_deg)
    terms = {
        name: _trilinear(cube, axes, coordinates, RAYLEIGH_AXES)
        for name, cube in cubes.items()
    }

    phi = np.radians(np.asarray(relative_azimuth_deg, dtype=np.float64))
    path = terms["a0"] + 2.0 * terms["a1"] * np.cos(phi) + 2.0 * terms["a2"] * np.cos(2.0 * phi)
    return {
        "path_reflectance": path,
        "transmittance_down": terms["transmittance_down"],
        "transmittance_up": terms["transmittance_up"],
        "spherical_albedo": terms["spherical_albedo"],
    }


@functools.lru_cache(maxsize=4)
def _aerosol_grids():
    """``{(platform, band, model): (axes, cubes, azimuths)}`` from the table.

    ``axes`` is ``(optical depths, solar zeniths, view zeniths)``; the table is
    at sea level only, so there is no pressure axis - see
    :func:`atmosphere_terms` for how it is combined with the molecular table.
    ``azimuths`` is the stored azimuth axis in degrees, taken from the header,
    and ``cubes`` carries one grid per azimuth sample under ``azimuth_NNN``
    beside the transmittances, the spherical albedo and the interpolation
    error.
    """
    gathered: dict[tuple[str, int, str], dict[tuple[float, float, float], tuple]] = {}
    path = DATA_DIR / AEROSOL_TABLE
    if not path.exists():
        return {}

    azimuths: list[float] = []
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith("platform,"):
            # The azimuth axis is in the header: the table stores samples every
            # ten degrees rather than a fitted form, because three Fourier
            # terms miss an aerosol phase function by up to 0.18 where both
            # zenith angles are large.
            # Exactly ``az`` plus three digits. A looser test matches any other
            # column that happens to begin with those letters, which an earlier
            # version of this table did (``azimuth_fit_error``).
            azimuths = [
                float(name[2:])
                for name in line.split(",")
                if len(name) == 5 and name.startswith("az") and name[2:].isdigit()
            ]
            if not azimuths:
                raise ValueError(
                    f"{path} has no azXXX columns; it looks like an older table that stored "
                    "fitted azimuth terms, which cannot hold an aerosol phase function - "
                    "regenerate it with tools/rt6s/generate_tables.py --tables aerosol"
                )
            continue
        fields = line.split(",")
        key = (fields[0], int(fields[1]), fields[2])
        coordinate = (float(fields[3]), float(fields[4]), float(fields[5]))
        samples = [float(value) for value in fields[11:]]
        if len(samples) != len(azimuths):
            raise ValueError(
                f"{path} row for {key} has {len(samples)} azimuth samples but the header "
                f"names {len(azimuths)}"
            )
        gathered.setdefault(key, {})[coordinate] = (
            float(fields[7]), float(fields[8]), float(fields[9]), float(fields[10]),
            float(fields[6]), *samples,
        )

    names = (
        "transmittance_down", "transmittance_up", "spherical_albedo", "band_optical_depth",
        "interpolation_error",
        *(f"azimuth_{int(a):03d}" for a in azimuths),
    )
    grids = {}
    for key, points in gathered.items():
        depths = np.array(sorted({d for d, _, _ in points}))
        solars = np.array(sorted({s for _, s, _ in points}))
        views = np.array(sorted({v for _, _, v in points}))
        shape = (depths.size, solars.size, views.size)
        if len(points) != shape[0] * shape[1] * shape[2]:
            raise ValueError(
                f"the aerosol table is not a full grid for {key}: {len(points)} rows "
                f"for a {shape} grid"
            )
        cubes = {name: np.empty(shape) for name in names}
        for (depth, solar, view), values in points.items():
            index = (
                int(np.searchsorted(depths, depth)),
                int(np.searchsorted(solars, solar)),
                int(np.searchsorted(views, view)),
            )
            for name, value in zip(names, values):
                cubes[name][index] = value
        grids[key] = ((depths, solars, views), cubes, np.array(azimuths))
    return grids


def aerosol_models(platform: str = "S3A") -> tuple[str, ...]:
    """Which aerosol models the committed table covers."""
    return tuple(sorted({model for p, _, model in _aerosol_grids() if p == platform}))


def angstrom_exponent(depth_short, depth_long, wavelengths_nm=ANGSTROM_PAIR_NM):
    """The Angstrom exponent implied by two optical depths.

    ``alpha = -ln(tau_short / tau_long) / ln(lam_short / lam_long)``. Derived
    from the aerosol product's own measurements, because the field that would
    have stated it directly is empty in every granule checked - and because
    choosing an aerosol model from a land/water mask mislabels continental
    outflow over water and marine air over coastal land.

    NaN where either depth is non-positive, which is also what the product
    gives where it has no retrieval.
    """
    short = np.asarray(depth_short, dtype=np.float64)
    long = np.asarray(depth_long, dtype=np.float64)
    ratio_nm = np.log(wavelengths_nm[0] / wavelengths_nm[1])
    with np.errstate(divide="ignore", invalid="ignore"):
        alpha = -np.log(short / long) / ratio_nm
    return np.where((short > 0) & (long > 0), alpha, np.nan)


@functools.lru_cache(maxsize=16)
def model_angstrom(platform: str, model: str) -> float:
    """One model's own Angstrom exponent, read off the table.

    Taken from the band optical depths the generator stored, so it describes
    the model as 6SV implements it rather than as a textbook summarises it.
    Bands 4 (490 nm) and 17 (865 nm) are the pair with the widest separation
    the aerosol table covers.
    """
    grids = _aerosol_grids()
    depths = {}
    for band, nm in ((4, 490.0), (17, 865.0)):
        key = (platform, band, model)
        if key not in grids:
            raise ValueError(
                f"the aerosol table has no {model} rows for {platform} band {band}, so this "
                "model's Angstrom exponent cannot be read from it"
            )
        _, cubes, _ = grids[key]
        depths[nm] = float(np.median(cubes["band_optical_depth"]))
    return float(
        -np.log(depths[490.0] / depths[865.0]) / np.log(490.0 / 865.0)
    )


def _model_weights(platform, alpha):
    """How much of each model to use, from the measured Angstrom exponent.

    A linear blend between the two models on their own exponents, clamped at
    both ends. **This is the weakest physical choice in the correction**: two
    models span a line through a space that has at least three dimensions -
    size distribution, absorption and vertical placement - and the archive's
    own single-scattering albedo field, which would have constrained
    absorption, is empty. Where the exponent is unknown the more absorbing of
    the two is not assumed; the blend falls back to the model nearer the
    scene's own median.
    """
    models = aerosol_models(platform)
    if len(models) == 1:
        return {models[0]: 1.0}
    exponents = {name: model_angstrom(platform, name) for name in models}
    low, high = min(exponents, key=exponents.get), max(exponents, key=exponents.get)
    span = exponents[high] - exponents[low]
    if span <= 0:
        return {low: 1.0}
    alpha = np.asarray(alpha, dtype=np.float64)
    weight = np.clip((alpha - exponents[low]) / span, 0.0, 1.0)
    # An unknown exponent gets the midpoint rather than a silent extreme.
    weight = np.where(np.isfinite(weight), weight, 0.5)
    return {low: 1.0 - weight, high: weight}


def _model_path(grids, key, coordinates, azimuth):
    """One model's total path reflectance at sea level, at this geometry.

    Interpolate the three grid axes first, which leaves one azimuth curve per
    pixel, then interpolate along it. Doing it this way round means the azimuth
    step is linear between stored samples rather than through a fitted form that
    cannot hold the forward-scattering peak - three Fourier terms are lossless
    for Rayleigh and wrong by up to 0.18 for aerosol.
    """
    axes, cubes, azimuths = grids[key]
    curve = np.stack(
        [
            _trilinear(cubes[f"azimuth_{int(a):03d}"], axes, coordinates, AEROSOL_AXES)
            for a in azimuths
        ],
        axis=-1,
    )
    lower, upper, fraction = _axis_weights(azimuths, azimuth)
    return np.take_along_axis(
        curve, np.expand_dims(lower, -1), axis=-1
    )[..., 0] * (1.0 - fraction) + np.take_along_axis(
        curve, np.expand_dims(upper, -1), axis=-1
    )[..., 0] * fraction


def aerosol_path_components(
    platform: str,
    band: int,
    *,
    optical_depth_550,
    solar_zenith_deg,
    view_zenith_deg,
    relative_azimuth_deg,
):
    """The path reflectance's pieces, *before* the models are blended.

    :func:`atmosphere_terms` blends the two aerosol models on the Angstrom
    exponent and hands back one number, which is what almost every caller
    wants. The aerosol retrieval is the exception: it searches over the
    exponent at fixed geometry, and the exponent changes **only the blend
    weights** - the table interpolation underneath it, which is the whole cost,
    does not depend on the exponent at all.

    Calling the blended form once per exponent therefore repeats the expensive
    part nineteen times over. Measured: the retrieval spent 180 s of its 180 s
    on 100 000 pixels inside 2204 calls to :func:`_trilinear`, from 38 calls to
    the blended forward model. This returns the exponent-independent pieces so
    that caller can interpolate once and blend as often as it likes:

    - ``model_path``: each model's own total path reflectance at sea level;
    - ``molecular_path``: the sea-level molecular path the table includes, which
      has to come back off before the increment is applied at pixel pressure;
    - ``scale``: the below-the-floor ramp towards the exact zero at tau = 0.

    A caller reassembles the blended path as
    ``sum(weight * model_path[model])`` over
    :func:`_model_weights`, and the increment as
    ``scale * (blended - molecular_path)``.
    """
    grids = _aerosol_grids()
    if not grids:
        raise ValueError(
            f"{DATA_DIR / AEROSOL_TABLE} is absent, so no aerosol correction is available; "
            "generate it with tools/rt6s/generate_tables.py --tables aerosol"
        )
    molecular = rayleigh_terms(
        platform, band,
        pressure_hpa=STANDARD_PRESSURE_HPA,
        solar_zenith_deg=solar_zenith_deg,
        view_zenith_deg=view_zenith_deg,
        relative_azimuth_deg=relative_azimuth_deg,
    )
    azimuth = np.asarray(relative_azimuth_deg, dtype=np.float64)
    coordinates = (optical_depth_550, solar_zenith_deg, view_zenith_deg)
    floor = float(min(grids[key][0][0][0] for key in grids if key[0] == platform))
    paths = {}
    for model in aerosol_models(platform):
        key = (platform, int(band), model)
        if key not in grids:
            raise ValueError(
                f"the aerosol table has no {model} rows for {platform} band {band}"
            )
        paths[model] = _model_path(grids, key, coordinates, azimuth)
    return {
        "model_path": paths,
        "molecular_path": molecular["path_reflectance"],
        "scale": np.clip(
            np.asarray(optical_depth_550, dtype=np.float64) / floor, 0.0, 1.0
        ),
    }


def aerosol_terms(
    platform: str,
    band: int,
    *,
    optical_depth_550,
    angstrom,
    solar_zenith_deg,
    view_zenith_deg,
    relative_azimuth_deg,
):
    """The aerosol contribution, as an increment over the molecular atmosphere.

    The committed table holds the **total** path reflectance at sea level - the
    molecular part and the aerosol part together, since that is what 6SV
    reports as one number - so what comes back here is that total minus the
    molecular-only value at sea level, which is the increment
    :func:`atmosphere_terms` adds to the pressure-corrected Rayleigh term.

    The transmittances come back as *ratios* to the sea-level molecular ones,
    because transmittances multiply along a path, and the spherical albedo as a
    difference.
    """
    grids = _aerosol_grids()
    if not grids:
        raise ValueError(
            f"{DATA_DIR / AEROSOL_TABLE} is absent, so no aerosol correction is available; "
            "generate it with tools/rt6s/generate_tables.py --tables aerosol"
        )
    molecular = rayleigh_terms(
        platform, band,
        pressure_hpa=STANDARD_PRESSURE_HPA,
        solar_zenith_deg=solar_zenith_deg,
        view_zenith_deg=view_zenith_deg,
        relative_azimuth_deg=relative_azimuth_deg,
    )
    azimuth = np.asarray(relative_azimuth_deg, dtype=np.float64)
    coordinates = (optical_depth_550, solar_zenith_deg, view_zenith_deg)
    # The lightest optical depth the table carries. Taken before the blend
    # rather than from whichever model happened to be looked up last.
    floor = float(min(grids[key][0][0][0] for key in grids if key[0] == platform))

    blended = {"path": 0.0, "down": 0.0, "up": 0.0, "albedo": 0.0, "error": 0.0}
    for model, weight in _model_weights(platform, angstrom).items():
        key = (platform, int(band), model)
        if key not in grids:
            raise ValueError(
                f"the aerosol table has no {model} rows for {platform} band {band}"
            )
        axes, cubes, _azimuths = grids[key]
        picked = _model_path(grids, key, coordinates, azimuth)
        blended["path"] = blended["path"] + weight * picked
        for name, field in (
            ("down", "transmittance_down"),
            ("up", "transmittance_up"),
            ("albedo", "spherical_albedo"),
            ("error", "interpolation_error"),
        ):
            blended[name] = blended[name] + weight * _trilinear(
                cubes[field], axes, coordinates, AEROSOL_AXES
            )

    # **Below the first stored optical depth, scale towards the exact zero at
    # tau = 0 rather than clamping.** Clamping is right for the zenith axes,
    # where the curve past the last node is steep and unknown, and wrong here,
    # because the value at tau = 0 is not unknown: an atmosphere with no
    # aerosol in it contributes no aerosol path reflectance, does not dim the
    # transmittance and adds nothing to the spherical albedo, by definition.
    # Clamping instead handed a clear-air pixel the whole correction for the
    # table's lightest haze - and this archive's loading is 0.02 to 0.10, so
    # that was most of it, over-corrected by up to half again.
    #
    # Linear in tau is a good approximation over this one interval and an exact
    # anchor at the end of it: measured on the table itself, the increment per
    # unit optical depth is 0.157 at tau 0.05 against 0.148 at 0.15, so the
    # curve through the origin is nearly straight.
    depth = np.asarray(optical_depth_550, dtype=np.float64)
    scale = np.clip(depth / floor, 0.0, 1.0)

    with np.errstate(divide="ignore", invalid="ignore"):
        return {
            "path_increment": scale * (blended["path"] - molecular["path_reflectance"]),
            "transmittance_down_ratio": 1.0 + scale * (
                blended["down"] / molecular["transmittance_down"] - 1.0
            ),
            "transmittance_up_ratio": 1.0 + scale * (
                blended["up"] / molecular["transmittance_up"] - 1.0
            ),
            "spherical_albedo_increment": scale * (
                blended["albedo"] - molecular["spherical_albedo"]
            ),
            # What the azimuth interpolation costs at this geometry, from the
            # table's own leave-one-out estimate. It is the number to look at
            # before trusting a correction at a large solar and view zenith
            # together, where the aerosol phase function is sharply peaked.
            # **Not** scaled down with the optical depth like the terms above:
            # it is the curvature of the whole stored path curve, molecular
            # part included, so shrinking it towards zero with the aerosol
            # would understate what the azimuth step actually costs.
            "interpolation_error": blended["error"],
        }


def atmosphere_terms(
    platform: str,
    band: int,
    *,
    pressure_hpa,
    solar_zenith_deg,
    view_zenith_deg,
    relative_azimuth_deg,
    ozone_kg_m2,
    water_kg_m2,
    optical_depth_550=None,
    angstrom=None,
):
    """Everything :func:`surface_from_toa` needs, for one band.

    With no ``optical_depth_550`` this is the gas-and-Rayleigh atmosphere of
    Phase 2. With one, the aerosol increment is applied on top:

    - the path reflectances **add**, molecular at the pixel's own pressure plus
      the aerosol increment taken at sea level;
    - the transmittances **multiply**, molecular at pressure times the
      aerosol's ratio;
    - the spherical albedos **add**.

    The aerosol part is tabulated at sea level only, so this combination
    assumes the aerosol's own scattering does not depend on the air column
    above it - true to the extent that the aerosol sits in the boundary layer,
    and the reason elevated terrain is an open question for review.
    """
    molecular = rayleigh_terms(
        platform, band,
        pressure_hpa=pressure_hpa,
        solar_zenith_deg=solar_zenith_deg,
        view_zenith_deg=view_zenith_deg,
        relative_azimuth_deg=relative_azimuth_deg,
    )
    terms = {
        "gas_transmittance": gas_transmittance(
            platform, band,
            ozone_kg_m2=ozone_kg_m2, water_kg_m2=water_kg_m2,
            solar_zenith_deg=solar_zenith_deg, view_zenith_deg=view_zenith_deg,
        ),
        "path_reflectance": molecular["path_reflectance"],
        "transmittance_down": molecular["transmittance_down"],
        "transmittance_up": molecular["transmittance_up"],
        "spherical_albedo": molecular["spherical_albedo"],
    }
    if optical_depth_550 is None:
        return terms

    aerosol = aerosol_terms(
        platform, band,
        optical_depth_550=optical_depth_550,
        angstrom=angstrom,
        solar_zenith_deg=solar_zenith_deg,
        view_zenith_deg=view_zenith_deg,
        relative_azimuth_deg=relative_azimuth_deg,
    )
    terms["path_reflectance"] = terms["path_reflectance"] + aerosol["path_increment"]
    terms["transmittance_down"] = (
        terms["transmittance_down"] * aerosol["transmittance_down_ratio"]
    )
    terms["transmittance_up"] = terms["transmittance_up"] * aerosol["transmittance_up_ratio"]
    terms["spherical_albedo"] = (
        terms["spherical_albedo"] + aerosol["spherical_albedo_increment"]
    )
    return terms


def surface_from_toa(
    toa,
    *,
    gas_transmittance=1.0,
    path_reflectance=0.0,
    transmittance_down=1.0,
    transmittance_up=1.0,
    spherical_albedo=0.0,
):
    """Invert the top-of-atmosphere reflectance for the surface's own.

    The defaults describe no atmosphere at all, so with none of them supplied
    this returns ``toa`` unchanged. That is deliberate: the phases fill the
    terms in one at a time, and an unfilled term must not quietly alter a
    pixel.

    NaN comes back only where the inversion is genuinely undefined: a
    non-positive transmittance, or a measurement that was already NaN.

    **A negative result is kept, not masked.** Where the path reflectance is
    brighter than the measurement the surface reflectance comes out below zero,
    and that is a real retrieval outcome rather than a failure: ESA's own
    water-leaving reflectance is negative on 28 % of its valid pixels in the
    matched scene this plan validates against. Masking those would hide the
    retrieval's error distribution and bias every statistic computed from the
    product towards agreement. A caller who wants only physical values should
    clip, and know how much was clipped.
    """
    toa = np.asarray(toa, dtype=np.float64)
    gas = np.asarray(gas_transmittance, dtype=np.float64)
    path = np.asarray(path_reflectance, dtype=np.float64)
    two_way = np.asarray(transmittance_down, dtype=np.float64) * np.asarray(
        transmittance_up, dtype=np.float64
    )
    albedo = np.asarray(spherical_albedo, dtype=np.float64)

    with np.errstate(divide="ignore", invalid="ignore"):
        corrected = toa / gas - path
        ratio = corrected / two_way
        surface = ratio / (1.0 + albedo * ratio)

    defined = (gas > 0) & (two_way > 0) & np.isfinite(toa) & np.isfinite(surface)
    return np.where(defined, surface, np.nan)


def toa_from_surface(
    surface,
    *,
    gas_transmittance=1.0,
    path_reflectance=0.0,
    transmittance_down=1.0,
    transmittance_up=1.0,
    spherical_albedo=0.0,
):
    """The forward model, for checking the inversion against itself.

    Kept beside the inversion rather than in the tests, because every later
    phase validates a new term by putting it through both directions.
    """
    surface = np.asarray(surface, dtype=np.float64)
    albedo = np.asarray(spherical_albedo, dtype=np.float64)
    two_way = np.asarray(transmittance_down, dtype=np.float64) * np.asarray(
        transmittance_up, dtype=np.float64
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        coupled = surface / (1.0 - albedo * surface)
        return (
            np.asarray(path_reflectance, dtype=np.float64) + two_way * coupled
        ) * np.asarray(gas_transmittance, dtype=np.float64)
