"""Where does the correction stop being trustworthy?

The whole OLCI holding sits at solar zenith 63 to 79 degrees, and two frames
showed the land residual going from 0.013 at 71 degrees to 0.094 at 79.6. Two
points cannot place a threshold, so this runs the correction across a sample of
the matched triples and reports the residual against ESA's own product as a
function of solar zenith - which is what a per-pixel quality band needs in
order to say anything honest.

    python3 sweep.py [--count 20] [--decimate 8] [--platform S3A]

Each row is one acquisition: its median solar zenith, how much of the frame is
usable, and the median residual per band over clear land (against Synergy
surface reflectance) and over clear water (against the water-leaving
reflectance). Frames with too few usable pixels to mean anything are reported
as skipped rather than averaged in.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

from osgeo import gdal  # noqa: E402

gdal.UseExceptions()

from budget import (  # noqa: E402
    BANDS,
    _toa,
    covers,
    frame_aerosol,
    level2_reflectance,
    scratch_dir,
    sensing_window,
    tie_field,
)
from pairs import collect  # noqa: E402

from pysent._atmosphere import (  # noqa: E402
    atmosphere_terms,
    gas_transmittance,
    pixel_pressure,
    platform_of,
    rayleigh_terms,
    relative_azimuth,
    surface_from_toa,
)
from pysent._olci_l1 import (  # noqa: E402
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

#: Below this many usable pixels a median says nothing, so the frame is skipped.
MINIMUM_PIXELS = 20_000


def corrected_frame(l1_path, decimate, aod_paths=()):
    """Surface reflectance for the three bands, plus the masks and geometry.

    With ``aod_paths``, the one whose orbit-long sensing window covers this
    frame is used to apply the aerosol tier as well, and the result carries a
    second set of surfaces under ``aerosol_surfaces`` along with the loading it
    used. Without one - or when none of them covers the frame - that key is
    ``None``, which is how a frame with no aerosol retrieval reports itself
    rather than silently falling back to a guessed loading.
    """
    scratch = scratch_dir("ac-sweep-")
    root = sen3_root(str(l1_path))
    platform = platform_of(str(l1_path))

    member, variable = radiance_member(BANDS[0])
    probe = open_sen3_variable(extract_member(root, find_member(root, member), scratch), variable)
    columns, rows = probe.RasterXSize, probe.RasterYSize
    probe = None
    shape = (max(1, rows // decimate), max(1, columns // decimate))

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
    altitude = read_packed(dataset.GetRasterBand(1), decimate=decimate, log10=False)
    dataset = None
    if altitude.shape != shape:
        altitude = altitude[: shape[0], : shape[1]]

    pressure = pixel_pressure(slp, altitude)
    azimuth = relative_azimuth(saa, oaa)
    rejected, _, bright = quality_mask(root, scratch, shape, ["invalid", "saturated"])
    land, _, _ = quality_mask(root, scratch, shape, ["land"])
    usable = ~rejected & ~bright

    aerosol = None
    window = sensing_window(str(l1_path))
    for candidate in aod_paths:
        if covers(sensing_window(str(candidate)), window):
            latitude = tie_field(root, "tie_geo_coordinates.nc", "latitude", shape, scratch)
            longitude = tie_field(root, "tie_geo_coordinates.nc", "longitude", shape, scratch)
            try:
                aerosol = frame_aerosol(str(candidate), latitude, longitude, scratch)
            except ValueError:
                aerosol = None
            break

    surfaces, aerosol_surfaces = {}, {} if aerosol else None
    for band in BANDS:
        toa = _toa(root, band, shape, sza, scratch, decimate)
        t_gas = gas_transmittance(
            platform, band, ozone_kg_m2=ozone, water_kg_m2=vapour,
            solar_zenith_deg=sza, view_zenith_deg=oza,
        )
        terms = rayleigh_terms(
            platform, band, pressure_hpa=pressure, solar_zenith_deg=sza,
            view_zenith_deg=oza, relative_azimuth_deg=azimuth,
        )
        surfaces[band] = surface_from_toa(
            toa,
            gas_transmittance=t_gas,
            path_reflectance=terms["path_reflectance"],
            transmittance_down=terms["transmittance_down"],
            transmittance_up=terms["transmittance_up"],
            spherical_albedo=terms["spherical_albedo"],
        )
        if aerosol is not None and np.isfinite(aerosol["optical_depth_550"]):
            hazy = atmosphere_terms(
                platform, band, pressure_hpa=pressure, solar_zenith_deg=sza,
                view_zenith_deg=oza, relative_azimuth_deg=azimuth,
                ozone_kg_m2=ozone, water_kg_m2=vapour,
                optical_depth_550=aerosol["optical_depth_550"],
                angstrom=aerosol["angstrom"],
            )
            aerosol_surfaces[band] = surface_from_toa(toa, **hazy)
        elif aerosol_surfaces is not None:
            aerosol_surfaces = None
    return {
        "shape": shape,
        "scratch": scratch,
        "solar_zenith": sza,
        "land": land & usable,
        "water": ~land & usable,
        "surfaces": surfaces,
        "aerosol": aerosol,
        "aerosol_surfaces": aerosol_surfaces,
    }


def residuals(frame, reference_path, kind, which="surfaces"):
    """Median residual per band against one Level-2 reference.

    ``which`` selects the tier: ``surfaces`` is gas and Rayleigh, and
    ``aerosol_surfaces`` the same with the aerosol increment applied. Both are
    measured over the same pixels, so the difference between them is the tier
    and not a change of sample.
    """
    if frame.get(which) is None:
        return {band: (float("nan"), 0) for band in BANDS}
    root = sen3_root(str(reference_path))
    mask = frame["land"] if kind == "syn" else frame["water"]
    out = {}
    for band in BANDS:
        member = (
            (f"Oa{band:02d}_reflectance.nc", f"Oa{band:02d}_reflectance")
            if kind == "wfr"
            else (f"Syn_Oa{band:02d}_reflectance.nc", f"SDR_Oa{band:02d}")
        )
        try:
            l2 = level2_reflectance(root, member[0], member[1], frame["shape"], frame["scratch"])
        except (ValueError, RuntimeError):
            out[band] = (float("nan"), 0)
            continue
        surface = frame[which][band]
        # The two tiers are compared over the pixels both can answer, so a
        # frame cannot look better simply by answering fewer pixels.
        keep = mask & np.isfinite(surface) & np.isfinite(l2)
        if frame.get("aerosol_surfaces") is not None:
            keep = keep & np.isfinite(frame["surfaces"][band]) & np.isfinite(
                frame["aerosol_surfaces"][band]
            )
        count = int(keep.sum())
        if count < MINIMUM_PIXELS:
            out[band] = (float("nan"), count)
            continue
        out[band] = (
            float(np.nanmedian(np.where(keep, surface - l2, np.nan))),
            count,
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--decimate", type=int, default=8)
    parser.add_argument("--platform", default="S3A")
    parser.add_argument("--months", nargs="+", default=["2026/10", "2026/11"])
    args = parser.parse_args()

    found = collect(args.platform, args.months)
    complete = sorted(
        key for key, kinds in found.items() if {"l1", "wfr", "syn"} <= set(kinds)
    )
    # Aerosol products are orbit-long, so they never share an acquisition key
    # with a frame and have to be matched by sensing window instead.
    aod_paths = sorted(
        {str(kinds["aod"]) for kinds in found.values() if "aod" in kinds}
    )
    # Consecutive, not strided: the archive alternates two solar-zenith groups
    # between neighbouring acquisitions, so a stride lands on one group only -
    # a sample of three strided frames came out all at 78 to 79 degrees.
    sample = complete[args.offset:][: args.count]
    print(f"{len(complete)} complete triples and {len(aod_paths)} aerosol products; "
          f"taking {len(sample)} triples consecutively from offset {args.offset}\n")

    print(f"{'acquisition':18s} {'SZA':>6s} {'AOD':>6s} {'alpha':>6s} "
          f"{'land 665':>9s} {'land 560':>9s} {'land 490':>9s} "
          f"{'water 665':>10s} {'water 560':>10s} {'water 490':>10s}")
    rows = []
    for key in sample:
        paths = found[key]
        try:
            frame = corrected_frame(paths["l1"], args.decimate, aod_paths)
        except Exception as error:
            print(f"{key[0]:18s} unreadable: {type(error).__name__}: {error}")
            continue
        try:
            land = residuals(frame, paths["syn"], "syn")
            water = residuals(frame, paths["wfr"], "wfr")
            hazy_land = residuals(frame, paths["syn"], "syn", "aerosol_surfaces")
            hazy_water = residuals(frame, paths["wfr"], "wfr", "aerosol_surfaces")
            zenith = float(np.nanmedian(frame["solar_zenith"]))
            loading = frame["aerosol"] or {}
            rows.append((zenith, land, water, hazy_land, hazy_water, loading))

            def cells(values, width):
                return " ".join(
                    f"{values[b][0]:{width}.4f}" if np.isfinite(values[b][0])
                    else f"{'-':>{width}s}"
                    for b in BANDS
                )

            depth = loading.get("optical_depth_550", float("nan"))
            alpha = loading.get("angstrom", float("nan"))
            depth_cell = f"{depth:6.3f}" if np.isfinite(depth) else f"{'-':>6s}"
            alpha_cell = f"{alpha:6.2f}" if np.isfinite(alpha) else f"{'-':>6s}"
            print(f"{key[0]:18s} {zenith:6.1f} {depth_cell} {alpha_cell} "
                  + cells(land, 9) + " " + cells(water, 10), flush=True)
            if frame["aerosol_surfaces"] is not None:
                print(f"{'  + aerosol':18s} {'':6s} {'':6s} {'':6s} "
                      + cells(hazy_land, 9) + " " + cells(hazy_water, 10), flush=True)
        finally:
            # **Release the frame before reading the next one.** A frame's
            # extracted members are a few hundred megabytes and this loop walks
            # fifty of them, so holding them all filled the volume to zero bytes
            # free mid-run - which showed up as two frames at the tail failing
            # with OSError, a disk fault wearing the costume of a data fault.
            shutil.rmtree(frame["scratch"], ignore_errors=True)

    print("\nBy solar zenith band, median of the absolute land residual:")
    print(f"{'zenith':12s} {'frames':>7s} {'665':>8s} {'560':>8s} {'490':>8s} {'worst':>8s}")
    for low, high in ((0, 68), (68, 72), (72, 76), (76, 80), (80, 90)):
        inside = [(z, land) for z, land, _, _, _, _ in rows if low <= z < high]
        if not inside:
            continue
        per_band = []
        for band in BANDS:
            values = [abs(land[band][0]) for _, land in inside if np.isfinite(land[band][0])]
            per_band.append(float(np.median(values)) if values else float("nan"))
        worst = np.nanmax(per_band) if np.any(np.isfinite(per_band)) else float("nan")
        print(f"{low:3d}-{high:<8d} {len(inside):7d} "
              + " ".join(f"{v:8.4f}" if np.isfinite(v) else f"{'-':>8s}" for v in per_band)
              + f" {worst:8.4f}")

    # Does the aerosol tier move the answer towards ESA's or away from it? Over
    # every frame that has an aerosol retrieval, counted per band and per scene
    # type, because the two scenes answer different questions: Synergy's
    # surface directional reflectance is the quantity being produced, while the
    # water-leaving reflectance is a different quantity and agreement with it
    # has a floor that no correction can go below.
    print("\nWhat the aerosol tier does to the agreement, per band:")
    print(f"{'scene':8s} {'band':>5s} {'frames':>7s} {'|res| base':>11s} "
          f"{'|res| +aer':>11s} {'better':>7s} {'worse':>7s}")
    for scene, base_index, hazy_index in (("land", 1, 3), ("water", 2, 4)):
        for band in BANDS:
            pairs = [
                (abs(row[base_index][band][0]), abs(row[hazy_index][band][0]))
                for row in rows
                if np.isfinite(row[base_index][band][0]) and np.isfinite(row[hazy_index][band][0])
            ]
            if not pairs:
                continue
            base = float(np.median([p[0] for p in pairs]))
            hazy = float(np.median([p[1] for p in pairs]))
            better = sum(1 for b, h in pairs if h < b)
            print(f"{scene:8s} {band:5d} {len(pairs):7d} {base:11.4f} {hazy:11.4f} "
                  f"{better:7d} {len(pairs) - better:7d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
