"""R1: which way round does GDAL hand over OLCI's band-indexed tables?

Reproduces the finding behind the solar-flux fix. Reads ``lambda0`` and
``solar_flux`` from a real Level-1 product under each setting of
``GDAL_NETCDF_BOTTOMUP`` and prints, for the three true-colour bands, the row
``band - 1`` would have taken and the row the wavelength says it should.

    python3 flux_orientation.py PRODUCT.zip [--column 1000]
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

from pysent._olci_l1 import BAND_WAVELENGTHS  # noqa: E402
from pysent._sen3 import extract_member, open_sen3_variable, sen3_root  # noqa: E402

TRUECOLOR = (4, 6, 8)


def read(local: str, name: str) -> np.ndarray:
    dataset = open_sen3_variable(local, name)
    array = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    return array


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("product")
    parser.add_argument("--column", type=int, default=1000)
    args = parser.parse_args()

    scratch = pathlib.Path(tempfile.mkdtemp(prefix="flux-orientation-"))
    local = extract_member(sen3_root(args.product), "instrument_data.nc", scratch)

    for setting in (None, "NO", "YES"):
        gdal.SetConfigOption("GDAL_NETCDF_BOTTOMUP", setting)
        wavelengths = read(local, "lambda0")[:, args.column]
        print(f"\nGDAL_NETCDF_BOTTOMUP={setting}  lambda0 by row:")
        print("  " + " ".join(f"{wavelengths[row]:7.1f}" for row in range(0, 21, 4)))
    gdal.SetConfigOption("GDAL_NETCDF_BOTTOMUP", None)

    wavelengths = read(local, "lambda0")[:, args.column]
    fluxes = read(local, "solar_flux")[:, args.column]
    print("\nband  nominal   row band-1  lambda0 there   flux used   right row   right flux   factor")
    for band in TRUECOLOR:
        nominal = BAND_WAVELENGTHS[band]
        taken = band - 1
        right = int(np.argmin(np.abs(wavelengths - nominal)))
        print(f"  Oa{band:02d}   {nominal:4d} nm      {taken:2d}        {wavelengths[taken]:7.1f} nm "
              f"{fluxes[taken]:10.1f}        {right:2d}   {fluxes[right]:10.1f}   "
              f"{fluxes[right] / fluxes[taken]:5.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
