"""Phase 6's physical numbers, before and after the solar-flux fix (item R1).

Phase 6 of the Sentinel-5P/Sentinel-3 plan accepted OLCI true colour on a
physical plausibility check: open water at 665 nm between 0.04 and 0.25, no
value above 1.0 over water, and every over-unity pixel flagged ``bright``.
Those numbers were measured with each band divided by the wrong band's solar
flux, so they have to be measured again - and the ``0 - 0.6, gamma 0.6`` style
was chosen on pictures that were 2.1 times too blue.

This reports both arithmetics side by side on a real frame, so the owner can
see what the fix moved.

    python3 truecolor_numbers.py PRODUCT.zip [--decimate 8]
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

from pysent._olci_l1 import (  # noqa: E402
    flux_row_for_band,
    quality_mask,
    radiance_member,
    solar_flux_per_pixel,
    solar_zenith,
)
from pysent._sen3 import extract_member, open_sen3_variable, read_packed, sen3_root  # noqa: E402

TRUECOLOR_BANDS = {"R (Oa08, 665 nm)": 8, "G (Oa06, 560 nm)": 6, "B (Oa04, 490 nm)": 4}


def old_flux_row(band: int) -> int:
    """What `main` did: the row index is the band number minus one."""
    return band - 1


def reflectance(root, scratch, band, decimate, zenith, *, row_chooser):
    """One band's TOA reflectance, with the flux row chosen by ``row_chooser``."""
    member, variable = radiance_member(band)
    dataset = open_sen3_variable(extract_member(root, member, scratch), variable)
    radiance = read_packed(dataset.GetRasterBand(1), decimate=decimate, log10=False)
    dataset = None

    local = extract_member(root, "instrument_data.nc", scratch)
    dataset = open_sen3_variable(local, "solar_flux")
    table = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    dataset = open_sen3_variable(local, "lambda0")
    wavelengths = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    dataset = open_sen3_variable(local, "detector_index")
    detector = dataset.GetRasterBand(1).ReadAsArray(
        buf_xsize=radiance.shape[1], buf_ysize=radiance.shape[0]
    )
    dataset = None

    row = row_chooser(wavelengths, band)
    fluxes = table[row]
    inside = (detector >= 0) & (detector < fluxes.size)
    flux = np.where(inside, fluxes[np.clip(detector, 0, fluxes.size - 1)], np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        cosine = np.cos(np.radians(zenith))
        values = (np.pi * radiance) / (flux * cosine)
        values = np.where(cosine > 0, values, np.nan)
    return values.astype(np.float32), float(np.nanmedian(fluxes)), float(np.nanmedian(wavelengths[row]))


def describe(label, values, water, bright):
    finite = np.isfinite(values)
    over = finite & (values > 1.0)
    water_values = values[water & finite]
    line = f"  {label:22s}"
    if water_values.size:
        p01, p50, p99 = np.percentile(water_values, [1, 50, 99])
        line += f" water p01/p50/p99 {p01:6.3f} {p50:6.3f} {p99:6.3f}"
    else:
        line += " water: none"
    line += f"   >1.0 {100 * over.sum() / max(1, finite.sum()):5.2f} %"
    if over.any():
        line += f"  of which bright {100 * (over & bright).sum() / over.sum():5.1f} %"
    print(line)
    return {"p50": float(np.nanmedian(values[finite])) if finite.any() else float("nan")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("product")
    parser.add_argument("--decimate", type=int, default=8)
    args = parser.parse_args()

    scratch = pathlib.Path(tempfile.mkdtemp(prefix="truecolor-numbers-"))
    root = sen3_root(args.product)

    member, variable = radiance_member(8)
    dataset = open_sen3_variable(extract_member(root, member, scratch), variable)
    probe = read_packed(dataset.GetRasterBand(1), decimate=args.decimate, log10=False)
    dataset = None
    shape = probe.shape
    zenith = solar_zenith(root, scratch, shape)

    # "Open water" the way the Phase 6 check defined it: not land, not bright.
    land, _missing, bright = quality_mask(root, scratch, shape, ["land"])
    water = ~land & ~bright
    print(f"{pathlib.Path(args.product).name}")
    print(f"  frame {shape[0]} x {shape[1]} at decimate {args.decimate}, "
          f"water {100 * water.mean():.1f} %, bright {100 * bright.mean():.1f} %\n")

    for arithmetic, chooser in (
        ("main (row = band - 1)", lambda w, b: old_flux_row(b)),
        ("fixed (row by lambda0)", flux_row_for_band),
    ):
        print(f"{arithmetic}:")
        medians = {}
        for label, band in TRUECOLOR_BANDS.items():
            values, flux, wavelength = reflectance(
                root, scratch, band, args.decimate, zenith, row_chooser=chooser
            )
            statistics = describe(label, values, water, bright)
            medians[label[0]] = statistics["p50"]
            print(f"  {'':22s} flux used {flux:7.1f} mW m-2 nm-1 at lambda0 {wavelength:6.1f} nm")
        print(f"  whole-frame medians R/G/B: "
              f"{medians['R']:.3f} / {medians['G']:.3f} / {medians['B']:.3f}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
