"""The reflectance budget: how much of top-of-atmosphere is atmosphere?

pysent's true-colour product is top-of-atmosphere reflectance and applies no
atmospheric correction. This measures, per band, what a correction would have
to remove, by comparing one Level-1 frame against the two fully corrected
Level-2 products of the *same* acquisition:

* ``OL_2_WFR`` ``Oa*_reflectance`` - water-leaving reflectance, water only.
* ``SY_2_SYN`` ``Syn_Oa*_reflectance`` - surface directional reflectance, the
  quantity an all-in correction is trying to produce, over land and water.

All three products are on the same 4865-column instrument grid for the same
orbit and frame, so the comparison is per pixel with no warping in between.

The Rayleigh term is computed from the frame's own geometry and pressure with
the standard optical thickness, in the single-scattering approximation:

    tau_R  = 0.008569 l^-4 (1 + 0.0113 l^-2 + 0.00013 l^-4) * (p / p0)
    rho_R  = tau_R * P(Theta) / (4 cos(sza) cos(oza))
    P      = 3/4 (1 + cos^2 Theta)

Single scattering is a **lower bound**: multiple scattering and the surface
(Fresnel) term add to it, more at blue than at red. So the residual this script
reports is an upper bound on what is left for the gas and aerosol terms.

    python3 budget.py --l1 L1.zip --wfr WFR.zip --syn SYN.zip [--decimate 4]
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from osgeo import gdal  # noqa: E402

gdal.UseExceptions()

from pysent._atmosphere import gas_transmittance, platform_of  # noqa: E402
from pysent._olci_l1 import (  # noqa: E402
    BAND_WAVELENGTHS,
    GEOMETRY_MEMBER,
    METEO_MEMBER,
    quality_mask,
    radiance_member,
    solar_flux_per_pixel,
    tie_point_field,
)
from pysent._sen3 import (  # noqa: E402
    extract_member,
    find_member,
    open_sen3_variable,
    read_packed,
    sen3_root,
)

# The three bands the true-colour composite renders, red first.
BANDS = (8, 6, 4)
STANDARD_PRESSURE_HPA = 1013.25


def tie_field(root, member, variable, shape, scratch):
    """A tie-point field expanded across track - the library's own reader.

    This used to be a copy of the expansion inside ``_olci_l1.solar_zenith``.
    Phase 1 generalised that into ``tie_point_field``, so the audit now calls
    the same code the pipeline does, which is the point of the generalisation:
    a measurement taken with a private copy of the reader proves nothing about
    the reader that ships.
    """
    return tie_point_field(root, member, variable, scratch, shape)


def rayleigh_thickness(nm, pressure_hpa):
    """Rayleigh optical thickness at the given pressure."""
    um = nm / 1000.0
    tau0 = 0.008569 * um**-4 * (1 + 0.0113 * um**-2 + 0.00013 * um**-4)
    return tau0 * (pressure_hpa / STANDARD_PRESSURE_HPA)


def rayleigh_reflectance(nm, pressure_hpa, sza, oza, saa, oaa):
    """Single-scattering Rayleigh reflectance - a lower bound on the real term."""
    mu_s = np.cos(np.radians(sza))
    mu_v = np.cos(np.radians(oza))
    sin_s = np.sqrt(np.clip(1 - mu_s**2, 0, None))
    sin_v = np.sqrt(np.clip(1 - mu_v**2, 0, None))
    cos_theta = -mu_s * mu_v + sin_s * sin_v * np.cos(np.radians(saa - oaa))
    phase = 0.75 * (1 + cos_theta**2)
    tau = rayleigh_thickness(nm, pressure_hpa)
    with np.errstate(divide="ignore", invalid="ignore"):
        return tau * phase / (4 * mu_s * mu_v)


def level2_reflectance(root, member, variable, shape, scratch):
    """One Level-2 reflectance band at the shape being worked at."""
    local = extract_member(root, find_member(root, member), scratch)
    dataset = open_sen3_variable(local, variable)
    band = dataset.GetRasterBand(1)
    decimate = max(1, band.YSize // max(1, shape[0]))
    values = read_packed(band, decimate=decimate, log10=False)
    dataset = None
    if values.shape != shape:
        values = values[: shape[0], : shape[1]]
    return values


def pearson(a, b):
    keep = np.isfinite(a) & np.isfinite(b)
    if keep.sum() < 100:
        return float("nan")
    x, y = a[keep].astype(np.float64), b[keep].astype(np.float64)
    if x.std() == 0 or y.std() == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--l1", required=True)
    parser.add_argument("--wfr")
    parser.add_argument("--syn")
    parser.add_argument("--decimate", type=int, default=4)
    args = parser.parse_args()

    scratch = pathlib.Path(tempfile.mkdtemp(prefix="ac-budget-"))
    l1 = sen3_root(args.l1)

    # Shape comes from the decimated radiance band, and everything else is read
    # at that shape so no array is resampled after the fact.
    member, variable = radiance_member(BANDS[0])
    probe = open_sen3_variable(extract_member(l1, find_member(l1, member), scratch), variable)
    # Take the sizes off the band before the dataset goes: a band whose dataset
    # has been released is a dangling pointer, and GDAL does not say so politely.
    full_columns, full_rows = probe.RasterXSize, probe.RasterYSize
    probe = None
    shape = (max(1, full_rows // args.decimate), max(1, full_columns // args.decimate))
    print(f"frame {full_columns} x {full_rows}, read at {shape[1]} x {shape[0]} "
          f"(decimate {args.decimate})\n")

    sza = tie_field(l1, GEOMETRY_MEMBER, "SZA", shape, scratch)
    oza = tie_field(l1, GEOMETRY_MEMBER, "OZA", shape, scratch)
    saa = tie_field(l1, GEOMETRY_MEMBER, "SAA", shape, scratch)
    oaa = tie_field(l1, GEOMETRY_MEMBER, "OAA", shape, scratch)
    slp = tie_field(l1, METEO_MEMBER, "sea_level_pressure", shape, scratch)
    ozone = tie_field(l1, METEO_MEMBER, "total_ozone", shape, scratch)
    vapour = tie_field(l1, METEO_MEMBER, "total_columnar_water_vapour", shape, scratch)
    platform = platform_of(args.l1)

    rejected, _, bright = quality_mask(l1, scratch, shape, ["invalid", "saturated"])
    land, _, _ = quality_mask(l1, scratch, shape, ["land"])
    water = ~land & ~rejected & ~bright
    land_clear = land & ~rejected & ~bright

    print(f"geometry: SZA median {np.nanmedian(sza):.1f} deg, OZA up to {np.nanmax(oza):.1f} deg")
    print(f"pressure median {np.nanmedian(slp):.1f} hPa, ozone median "
          f"{np.nanmedian(ozone):.3g} (product units), water vapour median "
          f"{np.nanmedian(vapour):.3g}")
    print(f"pixels: {water.sum():,} clear water, {land_clear.sum():,} clear land, "
          f"{bright.sum():,} bright\n")

    for scene, mask, reference in (
        ("water", water, ("wfr", args.wfr)),
        ("land", land_clear, ("syn", args.syn)),
    ):
        if reference[1] is None:
            continue
        kind = reference[0]
        root = sen3_root(reference[1])
        print(f"=== {scene}: Level-1 against {kind.upper()} "
              f"({'water-leaving' if kind == 'wfr' else 'surface directional'} reflectance)")
        print(f"{'band':6s} {'nm':>5s} {'TOA':>8s} {'T_gas':>7s} {'TOA/T_gas':>10s} "
              f"{'rho_R':>8s} {'corrected':>10s} {'L2':>8s} {'residual':>9s} "
              f"{'r(TOA,L2)':>10s} {'r(corr,L2)':>11s}")
        for b in BANDS:
            nm = BAND_WAVELENGTHS[b]
            toa = _toa(l1, b, shape, sza, scratch, args.decimate)
            # The gas term: the library's own tables, not a formula repeated here.
            t_gas = gas_transmittance(
                platform, b, ozone_kg_m2=ozone, water_kg_m2=vapour,
                solar_zenith_deg=sza, view_zenith_deg=oza,
            )
            rho_r = rayleigh_reflectance(nm, slp, sza, oza, saa, oaa)
            # The member names match across the two products but the variables
            # inside them do not: Synergy calls it SDR_Oa08, not Syn_Oa08_*.
            member = (f"Oa{b:02d}_reflectance.nc", f"Oa{b:02d}_reflectance") if kind == "wfr" else (
                f"Syn_Oa{b:02d}_reflectance.nc", f"SDR_Oa{b:02d}")
            l2 = level2_reflectance(root, member[0], member[1], shape, scratch)

            keep = mask & np.isfinite(toa) & np.isfinite(l2)
            degassed = toa / t_gas
            corrected = degassed - rho_r
            stats = [
                np.nanmedian(x[keep]) for x in (toa, t_gas, degassed, rho_r, corrected, l2)
            ]
            print(f"Oa{b:02d}  {nm:5d} {stats[0]:8.4f} {stats[1]:7.4f} {stats[2]:10.4f} "
                  f"{stats[3]:8.4f} {stats[4]:10.4f} {stats[5]:8.4f} "
                  f"{stats[4] - stats[5]:9.4f} "
                  f"{pearson(np.where(keep, toa, np.nan), np.where(keep, l2, np.nan)):10.3f} "
                  f"{pearson(np.where(keep, corrected, np.nan), np.where(keep, l2, np.nan)):11.3f}")
        print()
    return 0


def _toa(root, band, shape, sza, scratch, decimate):
    """Top-of-atmosphere reflectance, as the pipeline computes it."""
    member, variable = radiance_member(band)
    dataset = open_sen3_variable(extract_member(root, find_member(root, member), scratch), variable)
    radiance = read_packed(dataset.GetRasterBand(1), decimate=decimate, log10=False)
    dataset = None
    if radiance.shape != shape:
        radiance = radiance[: shape[0], : shape[1]]
    flux = solar_flux_per_pixel(root, scratch, band, shape)
    cosine = np.cos(np.radians(sza))
    with np.errstate(divide="ignore", invalid="ignore"):
        rho = (np.pi * radiance) / (flux * cosine)
    return np.where(cosine > 0, rho, np.nan).astype(np.float32)


if __name__ == "__main__":
    raise SystemExit(main())
