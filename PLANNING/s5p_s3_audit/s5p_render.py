"""Render float rasters to PNG with numpy + GDAL (no matplotlib/PIL in the CI image).

    python3 s5p_render.py OUT.png --panel IN.tif vmin vmax [--panel ...] [--cmap viridis] [--zoom N]
        [--crop x0 y0 w h] [--scale lin|log] [--mult 1e6]

Several ``--panel`` arguments are placed side by side with a gap; a colour bar is
drawn under each.  No-data is mid-grey so "no retrieval" is visibly different from
"value at the bottom of the scale".  There is deliberately no text rendering - the
captions (ranges, units) belong in the report that embeds the picture.
"""
from __future__ import annotations

import argparse

import numpy as np
from osgeo import gdal

gdal.UseExceptions()

# 9 anchors per map, interpolated linearly in RGB.
CMAPS = {
    "viridis": [(68, 1, 84), (71, 44, 122), (59, 81, 139), (44, 113, 142), (33, 144, 141),
                (39, 173, 129), (92, 200, 99), (170, 220, 50), (253, 231, 37)],
    "magma": [(0, 0, 4), (28, 16, 68), (79, 18, 123), (129, 37, 129), (181, 54, 122),
              (229, 80, 100), (251, 135, 97), (254, 194, 135), (252, 253, 191)],
    "turbo-ish": [(48, 18, 59), (70, 107, 227), (40, 187, 236), (49, 242, 153), (162, 252, 60),
                  (237, 208, 58), (251, 128, 34), (200, 42, 4), (122, 4, 3)],
    "diverging": [(5, 48, 97), (33, 102, 172), (103, 169, 207), (209, 229, 240), (247, 247, 247),
                  (253, 219, 199), (239, 138, 98), (178, 24, 43), (103, 0, 31)],
}
NODATA_RGB = (150, 150, 150)
BG_RGB = (255, 255, 255)


def ramp(name: str, n: int = 256) -> np.ndarray:
    anchors = np.array(CMAPS[name], dtype=np.float64)
    x = np.linspace(0, len(anchors) - 1, n)
    idx = np.clip(np.floor(x).astype(int), 0, len(anchors) - 2)
    frac = (x - idx)[:, None]
    return (anchors[idx] * (1 - frac) + anchors[idx + 1] * frac).astype(np.uint8)


def colorize(arr: np.ndarray, vmin: float, vmax: float, cmap: str, scale: str = "lin") -> np.ndarray:
    lut = ramp(cmap)
    if scale == "log":
        with np.errstate(invalid="ignore", divide="ignore"):
            arr = np.log10(np.where(arr > 0, arr, np.nan))
        vmin, vmax = np.log10(vmin), np.log10(vmax)
    t = np.clip((arr - vmin) / (vmax - vmin), 0, 1)
    rgb = lut[np.nan_to_num(t * 255, nan=0).astype(np.uint8)]
    rgb[~np.isfinite(arr)] = NODATA_RGB
    return rgb


def colorbar(width: int, cmap: str, height: int = 12) -> np.ndarray:
    lut = ramp(cmap, width)
    return np.repeat(lut[None, :, :], height, axis=0)


def save_png(rgb: np.ndarray, path: str) -> None:
    h, w, _ = rgb.shape
    mem = gdal.GetDriverByName("MEM").Create("", w, h, 3, gdal.GDT_Byte)
    for i in range(3):
        mem.GetRasterBand(i + 1).WriteArray(rgb[:, :, i])
    gdal.GetDriverByName("PNG").CreateCopy(path, mem)


def load(path: str) -> np.ndarray:
    ds = gdal.Open(path)
    return ds.GetRasterBand(1).ReadAsArray().astype(np.float64)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--panel", nargs=3, action="append", metavar=("TIF", "VMIN", "VMAX"), required=True)
    ap.add_argument("--cmap", default="viridis")
    ap.add_argument("--scale", default="lin", choices=["lin", "log"])
    ap.add_argument("--zoom", type=int, default=1)
    ap.add_argument("--crop", nargs=4, type=int, metavar=("X0", "Y0", "W", "H"))
    ap.add_argument("--mult", type=float, default=1.0, help="multiply data before applying vmin/vmax")
    args = ap.parse_args()

    tiles = []
    for tif, vmin, vmax in args.panel:
        arr = load(tif) * args.mult
        if args.crop:
            x0, y0, w, h = args.crop
            arr = arr[y0:y0 + h, x0:x0 + w]
        rgb = colorize(arr, float(vmin), float(vmax), args.cmap, args.scale)
        if args.zoom > 1:
            rgb = np.repeat(np.repeat(rgb, args.zoom, axis=0), args.zoom, axis=1)
        bar = colorbar(rgb.shape[1], args.cmap)
        tiles.append(np.vstack([rgb, np.full((6, rgb.shape[1], 3), BG_RGB, np.uint8), bar]))
    height = max(t.shape[0] for t in tiles)
    gap = np.full((height, 10, 3), BG_RGB, np.uint8)
    row = []
    for i, t in enumerate(tiles):
        pad = np.full((height - t.shape[0], t.shape[1], 3), BG_RGB, np.uint8)
        row.append(np.vstack([t, pad]))
        if i < len(tiles) - 1:
            row.append(gap)
    save_png(np.hstack(row), args.out)
    print(args.out, np.hstack(row).shape)


if __name__ == "__main__":
    main()
