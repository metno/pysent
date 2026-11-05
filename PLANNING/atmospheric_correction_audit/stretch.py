"""What stretch a corrected true-colour picture needs - the plan's Q6.

The owner's answer to Q6 is a separate stretch per mode: the frozen
``(0.0, 0.6)`` with ``gamma 0.6`` stays exactly as it is for top-of-atmosphere,
and corrected mode gets its own, measured here.

**The criterion has to be stated before the numbers, or the numbers are just a
preference.** Two conditions pin the two unknowns:

1. **Clear land keeps the tone it had.** The correction's job is to take the
   haze out, not to darken the picture. So the display value of the median
   clear-land pixel should come out roughly where the frozen stretch put it -
   otherwise every user reads a correct picture as an under-exposed one and
   reaches for the style options.
2. **Cloud still lands near white without clipping.** The frozen range was
   chosen to put cloud "near white", and clipping the top of a composite throws
   away the only part of the scene that is reliably bright. So the frame's own
   bright p99 maps to about 0.95 of full scale.

Two conditions, two unknowns - the range's top and the gamma - so the pair is
determined rather than chosen, and the only judgement left is the 0.95.

Measured across frames rather than one, because the whole reason Q6 was asked
is that the frozen numbers came from a single look at pictures that turned out
to be 2.1x too blue.
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

from budget import BANDS, _toa, scratch_dir, tie_field  # noqa: E402
from pairs import collect  # noqa: E402

from pysent._atmosphere import (  # noqa: E402
    atmosphere_terms,
    pixel_pressure,
    platform_of,
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

# The frozen style, which this script must not change and compares against.
FROZEN_RANGE = (0.0, 0.6)
FROZEN_GAMMA = 0.6

# How bright the brightest part of the scene should render. Not 1.0: a composite
# that drives its own p99 to full scale has no headroom left for the pixels
# above it, and cloud tops vary more between frames than within one.
BRIGHT_TARGET = 0.95

#: Below this many clear pixels a median says nothing.
MINIMUM_PIXELS = 20_000


def display(value, low, high, gamma):
    """What the RGB writer makes of a reflectance, as a 0-1 fraction."""
    scaled = np.clip((np.asarray(value, dtype=np.float64) - low) / (high - low), 0.0, 1.0)
    return scaled ** gamma


def frame_distribution(l1_path, decimate, aerosol="climatological"):
    """Percentiles of the top-of-atmosphere and corrected composite, one frame.

    The aerosol tier uses the stated climatological loading rather than a
    retrieval, deliberately: the stretch has to work on any frame the option is
    turned on for, including the ones where the retrieval refuses, so measuring
    it on the best-case loading would set it for the wrong population.
    """
    scratch = scratch_dir("ac-stretch-")
    try:
        root = sen3_root(str(l1_path))
        platform = platform_of(str(l1_path))

        member, variable = radiance_member(BANDS[0])
        probe = open_sen3_variable(
            extract_member(root, find_member(root, member), scratch), variable
        )
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

        geometry = dict(
            pressure_hpa=pixel_pressure(slp, altitude),
            solar_zenith_deg=sza, view_zenith_deg=oza,
            relative_azimuth_deg=relative_azimuth(saa, oaa),
            ozone_kg_m2=ozone, water_kg_m2=vapour,
        )
        rejected, _, bright = quality_mask(root, scratch, shape, ["invalid", "saturated"])
        land, _, _ = quality_mask(root, scratch, shape, ["land"])
        clear = ~rejected & ~bright
        clear_land = clear & land

        from pysent._aerosol_retrieval import CLIMATOLOGICAL_ANGSTROM, CLIMATOLOGICAL_DEPTH_550

        depth = np.full(shape, CLIMATOLOGICAL_DEPTH_550)
        angstrom = np.full(shape, CLIMATOLOGICAL_ANGSTROM)

        toa_bands, corrected_bands = {}, {}
        for band in BANDS:
            toa = _toa(root, band, shape, sza, scratch, decimate)
            toa_bands[band] = np.where(rejected, np.nan, toa)
            terms = atmosphere_terms(
                platform, band, optical_depth_550=depth, angstrom=angstrom, **geometry
            )
            corrected_bands[band] = np.where(rejected, np.nan, surface_from_toa(toa, **terms))

        # The composite's brightness is the three bands together: a stretch set
        # from one band would be right for that band and wrong for the picture.
        def stack(bands):
            return np.stack([bands[band] for band in BANDS])

        def summarise(bands, where):
            values = stack(bands)[:, where]
            values = values[np.isfinite(values)]
            if values.size < MINIMUM_PIXELS:
                return None
            return {
                "p50": float(np.percentile(values, 50)),
                "p99": float(np.percentile(values, 99)),
            }

        bright_values = stack(toa_bands)[:, bright & ~rejected]
        bright_values = bright_values[np.isfinite(bright_values)]
        corrected_bright = stack(corrected_bands)[:, bright & ~rejected]
        corrected_bright = corrected_bright[np.isfinite(corrected_bright)]

        return {
            "name": pathlib.Path(l1_path).name,
            "solar_zenith": float(np.nanmedian(sza)),
            "clear_land_pixels": int(np.count_nonzero(clear_land)),
            "bright_fraction": float(np.mean(bright[~rejected])) if (~rejected).any() else 0.0,
            "toa_land": summarise(toa_bands, clear_land),
            "corrected_land": summarise(corrected_bands, clear_land),
            "toa_water": summarise(toa_bands, clear & ~land),
            "corrected_water": summarise(corrected_bands, clear & ~land),
            "toa_bright_p99": (
                float(np.percentile(bright_values, 99)) if bright_values.size else None
            ),
            "corrected_bright_p99": (
                float(np.percentile(corrected_bright, 99)) if corrected_bright.size else None
            ),
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# A corrected reflectance above one is not a brighter surface, it is the
# inversion applied where its own assumptions do not hold: the correction places
# the surface at ground level under the whole atmosphere, and a cloud top is
# neither. So one is the natural top of the ramp - it is the physical ceiling of
# a reflectance, and everything above it is already outside the model.
REFLECTANCE_CEILING = 1.0


def solve_on_bright(land_median, bright_p99, *, frozen_land_median):
    """The (high, gamma) pair that puts land where it was and cloud at 0.95.

    This was the first criterion and it is kept for comparison, because the
    measurement is what argues against it. Two conditions, two unknowns:

        (land/high)**g = target_land
        (p99/high)**g  = BRIGHT_TARGET

    The ratio eliminates ``high``, so ``g`` comes out of the logs. **The flaw is
    in the second condition**: the corrected bright p99 measures out around 1.0
    and ranges to 1.10 across frames, because those are cloud pixels the
    correction's assumptions do not cover. Anchoring the ramp on them sets the
    whole picture's tone from the one part of it that is not a retrieval.
    """
    target_land = display(frozen_land_median, *FROZEN_RANGE, FROZEN_GAMMA)
    if not (0 < land_median < bright_p99) or not 0 < target_land < BRIGHT_TARGET:
        return None
    gamma = np.log(target_land / BRIGHT_TARGET) / np.log(land_median / bright_p99)
    high = bright_p99 / BRIGHT_TARGET ** (1.0 / gamma)
    return float(high), float(gamma), float(target_land)


def solve_on_ceiling(land_median, *, frozen_land_median):
    """The gamma that keeps clear land's tone, with the ramp's top at one.

    One condition and one unknown, because the top is no longer being fitted:
    it is a reflectance of 1.0, the physical ceiling. That removes the
    dependence on cloud entirely, which is the point - the stretch should serve
    the pixels the correction is valid for and merely not clip the ones it is
    not.
    """
    target_land = display(frozen_land_median, *FROZEN_RANGE, FROZEN_GAMMA)
    if not 0 < land_median < REFLECTANCE_CEILING or not 0 < target_land < 1:
        return None
    gamma = np.log(target_land) / np.log(land_median / REFLECTANCE_CEILING)
    return REFLECTANCE_CEILING, float(gamma), float(target_land)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default="S3A")
    parser.add_argument("--months", nargs="+", default=["2026/11"])
    parser.add_argument("--decimate", type=int, default=8)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--paths", nargs="*", default=[])
    arguments = parser.parse_args()

    if arguments.paths:
        candidates = [pathlib.Path(path) for path in arguments.paths]
    else:
        found = collect(arguments.platform, arguments.months)
        candidates = [
            products["l1"] for _key, products in sorted(found.items()) if "l1" in products
        ]
    candidates = candidates[: arguments.limit]
    if not candidates:
        print("no Level-1 frames found")
        return 1

    print(f"{len(candidates)} frame(s), decimate {arguments.decimate}\n")
    header = (
        f"{'frame':<34} {'SZA':>5} {'bright':>7} "
        f"{'TOA land':>9} {'corr land':>10} {'TOA br p99':>11} {'corr br p99':>12}"
    )
    print(header)
    print("-" * len(header))

    rows = []
    for path in candidates:
        try:
            row = frame_distribution(path, arguments.decimate)
        except Exception as error:                       # noqa: BLE001
            print(f"{pathlib.Path(path).name[:34]:<34} failed: {type(error).__name__}: {error}")
            continue
        if row["toa_land"] is None or row["corrected_land"] is None:
            print(
                f"{row['name'][:34]:<34} {row['solar_zenith']:5.1f} "
                f"{row['bright_fraction']:6.1%}  too little clear land"
            )
            continue
        rows.append(row)
        print(
            f"{row['name'][:34]:<34} {row['solar_zenith']:5.1f} {row['bright_fraction']:6.1%} "
            f"{row['toa_land']['p50']:9.4f} {row['corrected_land']['p50']:10.4f} "
            f"{(row['toa_bright_p99'] or float('nan')):11.4f} "
            f"{(row['corrected_bright_p99'] or float('nan')):12.4f}"
        )

    if not rows:
        print("\nnothing measurable")
        return 1

    land_toa = float(np.median([row["toa_land"]["p50"] for row in rows]))
    land_corr = float(np.median([row["corrected_land"]["p50"] for row in rows]))
    bright_corr = [row["corrected_bright_p99"] for row in rows if row["corrected_bright_p99"]]
    bright_toa = [row["toa_bright_p99"] for row in rows if row["toa_bright_p99"]]

    print(f"\nover {len(rows)} frame(s), medians:")
    print(f"  clear-land composite, TOA        {land_toa:.4f}")
    print(f"  clear-land composite, corrected  {land_corr:.4f}  "
          f"({land_corr / land_toa - 1:+.0%})")
    if bright_toa:
        print(f"  bright p99, TOA                  {float(np.median(bright_toa)):.4f}")
    if bright_corr:
        print(f"  bright p99, corrected            {float(np.median(bright_corr)):.4f}")

    frozen_land_display = display(land_toa, *FROZEN_RANGE, FROZEN_GAMMA)
    held = display(land_corr, *FROZEN_RANGE, FROZEN_GAMMA)
    print(f"\nwith the frozen stretch ({FROZEN_RANGE[0]:g}-{FROZEN_RANGE[1]:g}, "
          f"gamma {FROZEN_GAMMA:g}):")
    print(f"  TOA land renders at              {frozen_land_display:.3f} of full scale")
    print(f"  corrected land would render at   {held:.3f}  "
          f"({held / frozen_land_display - 1:+.0%}) <- the darkening Q6 is about")

    if not bright_corr:
        return 0
    bright_median = float(np.median(bright_corr))
    print(f"\ncorrected bright p99 across frames: "
          f"{min(bright_corr):.3f} to {max(bright_corr):.3f}")
    print("  above one, because the correction places the surface at ground level")
    print("  under the whole atmosphere and a cloud top is neither - so anchoring")
    print("  the ramp here would set the picture's tone from its least valid part.")

    rejected_answer = solve_on_bright(land_corr, bright_median, frozen_land_median=land_toa)
    answer = solve_on_ceiling(land_corr, frozen_land_median=land_toa)

    if rejected_answer:
        high, gamma, _target = rejected_answer
        print(f"\nanchored on cloud at {BRIGHT_TARGET:g} (rejected): "
              f"(0.0, {high:.3f}), gamma {gamma:.3f}")
    if answer:
        high, gamma, target = answer
        print("\nanchored on the reflectance ceiling, keeping clear land's tone:")
        print(f"  value_range  (0.0, {high:.3f})")
        print(f"  gamma        {gamma:.3f}")
        print(f"  land renders at {display(land_corr, 0.0, high, gamma):.3f} "
              f"against the frozen {target:.3f}")
        print(f"  bright p99 renders at {display(bright_median, 0.0, high, gamma):.3f}")
        print("\nrounded for a default, and what the rounding costs:")
        for candidate in (round(gamma, 2), round(gamma * 20) / 20, round(gamma, 1)):
            rendered = display(land_corr, 0.0, high, candidate)
            print(
                f"  gamma {candidate:g}: land {rendered:.3f} "
                f"({rendered / target - 1:+.1%} against the frozen tone), "
                f"bright {display(bright_median, 0.0, high, candidate):.3f}"
            )
        print("\nper-frame land tone at the rounded default, to show the spread:")
        for row in rows:
            own = display(row["corrected_land"]["p50"], 0.0, high, round(gamma, 2))
            frozen = display(row["toa_land"]["p50"], *FROZEN_RANGE, FROZEN_GAMMA)
            print(
                f"  {row['name'][:34]:<34} corrected {own:.3f} vs frozen TOA "
                f"{frozen:.3f} ({own / frozen - 1:+.0%})"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
