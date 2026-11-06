"""Rank recent OLCI WFR frames by clear-water fraction, reading only wqsf.nc (remote, ~4 MB).

    python3 s3_scan_water.py 202606 --bbox 0,56,15,66 [--pages 3]
"""
import argparse, os, sys
import numpy as np
from osgeo import gdal
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from catalogue_survey import _get, NS, _records
from s3_pick import head_size

gdal.UseExceptions()
BAD = [0, 2, 3, 4, 23, 24]  # INVALID LAND CLOUD SNOW_ICE CLOUD_AMBIGUOUS CLOUD_MARGIN
ap = argparse.ArgumentParser()
ap.add_argument("month"); ap.add_argument("--bbox", default="0,56,15,66"); ap.add_argument("--pages", type=int, default=3)
ap.add_argument("--type", default="OL_2_WFR")
a = ap.parse_args()
x0, y0, x1, y1 = a.bbox.split(",")
rows = []
for page in range(a.pages):
    root = _get({"service": "CSW", "version": "2.0.2", "request": "GetRecords", "typenames": "csw:Record",
                 "elementsetname": "full", "resulttype": "results", "maxrecords": "10", "startposition": str(1 + 10 * page),
                 "constraintlanguage": "CQL_TEXT", "constraint_language_version": "1.1.0",
                 "constraint": f"dc:title like '%{a.type}%{a.month}%NT%' AND BBOX(ows:BoundingBox, {x0}, {y0}, {x1}, {y1})",
                 "outputschema": "http://www.opengis.net/cat/csw/2.0.2"}).find("csw:SearchResults", NS)
    for rec in _records(root):
        url = rec["refs"].get("WWW:DOWNLOAD-1.0-http--download")
        if not url or head_size(url) <= 0:
            continue
        p = "/vsizip//vsicurl/" + url + "/" + os.path.basename(url)[:-4] + ".SEN3/wqsf.nc"
        try:
            ds = gdal.Open(f'NETCDF:"{p}":WQSF')
            w = ds.GetRasterBand(1).ReadAsArray(buf_xsize=ds.RasterXSize // 8, buf_ysize=ds.RasterYSize // 8).astype("uint64")
        except Exception as e:
            continue
        bad = np.zeros(w.shape, bool)
        for b in BAD:
            bad |= ((w >> np.uint64(b)) & np.uint64(1)).astype(bool)
        water = ((w >> np.uint64(1)) & np.uint64(1)).astype(bool)
        rows.append((float((water & ~bad).mean()), float(water.mean()), head_size(url) / 1e6, rec["title"], url))
for r in sorted(rows, reverse=True)[:6]:
    print(f"clear water {100*r[0]:5.1f}%  water {100*r[1]:5.1f}%  {r[2]:6.1f} MB  {r[3]}\n   {r[4]}")
