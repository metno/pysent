"""Is the 560 nm bias a lone band or a spectral trend?

Phase 2 left 560 nm about 0.012 low against Synergy surface reflectance, in
nearly every frame and independently of solar zenith. This localises it, by
turning the comparison round: instead of asking what surface reflectance our
correction produces, it asks **what path reflectance Synergy's answer implies**,

    rho_implied = rho_toa / T_gas  -  T_down T_up * rho_syn / (1 - S rho_syn)

and subtracts the Rayleigh part we already model. What is left must be the
aerosol path reflectance - and the aerosol path reflectance **cannot be
negative**, must fall smoothly with wavelength, and must be consistent with the
optical depth the aerosol product measured over the same ground.

So the residual becomes a physical test rather than a comparison: any band
where the implied aerosol path comes out negative says that band's chain is
wrong, and whether it is one band or a whole stretch of the spectrum says
where to look. Synergy carries sixteen OLCI bands, including 510, 560 and 620,
which brackets the Chappuis ozone band that 560 sits on.

    python3 spectral.py --l1 L1.zip --syn SYN.zip [--decimate 8]
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

from osgeo import gdal  # noqa: E402

gdal.UseExceptions()

from budget import _toa, level2_reflectance, tie_field  # noqa: E402

from pysent._atmosphere import (  # noqa: E402
    gas_transmittance,
    pixel_pressure,
    platform_of,
    rayleigh_terms,
    relative_azimuth,
)
from pysent._olci_l1 import (  # noqa: E402
    BAND_WAVELENGTHS,
    GEOMETRY_MEMBER,
    METEO_MEMBER,
    quality_mask,
    radiance_member,
)
from pysent._sen3 import (  # noqa: E402
    extract_member,
    find_member,
    open_sen3_variable,
    read_packed,
    sen3_root,
)

# The bands Synergy publishes a surface directional reflectance for.
SYNERGY_BANDS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 16, 17, 18, 21)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--l1", required=True)
    parser.add_argument("--syn", required=True)
    parser.add_argument("--decimate", type=int, default=8)
    args = parser.parse_args()

    scratch = pathlib.Path(tempfile.mkdtemp(prefix="ac-spectral-"))
    root = sen3_root(args.l1)
    syn_root = sen3_root(args.syn)
    platform = platform_of(args.l1)

    member, variable = radiance_member(8)
    probe = open_sen3_variable(extract_member(root, find_member(root, member), scratch), variable)
    columns, rows = probe.RasterXSize, probe.RasterYSize
    probe = None
    shape = (max(1, rows // args.decimate), max(1, columns // args.decimate))

    sza = tie_field(root, GEOMETRY_MEMBER, "SZA", shape, scratch)
    oza = tie_field(root, GEOMETRY_MEMBER, "OZA", shape, scratch)
    saa = tie_field(root, GEOMETRY_MEMBER, "SAA", shape, scratch)
    oaa = tie_field(root, GEOMETRY_MEMBER, "OAA", shape, scratch)
    slp = tie_field(root, METEO_MEMBER, "sea_level_pressure", shape, scratch)
    ozone = tie_field(root, METEO_MEMBER, "total_ozone", shape, scratch)
    vapour = tie_field(root, METEO_MEMBER, "total_columnar_water_vapour", shape, scratch)

    dataset = open_sen3_variable(
        extract_member(root, find_member(root, "geo_coordinates.nc"), scratch), "altitude"
    )
    altitude = read_packed(dataset.GetRasterBand(1), decimate=args.decimate, log10=False)
    dataset = None
    if altitude.shape != shape:
        altitude = altitude[: shape[0], : shape[1]]

    pressure = pixel_pressure(slp, altitude)
    azimuth = relative_azimuth(saa, oaa)
    rejected, _, bright = quality_mask(root, scratch, shape, ["invalid", "saturated"])
    land, _, _ = quality_mask(root, scratch, shape, ["land"])
    keep_base = land & ~rejected & ~bright
    print(f"{int(keep_base.sum()):,} clear land pixels, solar zenith median "
          f"{np.nanmedian(sza):.1f} deg\n")

    print(f"{'band':6s} {'nm':>5s} {'TOA':>8s} {'T_gas':>7s} {'rho_R':>8s} "
          f"{'SYN SDR':>8s} {'implied aerosol path':>21s}")
    for band in SYNERGY_BANDS:
        nm = BAND_WAVELENGTHS[band]
        try:
            syn = level2_reflectance(
                syn_root, f"Syn_Oa{band:02d}_reflectance.nc", f"SDR_Oa{band:02d}", shape, scratch
            )
        except (ValueError, RuntimeError):
            continue
        toa = _toa(root, band, shape, sza, scratch, args.decimate)
        t_gas = gas_transmittance(
            platform, band, ozone_kg_m2=ozone, water_kg_m2=vapour,
            solar_zenith_deg=sza, view_zenith_deg=oza,
        )
        terms = rayleigh_terms(
            platform, band, pressure_hpa=pressure, solar_zenith_deg=sza,
            view_zenith_deg=oza, relative_azimuth_deg=azimuth,
        )
        two_way = terms["transmittance_down"] * terms["transmittance_up"]
        coupled = syn / (1.0 - terms["spherical_albedo"] * syn)
        implied = toa / t_gas - terms["path_reflectance"] - two_way * coupled

        keep = keep_base & np.isfinite(toa) & np.isfinite(syn)
        pick = lambda x: float(np.nanmedian(np.where(keep, x, np.nan)))  # noqa: E731
        value = pick(implied)
        flag = "  <-- NEGATIVE, impossible" if value < 0 else ""
        print(f"Oa{band:02d}  {nm:5d} {pick(toa):8.4f} {pick(t_gas):7.4f} "
              f"{pick(terms['path_reflectance']):8.4f} {pick(syn):8.4f} "
              f"{value:21.4f}{flag}")

    print("\nThe implied aerosol path reflectance must be positive and fall smoothly with\n"
          "wavelength. A single band out of line localises the fault to that band's chain;\n"
          "a run of bands out of line points at the gas or Rayleigh model instead.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
