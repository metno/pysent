"""The Sentinel-5P freeze: do these products still render byte-identical pixels?

The Sentinel-5P defaults are frozen while an expert review study is open, so any
branch that touches shared code has to show that it changes no Sentinel-5P
pixel. Hashing the **pixels** rather than the file matters: the provenance tags
carry a version string and a software name, so two identical rasters written by
two commits never have the same file hash.

Run it with each source tree on ``sys.path`` and compare the output:

    python3 s5p_freeze_check.py --src /path/to/branch/src --out /tmp/branch
    python3 s5p_freeze_check.py --src /path/to/main/src   --out /tmp/main
    diff /tmp/branch/hashes.txt /tmp/main/hashes.txt
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys

CASES = (
    # (label, granule, product, qa_min)
    ("no2-window-qa75", "s5p_no2_window.nc", "no2", 0.75),
    ("no2-archive-qa75", "S5P_NRTI_L2__NO2____20261009T111407_20261009T111907_46578_03_020901_20261009T115729.nc", "no2", 0.75),
    ("co-archive-qa50", "S5P_NRTI_L2__CO_____20261009T011407_20261009T011907_46572_03_020901_20261009T030243.nc", "co", 0.5),
    ("o3-archive-qa50", "S5P_NRTI_L2__O3_____20261009T011407_20261009T011907_46572_03_020800_20261009T030444.nc", "o3", 0.5),
)


def band_hashes(path: pathlib.Path) -> list[str]:
    """One hash per band of the raster's pixels, tags excluded."""
    from osgeo import gdal

    dataset = gdal.Open(str(path))
    hashes = []
    for index in range(1, dataset.RasterCount + 1):
        array = dataset.GetRasterBand(index).ReadAsArray()
        hashes.append(hashlib.sha256(array.tobytes()).hexdigest()[:16])
    dataset = None
    return hashes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=pathlib.Path, required=True, help="the source tree to test")
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--data", type=pathlib.Path,
                        default=pathlib.Path("/mnt/data/pysent/testdata"))
    parser.add_argument("--fixtures", type=pathlib.Path, default=None,
                        help="where the committed test fixtures are (tests/data)")
    args = parser.parse_args()

    sys.path.insert(0, str(args.src))
    from osgeo import gdal

    gdal.UseExceptions()
    from pysent.s5p import process_sentinel_s5p_netcdf

    fixtures = args.fixtures or args.src.parent / "tests" / "data"
    args.out.mkdir(parents=True, exist_ok=True)
    lines = []
    for label, name, product, qa_min in CASES:
        granule = (fixtures / name) if (fixtures / name).exists() else (args.data / name)
        if not granule.exists():
            lines.append(f"{label}: SKIPPED, {name} not found")
            continue
        results = process_sentinel_s5p_netcdf(
            input_dataset=str(granule),
            output_dir=args.out / label,
            products={product: {"qa_min": qa_min}},
            processing_options={"styled": True, "thumbnail": True},
        )
        for result in results:
            for key in ("path", "styled_path"):
                if result.get(key):
                    hashes = band_hashes(pathlib.Path(result[key]))
                    lines.append(f"{label} {key} {','.join(hashes)}")
            lines.append(
                f"{label} stats "
                + ",".join(f"{result['value_stats'][k]:.9g}" for k in ("min", "p01", "p50", "p99", "max"))
                + f",valid={result['valid_fraction']:.9g}"
            )

    text = "\n".join(lines) + "\n"
    (args.out / "hashes.txt").write_text(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
