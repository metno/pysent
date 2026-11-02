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
import atexit
import datetime
import os
import pathlib
import re
import shutil
import sys
import tempfile

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from osgeo import gdal  # noqa: E402

gdal.UseExceptions()

from pysent._atmosphere import (  # noqa: E402
    angstrom_exponent,
    atmosphere_terms,
    gas_transmittance,
    pixel_pressure,
    platform_of,
    rayleigh_terms,
    relative_azimuth,
    surface_from_toa,
)
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

#: Where these scripts unpack product members. **Not /tmp.** A frame's
#: extracted members run to a few hundred megabytes, and ``sweep.py`` walks
#: fifty-odd frames in one run, so the default temporary directory fills the
#: root volume - which it did, to zero bytes free, mid-sweep. The data volume
#: has room for it; ``PYSENT_AUDIT_SCRATCH`` overrides, and the fallback is the
#: platform default for a host without that volume.
SCRATCH_ROOT = os.environ.get("PYSENT_AUDIT_SCRATCH") or (
    "/mnt/data/pysent-audit-scratch" if pathlib.Path("/mnt/data").is_dir() else None
)

AOD_MEMBER = "NTC_AOD.nc"
# Which aerosol wavelength stands closest to each band the composite renders.
NEAREST_AEROSOL_NM = {8: 670, 6: 550, 4: 440}
TIMES = re.compile(r"____(\d{8}T\d{6})_(\d{8}T\d{6})_")


def scratch_dir(prefix):
    """A directory to unpack product members into, on the data volume.

    Every script here uses this rather than ``tempfile.mkdtemp`` directly, so
    that one place decides where a few hundred megabytes per frame land - and
    so that one place removes them again at exit, on every return path and on
    an exception. A script that walks many frames should not wait for exit;
    ``sweep.py`` deletes each frame's directory as soon as it is done with it.
    """
    if SCRATCH_ROOT:
        pathlib.Path(SCRATCH_ROOT).mkdir(parents=True, exist_ok=True)
    path = pathlib.Path(tempfile.mkdtemp(prefix=prefix, dir=SCRATCH_ROOT))
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


def sensing_window(path):
    """The start and stop a product name declares."""
    match = TIMES.search(pathlib.Path(str(path)).name)
    if not match:
        raise ValueError(f"cannot read a sensing window from {pathlib.Path(str(path)).name!r}")
    fmt = "%Y%m%dT%H%M%S"
    return (
        datetime.datetime.strptime(match.group(1), fmt),
        datetime.datetime.strptime(match.group(2), fmt),
    )


def covers(outer, inner):
    """Does the outer sensing window contain the inner one?"""
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def aod_field(root, variable, scratch):
    """One field off the aerosol product, on the aerosol product's own grid."""
    local = extract_member(root, find_member(root, AOD_MEMBER), scratch)
    dataset = open_sen3_variable(local, variable)
    values = read_packed(dataset.GetRasterBand(1), decimate=1, log10=False)
    dataset = None
    return values


def waer_aerosol(wfr_root, shape, scratch):
    """Per-pixel aerosol loading from the water product's own retrieval.

    ``OL_2_WFR`` carries a ``w_aer`` member holding ``T865``, the aerosol
    optical thickness at 865 nm, and ``A865``, the Angstrom exponent, **on the
    same instrument grid as the radiance** and 75 to 79 % populated over clear
    water. That is a per-pixel loading where :func:`frame_aerosol` gives one
    number for a whole frame off a coarse unaligned grid that was 9 % populated
    over the frame this was measured on.

    Returned at 550 nm, because that is the axis the table is built on:
    ``tau550 = T865 * (550/865) ** -A865``.

    **The two ESA products disagree.** On the water frame, this route gives a
    550 nm depth of 0.092 and the coarse product gives 0.042 - a factor of 2.2
    over the same water, from two processors sharing one radiance. The owner's
    decision is to believe this one over water, on the grounds that it is
    per-pixel, on the right grid, eight times better covered, and produced by
    the processor specialised for water.
    """
    fields = {}
    for variable in ("T865", "A865"):
        local = extract_member(wfr_root, find_member(wfr_root, "w_aer.nc"), scratch)
        dataset = open_sen3_variable(local, variable)
        band = dataset.GetRasterBand(1)
        decimate = max(1, band.YSize // max(1, shape[0]))
        values = read_packed(band, decimate=decimate, log10=False)
        dataset = None
        if values.shape != shape:
            values = values[: shape[0], : shape[1]]
        fields[variable] = values

    with np.errstate(divide="ignore", invalid="ignore"):
        depth = fields["T865"] * (550.0 / 865.0) ** (-fields["A865"])
    usable = np.isfinite(depth) & np.isfinite(fields["A865"]) & (fields["T865"] > 0)
    return {
        "optical_depth_550": np.where(usable, depth, np.nan),
        "angstrom": np.where(usable, fields["A865"], np.nan),
        "usable": usable,
    }


def frame_aerosol(aod_path, latitude, longitude, scratch):
    """The loading the aerosol product reports over this frame, as two numbers.

    One optical depth at 550 nm and one Angstrom exponent for the whole frame,
    both medians over the aerosol cells inside the frame's bounding box.

    **Per frame, not per pixel, and deliberately so.** The aerosol product is
    6 to 25 % populated on its own coarse grid, which is itself unaligned with
    the radiance grid, so a per-pixel field would be mostly a gap-filling
    policy - what to do where there is no retrieval - rather than a measurement
    of the aerosol tier. That policy is Phase 4's subject. What this answers is
    narrower and is what Phase 3 has to establish first: given the loading ESA
    itself reports here, does correcting for it move the three rendered bands
    towards ESA's surface reflectance or away from it?

    The exponent is derived from the optical depths at 440 and 670 nm, because
    the product's own ``ANG550_865`` field is empty in every granule checked.
    """
    root = sen3_root(aod_path)
    box = (
        float(np.nanmin(latitude)), float(np.nanmax(latitude)),
        float(np.nanmin(longitude)), float(np.nanmax(longitude)),
    )
    lat = aod_field(root, "latitude", scratch)
    lon = aod_field(root, "longitude", scratch)
    inside = (lat >= box[0]) & (lat <= box[1]) & (lon >= box[2]) & (lon <= box[3])
    if not inside.any():
        raise ValueError(f"no cell of {pathlib.Path(str(aod_path)).name} falls inside the frame")

    def median(variable):
        values = aod_field(root, variable, scratch)[inside]
        values = values[np.isfinite(values)]
        return (
            float(np.median(values)) if values.size else float("nan"),
            values.size / max(1, int(inside.sum())),
        )

    depth_550, coverage = median("AOD_550")
    depth_440, _ = median("AOD_440")
    depth_670, _ = median("AOD_670")
    return {
        "cells": int(inside.sum()),
        "coverage": coverage,
        "optical_depth_550": depth_550,
        "optical_depth_440": depth_440,
        "optical_depth_670": depth_670,
        "angstrom": angstrom_exponent(depth_440, depth_670),
    }


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
    parser.add_argument(
        "--aod", nargs="+",
        help="SY_2_AOD products for the day; the one whose sensing window covers this frame "
             "is used, and the aerosol tier is added to the comparison. Several are accepted "
             "because an aerosol product runs orbit-long, so which one covers a given frame "
             "is not something the caller should have to work out.",
    )
    parser.add_argument("--decimate", type=int, default=4)
    args = parser.parse_args()

    scratch = scratch_dir("ac-budget-")
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

    # Elevation is per pixel, not on the tie grid, and it matters: Rayleigh
    # optical depth follows pressure, so ground at 2 km scatters a fifth less.
    altitude_dataset = open_sen3_variable(
        extract_member(l1, find_member(l1, "geo_coordinates.nc"), scratch), "altitude"
    )
    altitude = read_packed(
        altitude_dataset.GetRasterBand(1), decimate=args.decimate, log10=False
    )
    altitude_dataset = None
    if altitude.shape != shape:
        altitude = altitude[: shape[0], : shape[1]]
    pressure = pixel_pressure(slp, altitude)
    azimuth = relative_azimuth(saa, oaa)
    print(f"elevation median {np.nanmedian(altitude):.0f} m, surface pressure median "
          f"{np.nanmedian(pressure):.1f} hPa, relative azimuth median "
          f"{np.nanmedian(azimuth):.1f} deg")

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

    aerosol = None
    if args.aod:
        # An aerosol product runs orbit-long, so it has to be checked against
        # the frame rather than assumed to match it - the same check the
        # library's explicit aerosol option will need.
        frame_window = sensing_window(args.l1)
        picked = [path for path in args.aod if covers(sensing_window(path), frame_window)]
        if not picked:
            print(f"no aerosol product covers the frame's {frame_window[0]} to "
                  f"{frame_window[1]}; the {len(args.aod)} offered cover "
                  + ", ".join(
                      f"{sensing_window(p)[0]} to {sensing_window(p)[1]}" for p in args.aod
                  ))
            return 2
        latitude = tie_field(l1, "tie_geo_coordinates.nc", "latitude", shape, scratch)
        longitude = tie_field(l1, "tie_geo_coordinates.nc", "longitude", shape, scratch)
        aerosol = frame_aerosol(picked[0], latitude, longitude, scratch)
        print(f"aerosol: {pathlib.Path(picked[0]).name}")
        print(f"         {aerosol['cells']:,} cells inside the frame, "
              f"{100.0 * aerosol['coverage']:.0f} % of them retrieved; "
              f"AOD(550) {aerosol['optical_depth_550']:.4f}, "
              f"AOD(440) {aerosol['optical_depth_440']:.4f}, "
              f"AOD(670) {aerosol['optical_depth_670']:.4f}, "
              f"Angstrom {aerosol['angstrom']:.3f}\n")

    for scene, mask, reference in (
        ("water", water, ("wfr", args.wfr)),
        ("land", land_clear, ("syn", args.syn)),
    ):
        if reference[1] is None:
            continue
        kind = reference[0]
        root = sen3_root(reference[1])

        # Over water, take the loading from the water product's own per-pixel
        # retrieval rather than the coarse product's one-number-per-frame; over
        # land there is no such field, so the coarse product stands. The two
        # disagree by a factor of 2.2 and this is the owner's decision between
        # them - see waer_aerosol.
        scene_aerosol, source = aerosol, "the coarse aerosol product, per frame"
        if kind == "wfr":
            try:
                per_pixel = waer_aerosol(root, shape, scratch)
            except (ValueError, RuntimeError) as error:
                print(f"    (no per-pixel aerosol in this WFR: {error})")
            else:
                covered = per_pixel["usable"] & mask
                if covered.sum() >= 100:
                    scene_aerosol = per_pixel
                    source = (
                        f"WFR w_aer, per pixel, on "
                        f"{100.0 * covered.sum() / max(1, int(mask.sum())):.0f} % of this scene; "
                        f"median AOD(550) "
                        f"{np.nanmedian(np.where(covered, per_pixel['optical_depth_550'], np.nan)):.4f}"
                        f", Angstrom "
                        f"{np.nanmedian(np.where(covered, per_pixel['angstrom'], np.nan)):.3f}"
                    )

        print(f"=== {scene}: Level-1 against {kind.upper()} "
              f"({'water-leaving' if kind == 'wfr' else 'surface directional'} reflectance)")
        if scene_aerosol is not None:
            print(f"    aerosol from {source}")
        print(f"{'band':6s} {'nm':>5s} {'TOA':>8s} {'T_gas':>7s} {'rho_R(ss)':>10s} "
              f"{'rho_R(6S)':>10s} {'T_dn*T_up':>10s} {'surface':>9s} {'L2':>8s} "
              f"{'residual':>9s} {'r(TOA,L2)':>10s} {'r(corr,L2)':>11s}"
              + ("" if scene_aerosol is None else
                 f" {'+aerosol':>9s} {'residual':>9s} {'r':>7s}"))
        for b in BANDS:
            nm = BAND_WAVELENGTHS[b]
            toa = _toa(l1, b, shape, sza, scratch, args.decimate)
            # The gas term: the library's own tables, not a formula repeated here.
            t_gas = gas_transmittance(
                platform, b, ozone_kg_m2=ozone, water_kg_m2=vapour,
                solar_zenith_deg=sza, view_zenith_deg=oza,
            )
            # The audit's own single-scattering estimate, kept as the
            # comparison: it is what Phase 2 replaces.
            rho_r_single = rayleigh_reflectance(nm, slp, sza, oza, saa, oaa)
            terms = rayleigh_terms(
                platform, b, pressure_hpa=pressure, solar_zenith_deg=sza,
                view_zenith_deg=oza, relative_azimuth_deg=azimuth,
            )
            # The member names match across the two products but the variables
            # inside them do not: Synergy calls it SDR_Oa08, not Syn_Oa08_*.
            member = (f"Oa{b:02d}_reflectance.nc", f"Oa{b:02d}_reflectance") if kind == "wfr" else (
                f"Syn_Oa{b:02d}_reflectance.nc", f"SDR_Oa{b:02d}")
            l2 = level2_reflectance(root, member[0], member[1], shape, scratch)

            keep = mask & np.isfinite(toa) & np.isfinite(l2)
            two_way = terms["transmittance_down"] * terms["transmittance_up"]
            corrected = surface_from_toa(
                toa,
                gas_transmittance=t_gas,
                path_reflectance=terms["path_reflectance"],
                transmittance_down=terms["transmittance_down"],
                transmittance_up=terms["transmittance_up"],
                spherical_albedo=terms["spherical_albedo"],
            )
            columns = (
                toa, t_gas, rho_r_single, terms["path_reflectance"], two_way, corrected, l2,
            )
            stats = [np.nanmedian(np.where(keep, x, np.nan)) for x in columns]
            extra = ""
            if scene_aerosol is not None:
                # The same inversion with the aerosol increment added on top,
                # at the loading the aerosol product reports for this frame.
                hazy = atmosphere_terms(
                    platform, b, pressure_hpa=pressure, solar_zenith_deg=sza,
                    view_zenith_deg=oza, relative_azimuth_deg=azimuth,
                    ozone_kg_m2=ozone, water_kg_m2=vapour,
                    optical_depth_550=scene_aerosol["optical_depth_550"],
                    angstrom=scene_aerosol["angstrom"],
                )
                with_aerosol = surface_from_toa(toa, **hazy)
                median = np.nanmedian(np.where(keep, with_aerosol, np.nan))
                extra = (
                    f" {median:9.4f} {median - stats[6]:9.4f} "
                    f"{pearson(np.where(keep, with_aerosol, np.nan), np.where(keep, l2, np.nan)):7.3f}"
                )
            print(f"Oa{b:02d}  {nm:5d} {stats[0]:8.4f} {stats[1]:7.4f} {stats[2]:10.4f} "
                  f"{stats[3]:10.4f} {stats[4]:10.4f} {stats[5]:9.4f} {stats[6]:8.4f} "
                  f"{stats[5] - stats[6]:9.4f} "
                  f"{pearson(np.where(keep, toa, np.nan), np.where(keep, l2, np.nan)):10.3f} "
                  f"{pearson(np.where(keep, corrected, np.nan), np.where(keep, l2, np.nan)):11.3f}"
                  + extra)
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
