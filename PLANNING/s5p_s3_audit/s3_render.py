"""Tiny colour-mapped PNG renderer: numpy + GDAL's PNG driver (no matplotlib/PIL).

    from s3_render import colorize, rgba_to_png, CMAPS, mosaic_legend
    rgba = colorize(values, vmin, vmax, "viridis", mask=valid, log=False)
    rgba_to_png(rgba, "out.png")

The colormaps are short anchor lists interpolated linearly in RGB.  They are
PROPOSALS for the owner to judge, not defaults baked into pysent.
"""
from __future__ import annotations

import numpy as np
from osgeo import gdal

#: anchor lists: (position 0..1, (r, g, b))
CMAPS: dict[str, list[tuple[float, tuple[int, int, int]]]] = {
    # perceptually uniform, sequential (viridis, 7 anchors)
    "viridis": [(0.0, (68, 1, 84)), (0.17, (70, 50, 127)), (0.33, (54, 92, 141)), (0.5, (39, 127, 142)),
                (0.67, (31, 161, 135)), (0.83, (74, 194, 109)), (1.0, (253, 231, 37))],
    # ocean-colour style: dark purple -> blue -> cyan -> green -> yellow -> red
    "chl": [(0.0, (72, 21, 109)), (0.2, (49, 80, 183)), (0.4, (30, 160, 200)), (0.6, (60, 190, 90)),
            (0.8, (240, 220, 40)), (1.0, (200, 40, 30))],
    # temperature: blue -> cyan -> white-ish -> orange -> red
    "temp": [(0.0, (49, 54, 149)), (0.25, (69, 117, 180)), (0.45, (171, 217, 233)), (0.5, (245, 245, 235)),
             (0.65, (253, 174, 97)), (0.85, (215, 48, 39)), (1.0, (130, 0, 38))],
    # vegetation: brown -> tan -> light green -> dark green
    "ndvi": [(0.0, (140, 90, 40)), (0.25, (220, 200, 140)), (0.45, (190, 220, 110)), (0.7, (60, 160, 60)),
             (1.0, (0, 80, 30))],
    "gray": [(0.0, (0, 0, 0)), (1.0, (255, 255, 255))],
}


def _lut(name: str, n: int = 256) -> np.ndarray:
    anchors = CMAPS[name]
    xs = np.array([a[0] for a in anchors])
    out = np.zeros((n, 3), dtype=np.uint8)
    grid = np.linspace(0, 1, n)
    for ch in range(3):
        out[:, ch] = np.interp(grid, xs, [a[1][ch] for a in anchors]).round()
    return out


def colorize(values, vmin: float, vmax: float, cmap: str = "viridis", *, mask=None, log: bool = False,
             nodata_rgb=(0, 0, 0)) -> np.ndarray:
    """Return an (H, W, 4) uint8 RGBA array; masked / non-finite pixels are transparent."""
    v = np.asarray(values, dtype="float64")
    ok = np.isfinite(v)
    if mask is not None:
        ok &= np.asarray(mask, dtype=bool)
    if log:
        ok &= v > 0
        v = np.where(ok, np.log10(np.where(ok, v, 1.0)), np.nan)
        vmin, vmax = np.log10(vmin), np.log10(vmax)
    t = np.clip((v - vmin) / (vmax - vmin), 0, 1)
    idx = np.where(ok, t * 255, 0).round().astype(np.uint8)
    rgb = _lut(cmap)[idx]
    rgba = np.zeros(v.shape + (4,), dtype=np.uint8)
    rgba[..., :3] = np.where(ok[..., None], rgb, np.array(nodata_rgb, dtype=np.uint8))
    rgba[..., 3] = np.where(ok, 255, 0)
    return rgba


def rgba_to_png(rgba: np.ndarray, path: str, background=(235, 238, 240)) -> None:
    """Write RGBA as an opaque PNG over a light background (transparency viewers differ)."""
    a = rgba[..., 3:4].astype("float32") / 255.0
    rgb = (rgba[..., :3] * a + np.array(background, dtype="float32") * (1 - a)).round().astype(np.uint8)
    h, w, _ = rgb.shape
    mem = gdal.GetDriverByName("MEM").Create("", w, h, 3, gdal.GDT_Byte)
    for i in range(3):
        mem.GetRasterBand(i + 1).WriteArray(rgb[..., i])
    gdal.GetDriverByName("PNG").CreateCopy(path, mem)


def decimate(a: np.ndarray, factor: int) -> np.ndarray:
    return a[::factor, ::factor]


def legend_strip(vmin: float, vmax: float, cmap: str, width: int = 512, height: int = 24, log: bool = False) -> np.ndarray:
    ramp = np.linspace(0, 1, width)[None, :].repeat(height, axis=0)
    rgba = colorize(ramp, 0.0, 1.0, cmap)
    return rgba
