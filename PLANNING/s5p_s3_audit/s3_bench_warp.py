"""Alternate warp variants, each in a fresh process; print every result and the failure count.

    python3 -u s3_bench_warp.py PRODUCT_DIR OUT_DIR [--reps 1]

Variants are defined for the OLCI WFR chl_nn variable (log10-packed Byte, 4865 x 4091).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    line = next((l for l in r.stdout.splitlines() if l.startswith("{")), None)
    if r.returncode or not line:
        return None, (r.stderr or r.stdout)[-300:]
    return json.loads(line), ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("product")
    ap.add_argument("out")
    ap.add_argument("--reps", type=int, default=1)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    P = args.product
    base = [sys.executable, os.path.join(HERE, "s3_warp.py")]
    targets = [(4326, 0.003, "4326@0.003deg"), (32661, 300.0, "32661@300m")]
    variants = []
    for epsg, res, tag in targets:
        variants += [
            (f"geoloc step16 {tag}", ["geoloc", f"{P}/chl_nn.nc", "CHL_NN", f"{P}/geo_coordinates.nc", "longitude", "latitude",
                                       f"{args.out}/g16_{epsg}.tif", str(epsg), str(res), "--geoloc-step", "16"]),
            (f"gcp-tps r64c4 {tag}", ["gcp", f"{P}/chl_nn.nc", "CHL_NN", f"{P}/tie_geo_coordinates.nc", "longitude", "latitude",
                                      f"{args.out}/gcp_{epsg}.tif", str(epsg), str(res)]),
            (f"geoloc step4 {tag}", ["geoloc", f"{P}/chl_nn.nc", "CHL_NN", f"{P}/geo_coordinates.nc", "longitude", "latitude",
                                      f"{args.out}/g4_{epsg}.tif", str(epsg), str(res), "--geoloc-step", "4"]),
            (f"geoloc step1 {tag}", ["geoloc", f"{P}/chl_nn.nc", "CHL_NN", f"{P}/geo_coordinates.nc", "longitude", "latitude",
                                      f"{args.out}/g1_{epsg}.tif", str(epsg), str(res), "--geoloc-step", "1"]),
        ]
    fails = ok = 0
    for rep in range(args.reps):
        for name, argv in variants:
            res, err = run(base + argv)
            if res is None:
                fails += 1
                print(f"FAIL {name}: {err}", flush=True)
            else:
                ok += 1
                print(f"rep{rep} {name:34s} {res['seconds']:7.1f} s  setup {res['setup_seconds']:5.1f} s  "
                      f"peak {res['peak_rss_mb']:5.0f} MB  out {res['out_size_px']}  {res['out_mb']} MB", flush=True)
    print(f"done: {ok} ok, {fails} failed", flush=True)


if __name__ == "__main__":
    main()
