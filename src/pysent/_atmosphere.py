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
