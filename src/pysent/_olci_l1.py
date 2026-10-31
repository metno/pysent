"""Top-of-atmosphere reflectance from OLCI Level-1 radiance.

An OLCI Level-1 product holds **radiance**, not reflectance, despite what the
catalogue abstract says. Turning one into the other is the whole of this module:

    rho = pi * L / (F0 * cos(theta))

with ``L`` the measured radiance, ``F0`` the solar irradiance for that band and
detector, and ``theta`` the solar zenith angle. No Earth-Sun distance term: the
solar flux shipped in the product is already seasonally corrected.

Three details make it more than one line of arithmetic, and each is a place to
get it quietly wrong:

* **The solar flux is per band and per detector**, a 21 x 3700 table, and every
  pixel names its own detector in ``detector_index``. A single band-average
  flux would be wrong by the detector-to-detector variation, about 1 % here.
  **And the table's rows do not arrive in band order.** GDAL's netCDF driver
  hands a non-geographic 2-D array back bottom-up, so row 0 holds band 21:
  ``lambda0`` read straight off the file runs 1016, 939, 899 ... 442, 412,
  400 nm. Taking row ``band - 1`` divides each band by the flux of band
  ``22 - band`` - blue by 2.10 times too little, green by 1.56 and red by 1.27,
  a strong blue cast on every picture. The row is matched by wavelength
  instead; see :func:`flux_row_for_band`.
* **The solar zenith angle is on a tie-point grid** of 77 columns against the
  image's 4865, so it has to be interpolated across the track. Using it
  unscaled or unexpanded puts the whole scene on the wrong illumination.
* **The result is not bounded by one.** Over open water that is not flagged
  bright, measured reflectance at 665 nm runs 0.04 to 0.25 and never exceeds 1.
  Over cloud it reaches 1.6. Cloud really can exceed unity in forward
  scattering, and bright-edge effects add more, so the ceiling is not enforced -
  a picture clips it, and a caller who wants physical values gets the honest
  number plus the flag that says the pixel is bright.

Measured on a real frame after the flux rows were matched by wavelength: over
open water at 665 nm the reflectance runs 0.032 / 0.045 / 0.211 (p1 / p50 /
p99), the whole-frame medians are 0.174 / 0.188 / 0.220 for red / green / blue
- bluer than red, which is what Rayleigh scattering does to a top-of-atmosphere
spectrum - and 0.25 % of pixels exceed one, every one of them flagged
``bright``. With the rows taken as ``band - 1`` the same frame gave 0.275 over
water in blue and 23.7 % of blue pixels above one.

What this module cannot do is validate itself against another product. The
Level-2 reflectance of the same scene is **water-leaving** reflectance, after
atmospheric correction: on a matched pair its median is 0.0002 against 0.055
here, it is negative on 28 % of its valid pixels, and the two correlate at
-0.06. They are different physical quantities and comparing them says nothing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from ._sen3 import extract_member, open_sen3_variable, read_flag_table, read_packed

# Members a reflectance conversion needs beyond the radiance band itself.
INSTRUMENT_MEMBER = "instrument_data.nc"
GEOMETRY_MEMBER = "tie_geometries.nc"
QUALITY_MEMBER = "qualityFlags.nc"
QUALITY_VARIABLE = "quality_flags"

# OLCI band centres, for naming and for picking a true-colour triple.
BAND_WAVELENGTHS: dict[int, int] = {
    1: 400, 2: 412, 3: 442, 4: 490, 5: 510, 6: 560, 7: 620, 8: 665,
    9: 674, 10: 681, 11: 709, 12: 754, 13: 761, 14: 764, 15: 767,
    16: 779, 17: 865, 18: 885, 19: 900, 20: 940, 21: 1020,
}


def radiance_member(band: int) -> tuple[str, str]:
    """The member and variable holding one band's radiance."""
    if band not in BAND_WAVELENGTHS:
        raise ValueError(f"band must be 1 to 21, not {band}")
    return f"Oa{band:02d}_radiance.nc", f"Oa{band:02d}_radiance"


def solar_zenith(root: str, scratch: Path, shape: tuple[int, int]) -> np.ndarray:
    """Solar zenith angle in degrees, expanded from its tie-point grid.

    The grid is a few dozen columns wide against the image's thousands, so each
    row is interpolated across the track. Rows are not interpolated: the tie
    grid has one row per image row.
    """
    rows, columns = shape
    dataset = open_sen3_variable(extract_member(root, GEOMETRY_MEMBER, scratch), "SZA")
    band = dataset.GetRasterBand(1)
    # The tie grid has one row per image row, so a decimated image needs a
    # decimated grid: read it at the row count being worked at rather than
    # reading it whole and trying to match afterwards.
    tie = read_packed(band, decimate=max(1, band.YSize // max(1, rows)), log10=False)
    dataset = None

    if tie.shape[0] != rows:
        # A decimation that does not divide evenly leaves a row or two over;
        # trim or pad by repeating the edge rather than failing on arithmetic.
        if tie.shape[0] > rows:
            tie = tie[:rows]
        else:
            tie = np.vstack([tie, np.repeat(tie[-1:], rows - tie.shape[0], axis=0)])
    if tie.shape[1] == columns:
        return tie.astype(np.float32)

    tie_columns = np.linspace(0, columns - 1, tie.shape[1])
    target = np.arange(columns)
    expanded = np.empty((rows, columns), dtype=np.float32)
    for row in range(rows):
        expanded[row] = np.interp(target, tie_columns, tie[row])
    return expanded


def flux_row_for_band(wavelengths: np.ndarray, band: int, *, tolerance_nm: float = 12.0) -> int:
    """Which row of a band-indexed table belongs to ``band``.

    **Never ``band - 1``.** GDAL's netCDF driver hands a 21 x 3700 table back
    bottom-up, so row 0 holds band 21: read straight off the file, row 0 of
    ``lambda0`` is 1016 nm, not the 400 nm of band 1. Indexing by ``band - 1``
    therefore divides every band by the solar flux of band ``22 - band`` - blue
    by 2.10 times too little, green by 1.56 and red by 1.27, which is a strong
    blue cast on every picture.

    The row is found instead by matching the table's own ``lambda0`` to the
    band's nominal wavelength, which is right whichever way round the driver
    hands the table over and does not depend on a config option a caller might
    change. ``lambda0`` varies slightly per detector, so the match is against
    each row's median.

    The hazard is specific to an array whose row index means *which band*
    rather than *which position*: the image bands, ``detector_index``, the tie
    grids and the geolocation are position-indexed and flip together, so they
    stay mutually consistent and a flip there would show as an upside-down
    picture.
    """
    nominal = BAND_WAVELENGTHS.get(int(band))
    if nominal is None:
        raise ValueError(f"band must be 1 to 21, not {band}")
    centres = np.nanmedian(np.asarray(wavelengths, dtype=np.float64), axis=1)
    row = int(np.nanargmin(np.abs(centres - nominal)))
    if not np.isfinite(centres[row]) or abs(centres[row] - nominal) > tolerance_nm:
        raise ValueError(
            f"no row of this product's lambda0 is within {tolerance_nm:g} nm of band {band}'s "
            f"{nominal} nm (nearest is row {row} at {centres[row]:.1f} nm), so the solar flux "
            "cannot be matched to the band"
        )
    return row


def solar_flux_per_pixel(root: str, scratch: Path, band: int, shape: tuple[int, int]) -> np.ndarray:
    """Solar irradiance for each pixel of one band, via its detector index.

    The flux table is ``[band, detector]`` and every pixel records which
    detector saw it, so the lookup is per pixel. Pixels with no detector get
    NaN rather than a neighbour's flux.

    Which row of the table is this band's is decided by
    :func:`flux_row_for_band`, not by the band number - see there for why.
    """
    local = extract_member(root, INSTRUMENT_MEMBER, scratch)
    dataset = open_sen3_variable(local, "solar_flux")
    table = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    dataset = open_sen3_variable(local, "lambda0")
    wavelengths = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    dataset = open_sen3_variable(local, "detector_index")
    # Read at the shape being worked at: the detector index is a full-resolution
    # array, and a decimated band needs the indices of the pixels it kept.
    # Nearest sampling, since a detector number is a label and an average of
    # two of them names a detector that saw neither pixel.
    detector = dataset.GetRasterBand(1).ReadAsArray(buf_xsize=shape[1], buf_ysize=shape[0])
    dataset = None

    if detector.shape != shape:
        raise ValueError(f"detector_index is {detector.shape} but the band is {shape}")
    if not 1 <= band <= table.shape[0]:
        raise ValueError(f"the flux table has {table.shape[0]} bands, so band {band} is out of range")
    if wavelengths.shape != table.shape:
        raise ValueError(
            f"lambda0 is {wavelengths.shape} but solar_flux is {table.shape}; they must be the "
            "same table shape for a row of one to name a row of the other"
        )

    row = table[flux_row_for_band(wavelengths, band)]
    inside = (detector >= 0) & (detector < row.size)
    return np.where(inside, row[np.clip(detector, 0, row.size - 1)], np.nan).astype(np.float32)


def toa_reflectance(
    root: str,
    band: int,
    scratch: Path,
    *,
    decimate: int = 1,
    solar_zenith_degrees: np.ndarray | None = None,
) -> np.ndarray:
    """One band as top-of-atmosphere reflectance.

    ``solar_zenith_degrees`` lets several bands of one scene share a single
    expansion of the tie grid, which is the expensive part.
    """
    member, variable = radiance_member(band)
    dataset = open_sen3_variable(extract_member(root, member, scratch), variable)
    radiance = read_packed(dataset.GetRasterBand(1), decimate=decimate, log10=False)
    dataset = None

    flux = solar_flux_per_pixel(root, scratch, band, radiance.shape)
    if solar_zenith_degrees is None:
        solar_zenith_degrees = solar_zenith(root, scratch, radiance.shape)
    if solar_zenith_degrees.shape != radiance.shape:
        raise ValueError(
            f"the solar zenith angle is {solar_zenith_degrees.shape} but the band is {radiance.shape}"
        )

    with np.errstate(invalid="ignore", divide="ignore"):
        cosine = np.cos(np.radians(solar_zenith_degrees.astype(np.float32)))
        reflectance = (np.pi * radiance) / (flux * cosine)
        # A non-positive cosine means the sun is at or below the horizon, where
        # a reflectance has no meaning.
        reflectance = np.where(cosine > 0, reflectance, np.nan)
    return reflectance.astype(np.float32)


def quality_mask(
    root: str,
    scratch: Path,
    shape: tuple[int, int],
    reject: list[str],
) -> tuple[np.ndarray, list[str], np.ndarray]:
    """Pixels to drop, the names this product does not define, and its bright mask.

    The bright mask comes back separately because it is not a rejection: it is
    the explanation for every reflectance above one. Measured on a real frame,
    **100 %** of the pixels exceeding unity are flagged bright, and over open
    water that is not bright the fraction is zero.
    """
    from ._sen3 import flag_selection

    local = extract_member(root, QUALITY_MEMBER, scratch)
    dataset = open_sen3_variable(local, QUALITY_VARIABLE)
    band = dataset.GetRasterBand(1)
    table = read_flag_table(band)
    flags = band.ReadAsArray(buf_xsize=shape[1], buf_ysize=shape[0])
    dataset = None

    rejected, missing = flag_selection(flags, table, reject, allow_missing=True) if reject else (
        np.zeros(shape, dtype=bool), [],
    )
    bright, _ = flag_selection(flags, table, ["bright"], allow_missing=True)
    return rejected, missing, bright
