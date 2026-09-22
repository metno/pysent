"""How far the polynomial GCP warp departs from the product's own geolocation grid.

    PLANNING/bulk_processing_audit/run.sh warp-accuracy <DATA_DIR>

The thin-plate spline interpolates a SAFE product's GCPs exactly, so the numbers
below are the polynomial transform's departure from the geolocation the product
ships with - the evidence behind keeping `use_tps=True` as the default
(PLANNING/PLANNING_bulk_processing.md, finding P5; docs/tuning-and-roadmap.md §4.1b).
"""
import sys
from pathlib import Path

import numpy as np
from osgeo import gdal, osr

from pysent import s1

gdal.UseExceptions()


def report(zip_path: str, variable: str = "Amplitude_VV", work_dir: str = "/tmp") -> None:
    vrt = Path(work_dir) / "warp_accuracy.vrt"
    s1._build_sentinel_s1_safe_vrt(zip_path, variable, vrt)
    dataset = gdal.Open(str(vrt))
    gcps = dataset.GetGCPs()

    source = osr.SpatialReference(); source.SetFromUserInput(dataset.GetGCPProjection())
    target = osr.SpatialReference(); target.SetFromUserInput(s1.S1_TARGET_EPSG)
    for srs in (source, target):
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    to_target = osr.CoordinateTransformation(source, target)

    truth = np.array([to_target.TransformPoint(g.GCPX, g.GCPY)[:2] for g in gcps])
    pixels = np.array([[g.GCPPixel, g.GCPLine] for g in gcps])

    def mapped(method: str) -> np.ndarray:
        transformer = gdal.Transformer(dataset, None, [f"METHOD={method}", f"DST_SRS={s1.S1_TARGET_EPSG}"])
        points = []
        for px, py in pixels:
            ok, xyz = transformer.TransformPoint(0, float(px), float(py), 0.0)
            points.append(xyz[:2] if ok else (np.nan, np.nan))
        return np.array(points)

    print(f"{len(gcps)} GCPs over {dataset.RasterXSize} x {dataset.RasterYSize} pixels")
    for method in ("GCP_TPS", "GCP_POLYNOMIAL"):
        error = np.linalg.norm(mapped(method) - truth, axis=1)
        print(f"\n{method}: RMS {np.sqrt((error ** 2).mean()):.1f} m, max {error.max():.1f} m")
        if method == "GCP_TPS":
            continue
        for name, value in [("median", np.median(error)), ("p90", np.percentile(error, 90)),
                            ("p95", np.percentile(error, 95)), ("p99", np.percentile(error, 99)),
                            ("max", error.max()), ("RMS", np.sqrt((error ** 2).mean()))]:
            print(f"   {name:>6}: {value:7.1f} m = {value / 40:5.2f} px at 40 m, {value / 160:5.2f} px at 160 m")
        across = pixels[:, 0] / dataset.RasterXSize
        along = pixels[:, 1] / dataset.RasterYSize
        edge = (np.minimum(across, 1 - across) < 0.1) | (np.minimum(along, 1 - along) < 0.1)
        print(f"   interior (n={int((~edge).sum())}): mean {error[~edge].mean():.1f} m, max {error[~edge].max():.1f} m")
        print(f"   edge     (n={int(edge.sum())}): mean {error[edge].mean():.1f} m, max {error[edge].max():.1f} m")


if __name__ == "__main__":
    products = sorted(Path(sys.argv[1] if len(sys.argv) > 1 else "/data").glob("S1*.zip"))
    if not products:
        raise SystemExit("no S1 product found; pass a directory holding one")
    report(str(products[0]))
