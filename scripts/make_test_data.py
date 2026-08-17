#!/usr/bin/env python3
"""Generate the small Sentinel test-data artifacts used by the tests and docs.

Queries the NBS catalogue for a **recent** Sentinel-1 GRD and Sentinel-2 scene
over Norway, then extracts a small window from each straight out of the remote
SAFE archive and writes it as a compressed GeoTIFF, plus a JSON manifest
recording exactly which product each window came from.

Nothing is downloaded in bulk. The SAFE archives are read in place over HTTP via
GDAL's ``/vsizip//vsicurl/`` chain, which the NBS THREDDS server supports
(``accept-ranges: bytes``), so only the handful of blocks covering the requested
window cross the network - a few hundred KB out of a 1-8 GB product.

    python scripts/make_test_data.py                  # refresh tests/data
    python scripts/make_test_data.py --days 14        # widen the search
    python scripts/make_test_data.py --window 1024    # bigger extracts
    python scripts/make_test_data.py --platform s2    # just Sentinel-2

The outputs are intentionally small enough to commit, so the test suite and the
documentation notebooks run offline and deterministically. Re-run this when you
want fresher scenes; the manifest makes the provenance of each artifact explicit.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from osgeo import gdal

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "tests" / "data"

CSW_ENDPOINT = "https://nbs.csw.met.no"
#: Mainland Norway plus surrounding waters (minx, miny, maxx, maxy in EPSG:4326).
NORWAY_BBOX = [4.0, 57.0, 32.0, 72.0]
ARCHIVE_MARKER = "nbsArchive/"

#: Sentinel-2 bands extracted, in the order they are stacked into the fixture.
#: These are pysent's own band names (S2_DEFAULT_PRODUCTS["true_color_vegetation"]),
#: which match GDAL's unpadded BANDNAME metadata - not the zero-padded form used
#: in the granule filenames (``..._B04_10m.jp2``).
S2_RGB_BANDS = ("B4", "B3", "B2")

# GDAL needs a little coaxing to be efficient against remote zips.
_GDAL_REMOTE_OPTIONS = {
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "26214400",  # 25 MB
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "CPL_VSIL_CURL_USE_HEAD": "YES",
}


def _configure_gdal() -> None:
    gdal.UseExceptions()
    for key, value in _GDAL_REMOTE_OPTIONS.items():
        gdal.SetConfigOption(key, value)


# --------------------------------------------------------------------------- #
# Catalogue search
# --------------------------------------------------------------------------- #
def find_recent_product(*, title_pattern: str, days: int, bbox: list[float]) -> dict[str, str]:
    """Return the most recent catalogue record matching ``title_pattern``.

    ``title_pattern`` is a CSW ``PropertyIsLike`` pattern, e.g. ``"S1%GRDH%"``.
    Raises LookupError when the search window contains nothing usable.
    """
    from owslib.csw import CatalogueServiceWeb
    from owslib.fes import And, BBox, PropertyIsGreaterThanOrEqualTo, PropertyIsLike

    since = (date.today() - timedelta(days=days)).isoformat()
    csw = CatalogueServiceWeb(CSW_ENDPOINT, timeout=60)
    constraint = And([
        PropertyIsGreaterThanOrEqualTo(propertyname="apiso:TempExtent_begin", literal=since),
        PropertyIsLike(propertyname="apiso:Title", literal=title_pattern),
        BBox(bbox, crs="urn:ogc:def:crs:EPSG::4326"),
    ])
    csw.getrecords2(constraints=[constraint], maxrecords=20, esn="full")

    candidates = []
    for identifier, record in csw.records.items():
        url = next(
            (
                ref.get("url")
                for ref in (getattr(record, "references", None) or [])
                if "download" in (ref.get("scheme") or "").lower() and ARCHIVE_MARKER in (ref.get("url") or "")
            ),
            None,
        )
        if not url:
            continue
        candidates.append({
            "identifier": identifier,
            "title": str(getattr(record, "title", "") or ""),
            "modified": str(getattr(record, "modified", "") or ""),
            "download_url": url,
        })

    if not candidates:
        raise LookupError(
            f"No downloadable product matching {title_pattern!r} in the last {days} days over {bbox}. "
            "Try a longer --days window."
        )
    # Newest first; the title carries the sensing timestamp so it sorts correctly.
    candidates.sort(key=lambda c: c["title"], reverse=True)
    return candidates[0]


def _vsi_safe_root(download_url: str) -> str:
    """The /vsizip//vsicurl/ path of the .SAFE directory inside a remote product zip."""
    base = f"/vsizip//vsicurl/{download_url}"
    entries = gdal.ReadDir(base) or []
    safe = next((e for e in entries if e.endswith(".SAFE")), None)
    if safe is None:
        raise RuntimeError(f"No .SAFE directory inside {download_url}")
    return f"{base}/{safe}"


# --------------------------------------------------------------------------- #
# Window selection
# --------------------------------------------------------------------------- #
def _pick_window(dataset: gdal.Dataset, size: int, *, probes: int = 9) -> tuple[int, int]:
    """Choose a window offset whose pixels are mostly valid data.

    Real scenes are partial - a swath crosses the raster diagonally and the rest
    is fill. A fixture cut from a nodata corner would be useless for exercising
    the stretch, so probe a few evenly spaced candidates and keep the one with
    the highest fraction of non-zero pixels. Deterministic: no randomness, so
    re-running on the same product yields the same window.
    """
    width, height = dataset.RasterXSize, dataset.RasterYSize
    band = dataset.GetRasterBand(1)
    best: tuple[float, tuple[int, int]] = (-1.0, (0, 0))

    steps = int(probes ** 0.5)
    for i in range(1, steps + 1):
        for j in range(1, steps + 1):
            x = min(max(int(width * i / (steps + 1)) - size // 2, 0), max(width - size, 0))
            y = min(max(int(height * j / (steps + 1)) - size // 2, 0), max(height - size, 0))
            # Cheap probe: a decimated read of the candidate window.
            probe = band.ReadAsArray(x, y, min(size, width), min(size, height), 64, 64)
            if probe is None:
                continue
            valid = float((probe > 0).mean())
            if valid > best[0]:
                best = (valid, (x, y))
            if valid > 0.98:  # good enough, stop probing
                return best[1]
    return best[1]


def _extract_window(source: str, out_path: Path, *, size: int, window: tuple[int, int] | None = None,
                    bands: list[int] | None = None) -> dict[str, Any]:
    """Translate a window of ``source`` into a small compressed GeoTIFF.

    gdal.Translate is used rather than a manual read/write so the geotransform is
    shifted (and GCPs subset) correctly for the extracted region.
    """
    dataset = gdal.Open(source, gdal.GA_ReadOnly)
    if dataset is None:
        raise RuntimeError(f"Unable to open {source}")

    width = min(size, dataset.RasterXSize)
    height = min(size, dataset.RasterYSize)
    x, y = window if window is not None else _pick_window(dataset, size)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    translated = gdal.Translate(
        str(out_path),
        dataset,
        srcWin=[x, y, width, height],
        bandList=bands,
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BLOCKXSIZE=256", "BLOCKYSIZE=256"],
    )
    if translated is None:
        raise RuntimeError(f"Failed to extract window from {source}")
    stats = translated.GetRasterBand(1).ComputeStatistics(False)
    result = {
        "path": out_path.name,
        "src_window": [x, y, width, height],
        "size": [translated.RasterXSize, translated.RasterYSize],
        "bands": translated.RasterCount,
        "dtype": gdal.GetDataTypeName(translated.GetRasterBand(1).DataType),
        "crs": (translated.GetProjectionRef() or "").split('"')[1] if translated.GetProjectionRef() else None,
        "stats_band1": {"min": stats[0], "max": stats[1], "mean": round(stats[2], 2)},
    }
    translated = None
    dataset = None
    result["bytes"] = out_path.stat().st_size
    return result


# --------------------------------------------------------------------------- #
# Per-platform extraction
# --------------------------------------------------------------------------- #
def build_s1(record: dict[str, str], out_dir: Path, size: int) -> dict[str, Any]:
    """Extract one window per polarisation from an S1 GRD product."""
    safe = _vsi_safe_root(record["download_url"])
    measurements = [f for f in (gdal.ReadDir(f"{safe}/measurement") or []) if f.endswith(".tiff")]
    if not measurements:
        raise RuntimeError(f"No measurement rasters in {safe}")

    artifacts: dict[str, Any] = {}
    shared_window: tuple[int, int] | None = None
    for name in sorted(measurements):
        # e.g. s1c-iw-grd-vh-2026...tiff -> "vh"
        polarisation = name.split("-")[3].upper()
        source = f"{safe}/measurement/{name}"
        if shared_window is None:
            probe = gdal.Open(source, gdal.GA_ReadOnly)
            shared_window = _pick_window(probe, size)
            probe = None
        # Same window for every polarisation so the fixtures are co-registered.
        info = _extract_window(source, out_dir / f"s1_{polarisation.lower()}_window.tif",
                               size=size, window=shared_window)
        info["polarization"] = polarisation
        info["source_member"] = f"measurement/{name}"
        artifacts[f"Amplitude_{polarisation}"] = info
    return artifacts


def build_s2(record: dict[str, str], out_dir: Path, size: int) -> dict[str, Any]:
    """Extract a co-registered RGB window (B04/B03/B02) from an S2 product.

    Uses GDAL's SENTINEL2 subdataset abstraction rather than reaching into the
    granule layout by hand, so this works for both L1C and L2A products (their
    IMG_DATA trees differ).
    """
    # The SENTINEL2 driver wants the product metadata XML; it does not descend
    # into a zip root on its own, so resolve <product>.SAFE/MTD_MSIL{1C,2A}.xml.
    safe = _vsi_safe_root(record["download_url"])
    entries = gdal.ReadDir(safe) or []
    metadata_name = next(
        (e for e in entries if e.startswith("MTD_MSIL") and e.endswith(".xml")),
        None,
    )
    if metadata_name is None:
        raise RuntimeError(f"No MTD_MSIL*.xml in {safe}; found {entries}")
    root = f"{safe}/{metadata_name}"
    dataset = gdal.Open(root, gdal.GA_ReadOnly)
    if dataset is None:
        raise RuntimeError(f"Unable to open {root}")
    subdatasets = dataset.GetMetadata("SUBDATASETS") or {}
    # The 10 m subdataset carries B04/B03/B02 together.
    ten_metre = next(
        (v for k, v in sorted(subdatasets.items()) if k.endswith("_NAME") and ":10m:" in v),
        None,
    )
    if ten_metre is None:
        raise RuntimeError(f"No 10 m subdataset in {root}; available: {sorted(subdatasets)}")

    sub = gdal.Open(ten_metre, gdal.GA_ReadOnly)

    def _normalize(name: str) -> str:
        # "B04" and "B4" refer to the same band; collapse to the unpadded form.
        name = name.strip().upper()
        return f"B{int(name[1:])}" if name.startswith("B") and name[1:].isdigit() else name

    band_index = {}
    for i in range(1, sub.RasterCount + 1):
        raw = (sub.GetRasterBand(i).GetMetadataItem("BANDNAME")
               or sub.GetRasterBand(i).GetDescription() or "")
        if raw:
            band_index[_normalize(raw)] = i
    missing = [b for b in S2_RGB_BANDS if _normalize(b) not in band_index]
    if missing:
        raise RuntimeError(f"Bands {missing} not found in {ten_metre}; have {sorted(band_index)}")

    window = _pick_window(sub, size)
    sub = None
    dataset = None

    info = _extract_window(
        ten_metre,
        out_dir / "s2_rgb_window.tif",
        size=size,
        window=window,
        bands=[band_index[_normalize(b)] for b in S2_RGB_BANDS],
    )
    info["band_order"] = list(S2_RGB_BANDS)
    info["source_subdataset"] = ten_metre.split(":")[-2] if ":" in ten_metre else ten_metre
    return {"rgb": info}


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory (default: tests/data)")
    parser.add_argument("--days", type=int, default=7, help="how far back to search the catalogue (default: 7)")
    parser.add_argument("--window", type=int, default=512, help="extract size in pixels (default: 512)")
    parser.add_argument("--platform", choices=["s1", "s2", "both"], default="both")
    parser.add_argument("--bbox", type=float, nargs=4, default=NORWAY_BBOX,
                        metavar=("MINX", "MINY", "MAXX", "MAXY"), help="search area (default: Norway)")
    args = parser.parse_args(argv)

    _configure_gdal()
    args.out.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "generated_by": "scripts/make_test_data.py",
        "csw_endpoint": CSW_ENDPOINT,
        "search": {"bbox": args.bbox, "days": args.days, "window_px": args.window},
        "products": {},
    }

    targets = []
    if args.platform in ("s1", "both"):
        targets.append(("S1", "S1%GRDH%", build_s1))
    if args.platform in ("s2", "both"):
        targets.append(("S2", "S2%MSIL%", build_s2))

    for label, pattern, builder in targets:
        print(f"[{label}] searching catalogue ({args.days} days, bbox={args.bbox}) ...", flush=True)
        record = find_recent_product(title_pattern=pattern, days=args.days, bbox=args.bbox)
        print(f"[{label}] {record['title']}\n       uuid {record['identifier']}", flush=True)
        print(f"[{label}] extracting {args.window}x{args.window} window over HTTP ...", flush=True)
        artifacts = builder(record, args.out, args.window)
        manifest["products"][label] = {**record, "artifacts": artifacts}
        for key, info in artifacts.items():
            print(f"       {info['path']}  {info['size'][0]}x{info['size'][1]} "
                  f"{info['dtype']}  {info['bytes'] / 1024:.0f} KB  ({key})", flush=True)

    manifest_path = args.out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    total = sum(f.stat().st_size for f in args.out.glob("*.tif"))
    print(f"\nwrote {manifest_path.relative_to(REPO_ROOT)} and {total / 1024:.0f} KB of rasters")
    return 0


if __name__ == "__main__":
    sys.exit(main())
