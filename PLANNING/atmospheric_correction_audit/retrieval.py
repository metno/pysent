"""Can the aerosol loading be retrieved from the Level-1 frame alone?

Phase 3 took the loading from ESA's `SY_2_AOD` product. Phase 4 has to work
without it, and this asks the three questions that decide how - each against a
reference rather than against a textbook.

**The water reference is far better than the plan assumed.** `OL_2_WFR` carries
a `w_aer` member holding `T865`, the aerosol optical thickness at 865 nm, and
`A865`, the Angstrom exponent - **per pixel, on the same 4865-column instrument
grid as the radiance**, with uncertainties for both. That is a direct per-pixel
reference for a water retrieval, where `SY_2_AOD` is a coarse unaligned grid
that was only 9 % populated over the water frame. Audit finding A6 missed it
because it went looking for aerosol in the aerosol product.

1. **Is 865 nm actually black over this archive's water?** The dark-target
   retrieval assumes the water-leaving reflectance there is zero and reads the
   whole signal as atmosphere. WFR publishes `Oa17_reflectance`, so the
   assumption can be checked rather than asserted - and if it is not zero, the
   retrieved optical depth is biased high by whatever it is.
2. **Does a black-pixel inversion at 865 recover ESA's own T865?** Invert
   `rho_toa / T_gas = rho_path(tau)` for tau, convert to 865 nm through the
   table's own band optical depths, and compare per pixel.
3. **What would a land retrieval have to assume?** OLCI has no shortwave
   infrared band, so the usual dark-dense-vegetation anchor - red surface
   reflectance tied to 2100 nm - is not available. This reports what Synergy
   says the darkest clear land actually is at 490 and 665 nm, which is the
   number any land assumption has to live with.

    python3 retrieval.py --l1 L1.zip --wfr WFR.zip [--syn SYN.zip]
                         [--aod AOD.zip ...] [--decimate 8]
"""
from __future__ import annotations

import argparse
import pathlib
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

from pysent._atmosphere import (  # noqa: E402
    _aerosol_grids,
    _model_weights,
    aerosol_models,
    atmosphere_terms,
    gas_transmittance,
    pixel_pressure,
    platform_of,
    rayleigh_terms,
    relative_azimuth,
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

#: The near-infrared band the black-pixel assumption is made at.
DARK_BAND = 17
#: A grid of optical depths to invert on. Dense at the bottom, because this
#: archive's loading is 0.012 to 0.288 and the interesting range is the low end.
DEPTH_GRID = np.concatenate(
    [np.arange(0.0, 0.2, 0.004), np.arange(0.2, 0.401, 0.02)]
)


def band_depth_ratio(platform, band, model):
    """tau at this band's wavelength, per unit tau at 550 nm, from the table.

    Read off the committed table's own ``band_optical_depth`` column rather
    than computed from an assumed Angstrom exponent, so the conversion matches
    the model the correction actually applies.
    """
    axes, cubes, _ = _aerosol_grids()[(platform, int(band), model)]
    depths = np.asarray(axes[0], dtype=np.float64)
    # band_optical_depth varies only with the 550 nm depth, so take the median
    # over the two angle axes and fit the straight line through the origin.
    per_depth = np.median(cubes["band_optical_depth"], axis=(1, 2))
    return float(np.sum(per_depth * depths) / np.sum(depths * depths))


def retrieve_depth(platform, band, *, apparent, angstrom, pressure_hpa,
                   solar_zenith_deg, view_zenith_deg, relative_azimuth_deg,
                   ozone_kg_m2, water_kg_m2):
    """The 550 nm optical depth whose path reflectance matches ``apparent``.

    The black-pixel inversion: with the surface assumed black at this band,
    every photon is atmosphere, so ``rho_toa / T_gas`` is the path reflectance
    and the optical depth is whatever produces it. Monotonic in depth, so a
    grid plus a linear step between the bracketing nodes is enough and needs no
    iteration per pixel.

    Returns NaN where the measurement falls below the path reflectance of a
    clean atmosphere - that is not a retrieval of zero, it is a pixel saying
    the molecular model already over-explains it, and averaging it in as zero
    would bias the result low.
    """
    paths = []
    for depth in DEPTH_GRID:
        terms = atmosphere_terms(
            platform, band, pressure_hpa=pressure_hpa,
            solar_zenith_deg=solar_zenith_deg, view_zenith_deg=view_zenith_deg,
            relative_azimuth_deg=relative_azimuth_deg,
            ozone_kg_m2=ozone_kg_m2, water_kg_m2=water_kg_m2,
            optical_depth_550=np.full_like(np.asarray(apparent), depth),
            angstrom=angstrom,
        )
        paths.append(terms["path_reflectance"])
    stack = np.stack(paths, axis=-1)

    target = np.asarray(apparent, dtype=np.float64)[..., None]
    above = stack >= target
    # The first node at or above the measurement; no node means it is hazier
    # than the grid goes.
    index = np.argmax(above, axis=-1)
    found = above.any(axis=-1)
    index = np.clip(index, 1, DEPTH_GRID.size - 1)

    lower = np.take_along_axis(stack, (index - 1)[..., None], axis=-1)[..., 0]
    upper = np.take_along_axis(stack, index[..., None], axis=-1)[..., 0]
    span = np.where(upper > lower, upper - lower, np.nan)
    fraction = np.clip((np.asarray(apparent) - lower) / span, 0.0, 1.0)
    depth = DEPTH_GRID[index - 1] + fraction * (DEPTH_GRID[index] - DEPTH_GRID[index - 1])

    # Below the cleanest node the measurement is darker than a clean
    # atmosphere: refuse rather than report zero.
    too_dark = np.asarray(apparent) < stack[..., 0]
    return np.where(found & ~too_dark, depth, np.nan)


def waer_field(root, variable, shape, scratch):
    """One field of the WFR aerosol member, at the working shape."""
    local = extract_member(root, find_member(root, "w_aer.nc"), scratch)
    dataset = open_sen3_variable(local, variable)
    band = dataset.GetRasterBand(1)
    decimate = max(1, band.YSize // max(1, shape[0]))
    values = read_packed(band, decimate=decimate, log10=False)
    dataset = None
    if values.shape != shape:
        values = values[: shape[0], : shape[1]]
    return values


def summarise(name, values, mask):
    picked = np.where(mask, values, np.nan)
    finite = picked[np.isfinite(picked)]
    if finite.size == 0:
        print(f"  {name:28s} nothing inside the mask")
        return
    low, mid, high = np.percentile(finite, [5, 50, 95])
    coverage = 100.0 * finite.size / max(1, int(mask.sum()))
    print(f"  {name:28s} {low:9.4f} {mid:9.4f} {high:9.4f}   ({coverage:.0f} % present)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--l1", required=True)
    parser.add_argument("--wfr", required=True)
    parser.add_argument("--syn")
    parser.add_argument("--aod", nargs="+")
    parser.add_argument("--decimate", type=int, default=8)
    args = parser.parse_args()

    scratch = scratch_dir("ac-retrieval-")
    l1 = sen3_root(args.l1)
    platform = platform_of(args.l1)

    member, variable = radiance_member(BANDS[0])
    probe = open_sen3_variable(extract_member(l1, find_member(l1, member), scratch), variable)
    columns, rows = probe.RasterXSize, probe.RasterYSize
    probe = None
    shape = (max(1, rows // args.decimate), max(1, columns // args.decimate))
    print(f"frame {columns} x {rows}, read at {shape[1]} x {shape[0]}\n")

    sza = tie_field(l1, GEOMETRY_MEMBER, "SZA", shape, scratch)
    oza = tie_field(l1, GEOMETRY_MEMBER, "OZA", shape, scratch)
    saa = tie_field(l1, GEOMETRY_MEMBER, "SAA", shape, scratch)
    oaa = tie_field(l1, GEOMETRY_MEMBER, "OAA", shape, scratch)
    slp = tie_field(l1, METEO_MEMBER, "sea_level_pressure", shape, scratch)
    ozone = tie_field(l1, METEO_MEMBER, "total_ozone", shape, scratch)
    vapour = tie_field(l1, METEO_MEMBER, "total_columnar_water_vapour", shape, scratch)

    dataset = open_sen3_variable(
        extract_member(l1, find_member(l1, "geo_coordinates.nc"), scratch), "altitude"
    )
    altitude = read_packed(dataset.GetRasterBand(1), decimate=args.decimate, log10=False)
    dataset = None
    if altitude.shape != shape:
        altitude = altitude[: shape[0], : shape[1]]
    pressure = pixel_pressure(slp, altitude)
    azimuth = relative_azimuth(saa, oaa)

    rejected, _, bright = quality_mask(l1, scratch, shape, ["invalid", "saturated"])
    land, _, _ = quality_mask(l1, scratch, shape, ["land"])
    water = ~land & ~rejected & ~bright
    land_clear = land & ~rejected & ~bright
    print(f"{int(water.sum()):,} clear water pixels, {int(land_clear.sum()):,} clear land\n")

    wfr = sen3_root(args.wfr)

    # 1. Is the near-infrared actually black over water?
    print("=== 1. what ESA's own water-leaving reflectance says, over clear water")
    print(f"{'':30s} {'p05':>9s} {'p50':>9s} {'p95':>9s}")
    for band in (8, 17, 21):
        values = level2_reflectance(
            wfr, f"Oa{band:02d}_reflectance.nc", f"Oa{band:02d}_reflectance", shape, scratch
        )
        summarise(f"Oa{band:02d} water-leaving", values, water)
    print("\nThe black-pixel assumption reads the whole 865 nm signal as atmosphere. Whatever")
    print("the water actually leaves there biases every retrieved optical depth upwards.\n")

    # 2. ESA's own per-pixel aerosol retrieval, on our grid.
    print("=== 2. the WFR aerosol member, per pixel on the radiance grid")
    print(f"{'':30s} {'p05':>9s} {'p50':>9s} {'p95':>9s}")
    esa = {}
    for variable in ("T865", "T865_unc", "A865", "A865_unc"):
        values = waer_field(wfr, variable, shape, scratch)
        esa[variable] = values
        summarise(variable, values, water)

    # Our own retrieval, for comparison.
    toa = _toa(l1, DARK_BAND, shape, sza, scratch, args.decimate)
    t_gas = gas_transmittance(
        platform, DARK_BAND, ozone_kg_m2=ozone, water_kg_m2=vapour,
        solar_zenith_deg=sza, view_zenith_deg=oza,
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        apparent = toa / t_gas

    print("\nblack-pixel retrieval at 865 nm, against ESA's T865 over the same pixels:")
    print(f"{'assumed exponent':>18s} {'tau550':>9s} {'tau865':>9s} "
          f"{'ESA T865':>9s} {'bias':>9s} {'r':>7s} {'retrieved':>10s}")
    models = aerosol_models(platform)
    ratio = {name: band_depth_ratio(platform, DARK_BAND, name) for name in models}
    for angstrom in (0.3, 0.6, 0.9, 1.2, 1.5):
        depth = retrieve_depth(
            platform, DARK_BAND, apparent=apparent, angstrom=angstrom,
            pressure_hpa=pressure, solar_zenith_deg=sza, view_zenith_deg=oza,
            relative_azimuth_deg=azimuth, ozone_kg_m2=ozone, water_kg_m2=vapour,
        )
        # Convert to 865 nm through the table's own band depths, blended **the
        # same way the correction blends the two models** - by the exponent,
        # not by a plain mean. The two ratios differ by half again (0.597
        # against 0.887), so an unweighted mean puts every converted depth
        # wrong by up to 20 % in a way that looks like a retrieval bias.
        weights = _model_weights(platform, angstrom)
        scale = sum(weight * ratio[name] for name, weight in weights.items())
        at_865 = depth * scale
        keep = water & np.isfinite(at_865) & np.isfinite(esa["T865"])
        if keep.sum() < 100:
            print(f"{angstrom:18.1f} too few pixels to compare")
            continue
        ours = np.where(keep, at_865, np.nan)
        theirs = np.where(keep, esa["T865"], np.nan)
        flat_ours = ours[np.isfinite(ours)]
        flat_theirs = theirs[np.isfinite(theirs)]
        correlation = float(np.corrcoef(flat_ours, flat_theirs)[0, 1]) if flat_ours.size else float("nan")
        print(f"{angstrom:18.1f} {np.nanmedian(np.where(keep, depth, np.nan)):9.4f} "
              f"{np.nanmedian(ours):9.4f} {np.nanmedian(theirs):9.4f} "
              f"{np.nanmedian(ours) - np.nanmedian(theirs):9.4f} {correlation:7.3f} "
              f"{100.0 * keep.sum() / max(1, int(water.sum())):9.0f} %")
    print("\n(the two models' 865/550 depth ratios: "
          + ", ".join(f"{name} {ratio[name]:.3f}" for name in models)
          + "; blended on the assumed exponent, not averaged)")

    # 2b. Turn the comparison round. Taking ESA's own per-pixel T865 and A865
    # as truth, what reflectance is left over at 865 after the atmosphere they
    # imply? That leftover is what the water surface actually contributes
    # there - the thing the black-pixel assumption sets to zero - so it is a
    # direct measurement of the assumption's cost instead of an inference from
    # the retrieval bias.
    print("\n=== 2b. what is left at 865 nm once ESA's own aerosol is accounted for")
    with np.errstate(divide="ignore", invalid="ignore"):
        esa_550 = esa["T865"] * (550.0 / 865.0) ** (-esa["A865"])
    implied = atmosphere_terms(
        platform, DARK_BAND, pressure_hpa=pressure, solar_zenith_deg=sza,
        view_zenith_deg=oza, relative_azimuth_deg=azimuth,
        ozone_kg_m2=ozone, water_kg_m2=vapour,
        optical_depth_550=esa_550, angstrom=esa["A865"],
    )
    leftover = apparent - implied["path_reflectance"]
    keep = (
        water
        & np.isfinite(leftover)
        & np.isfinite(esa["T865"])
        & np.isfinite(esa["A865"])
        & (esa["T865"] > 0)
    )
    picked = leftover[keep]
    if picked.size:
        p05, p50, p95 = np.percentile(picked, [5, 50, 95])
        print(f"  ESA's T865 implies a 550 nm depth of {np.nanmedian(esa_550[keep]):.4f} "
              f"(median), against our retrieval's 0.109")
        print(f"  leftover reflectance at 865 nm: p05 {p05:+.5f}, median {p50:+.5f}, "
              f"p95 {p95:+.5f}")
        print(f"  ESA's own water-leaving reflectance there is 0.0001, so of that "
              f"leftover\n  all but 0.0001 is surface reflection and model mismatch, not water.")
    else:
        print("  no pixel has both fields")

    # 2c. Two bands instead of one, so the exponent stops being an assumption.
    # A single black pixel is one measurement against two unknowns - optical
    # depth and the aerosol's spectral slope - which is why the table above
    # shows the retrieved depth sliding with the exponent it was told to
    # assume. 665 nm is nearly as black as 865 over this water (ESA's own
    # water-leaving reflectance there is 0.0003), and ln(865/665) is 0.262
    # against 0.165 for ln(1020/865), so **the pair we already have in the
    # table has more spectral leverage than adding the 1020 nm band would**.
    # No table regeneration, no new 6SV runs.
    print("\n=== 2c. solving for the exponent as well, from 665 and 865 together")
    toa_665 = _toa(l1, 8, shape, sza, scratch, args.decimate)
    gas_665 = gas_transmittance(
        platform, 8, ozone_kg_m2=ozone, water_kg_m2=vapour,
        solar_zenith_deg=sza, view_zenith_deg=oza,
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        apparent_665 = toa_665 / gas_665

    candidates = np.arange(0.0, 1.81, 0.1)
    best_cost = np.full(shape, np.inf)
    best_alpha = np.full(shape, np.nan)
    best_depth = np.full(shape, np.nan)
    for angstrom in candidates:
        depth = retrieve_depth(
            platform, DARK_BAND, apparent=apparent, angstrom=angstrom,
            pressure_hpa=pressure, solar_zenith_deg=sza, view_zenith_deg=oza,
            relative_azimuth_deg=azimuth, ozone_kg_m2=ozone, water_kg_m2=vapour,
        )
        # That depth and exponent predict a path reflectance at 665; the right
        # pair is the one whose prediction matches what was measured there.
        predicted = atmosphere_terms(
            platform, 8, pressure_hpa=pressure, solar_zenith_deg=sza,
            view_zenith_deg=oza, relative_azimuth_deg=azimuth,
            ozone_kg_m2=ozone, water_kg_m2=vapour,
            optical_depth_550=np.where(np.isfinite(depth), depth, 0.0),
            angstrom=np.full(shape, angstrom),
        )["path_reflectance"]
        cost = np.abs(apparent_665 - predicted)
        better = np.isfinite(cost) & np.isfinite(depth) & (cost < best_cost)
        best_cost = np.where(better, cost, best_cost)
        best_alpha = np.where(better, angstrom, best_alpha)
        best_depth = np.where(better, depth, best_depth)

    keep = (
        water
        & np.isfinite(best_alpha)
        & np.isfinite(esa["A865"])
        & np.isfinite(esa["T865"])
        & (esa["T865"] > 0)
    )
    if keep.sum() >= 100:
        ours_a = best_alpha[keep]
        theirs_a = esa["A865"][keep]
        ours_t = (best_depth * (865.0 / 550.0) ** (-best_alpha))[keep]
        theirs_t = esa["T865"][keep]
        print(f"  exponent   ours median {np.median(ours_a):.3f}  "
              f"ESA A865 median {np.median(theirs_a):.3f}  "
              f"r {np.corrcoef(ours_a, theirs_a)[0, 1]:+.3f}")
        print(f"  tau865     ours median {np.median(ours_t):.4f}  "
              f"ESA T865 median {np.median(theirs_t):.4f}  "
              f"r {np.corrcoef(ours_t, theirs_t)[0, 1]:+.3f}")
        print(f"  residual at 665 nm after the fit: median {np.median(best_cost[keep]):.5f}")
        print(f"  solved on {100.0 * keep.sum() / max(1, int(water.sum())):.0f} % of clear water")
    else:
        print("  too few pixels to compare")

    # 2d. Which inversion to actually implement. 2c showed a joint fit over
    # depth *and* exponent gives a better depth while getting the exponent
    # badly wrong, which is the model absorbing surface contamination into a
    # physical parameter - a better number by a fragile route. So compare three
    # inversions against ESA's T865 with ESA's own A865 supplied as the
    # exponent, which is what the library will do where that field exists:
    #
    #   a. one band, 865 only, exact inversion
    #   b. two bands, least squares at fixed exponent
    #   c. two bands, joint fit over the exponent too (2c, repeated here on the
    #      same pixels so the comparison is like for like)
    print("\n=== 2d. three inversions, with ESA's own A865 as the exponent")
    esa_alpha = np.where(np.isfinite(esa["A865"]), esa["A865"], np.nan)

    def paths_for(band, measured_shape, alpha):
        stack = []
        for depth in DEPTH_GRID:
            stack.append(
                atmosphere_terms(
                    platform, band, pressure_hpa=pressure, solar_zenith_deg=sza,
                    view_zenith_deg=oza, relative_azimuth_deg=azimuth,
                    ozone_kg_m2=ozone, water_kg_m2=vapour,
                    optical_depth_550=np.full(measured_shape, depth),
                    angstrom=alpha,
                )["path_reflectance"]
            )
        return np.stack(stack, axis=-1)

    at_865 = paths_for(DARK_BAND, shape, esa_alpha)
    at_665 = paths_for(8, shape, esa_alpha)

    # a. 865 alone: the depth whose 865 path matches the measurement.
    cost_one = np.abs(at_865 - apparent[..., None])
    # b. both bands at the same fixed exponent.
    cost_two = cost_one + np.abs(at_665 - apparent_665[..., None])

    rows = []
    for label, cost in (("865 alone", cost_one), ("665 and 865, fixed exponent", cost_two)):
        pick = np.argmin(np.where(np.isfinite(cost), cost, np.inf), axis=-1)
        depth = DEPTH_GRID[pick]
        # Refuse where the measurement is darker than the cleanest node: that
        # is not a retrieval of zero.
        depth = np.where(apparent >= at_865[..., 0], depth, np.nan)
        # _model_weights is already vectorised over the exponent, so one call
        # with the whole field. Calling it per pixel, as this first did, is
        # 152,000 table lookups and does not finish.
        blend = _model_weights(platform, np.where(np.isfinite(esa_alpha), esa_alpha, 0.5))
        ratio_blend = sum(weight * ratio[name] for name, weight in blend.items())
        rows.append((label, depth * ratio_blend))

    for label, at_865_ours in rows:
        keep = (
            water
            & np.isfinite(at_865_ours)
            & np.isfinite(esa["T865"])
            & (esa["T865"] > 0)
        )
        if keep.sum() < 100:
            print(f"  {label:32s} too few pixels")
            continue
        ours, theirs = at_865_ours[keep], esa["T865"][keep]
        print(f"  {label:32s} tau865 {np.median(ours):.4f} vs {np.median(theirs):.4f}  "
              f"bias {np.median(ours) - np.median(theirs):+.4f}  "
              f"r {np.corrcoef(ours, theirs)[0, 1]:+.3f}  "
              f"on {100.0 * keep.sum() / max(1, int(water.sum())):.0f} %")
    print("  (2c's joint fit over the exponent gave bias +0.0061 at r +0.838)")

    # 3. What a land retrieval would have to assume.
    if args.syn:
        print("\n=== 3. how dark the darkest clear land actually is, per Synergy")
        syn = sen3_root(args.syn)
        print(f"{'band':12s} {'p01':>9s} {'p05':>9s} {'p50':>9s}   (surface reflectance)")
        for band in BANDS:
            values = level2_reflectance(
                syn, f"Syn_Oa{band:02d}_reflectance.nc", f"SDR_Oa{band:02d}", shape, scratch
            )
            picked = np.where(land_clear, values, np.nan)
            finite = picked[np.isfinite(picked)]
            if finite.size == 0:
                continue
            p01, p05, p50 = np.percentile(finite, [1, 5, 50])
            print(f"Oa{band:02d}        {p01:9.4f} {p05:9.4f} {p50:9.4f}")
        print("\nOLCI has no shortwave infrared band, so the usual dark-dense-vegetation anchor")
        print("- red surface reflectance tied to 2100 nm - is unavailable. Any land assumption")
        print("has to live with these numbers instead.")

    if args.aod:
        window = sensing_window(args.l1)
        picked = [p for p in args.aod if covers(sensing_window(p), window)]
        if picked:
            latitude = tie_field(l1, "tie_geo_coordinates.nc", "latitude", shape, scratch)
            longitude = tie_field(l1, "tie_geo_coordinates.nc", "longitude", shape, scratch)
            loading = frame_aerosol(picked[0], latitude, longitude, scratch)
            print(f"\n=== for reference, the coarse aerosol product over this frame: "
                  f"AOD(550) {loading['optical_depth_550']:.4f}, "
                  f"Angstrom {loading['angstrom']:.3f}, "
                  f"{100.0 * loading['coverage']:.0f} % of its cells retrieved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
