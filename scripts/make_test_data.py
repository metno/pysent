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
import tempfile
import zipfile
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

# --------------------------------------------------------------------------- #
# Sentinel-5P
# --------------------------------------------------------------------------- #
S5P_NO2_VARIABLE = "nitrogendioxide_tropospheric_column"
S5P_ARRAYS = ("latitude", "longitude", "qa_value", S5P_NO2_VARIABLE)


def build_s5p(source: str, out_path: Path, *, scanlines: int) -> dict[str, Any]:
    """Cut the first ``scanlines`` rows of an S5P granule into a tiny NetCDF.

    The extract keeps the real group layout (``/PRODUCT``), the real packing
    (``qa_value`` as a byte with a 0.01 scale and 255 as fill) and the real fill
    values, because those are exactly what the pipeline has to get right. The
    arrays are written through GDAL's multidimensional API so the script needs
    no netCDF4 binding.

    Only the rows requested cross the network when ``source`` is a
    ``/vsicurl/`` URL, so this stays cheap against a 68 MB granule.
    """
    from pysent._swath import open_array, open_root

    root = open_root(source)
    # Read the window from each array, keeping the stored dtype and packing.
    windows: dict[str, Any] = {}
    for name in S5P_ARRAYS:
        array = open_array(root, f"/PRODUCT/{name}")
        raw = array.ReadAsArray()
        if raw.ndim >= 3 and raw.shape[0] == 1:
            raw = raw[0]
        windows[name] = {
            "data": raw[:scanlines],
            "nodata": array.GetNoDataValueAsDouble(),
            "scale": array.GetScale(),
            "offset": array.GetOffset(),
        }

    rows, columns = windows["latitude"]["data"].shape
    driver = gdal.GetDriverByName("netCDF")
    out_path.unlink(missing_ok=True)
    dataset = driver.CreateMultiDimensional(str(out_path))
    group = dataset.GetRootGroup().CreateGroup("PRODUCT")
    scanline = group.CreateDimension("scanline", None, None, rows)
    ground_pixel = group.CreateDimension("ground_pixel", None, None, columns)

    numpy_to_gdal = {
        "float32": gdal.GDT_Float32,
        "float64": gdal.GDT_Float64,
        "uint8": gdal.GDT_Byte,
    }
    written: dict[str, Any] = {}
    for name, window in windows.items():
        data = window["data"]
        gdal_type = numpy_to_gdal.get(str(data.dtype))
        if gdal_type is None:
            raise SystemExit(f"{name}: unexpected dtype {data.dtype}")
        array = group.CreateMDArray(name, [scanline, ground_pixel], gdal.ExtendedDataType.Create(gdal_type))
        # netCDF refuses to define a fill value once the variable holds data,
        # so the packing is declared before anything is written.
        if window["nodata"] is not None:
            array.SetNoDataValueDouble(window["nodata"])
        if window["scale"] not in (None, 1.0):
            array.SetScale(window["scale"])
        if window["offset"] not in (None, 0.0):
            array.SetOffset(window["offset"])
        array.Write(data.tobytes())
        written[name] = {"dtype": str(data.dtype), "shape": list(data.shape)}
    dataset = None

    return {
        "no2_window": {
            "path": str(out_path.relative_to(REPO_ROOT)) if out_path.is_relative_to(REPO_ROOT) else str(out_path),
            "size": [columns, rows],
            "dtype": "mixed",
            "bytes": out_path.stat().st_size,
            "arrays": written,
            "variable": S5P_NO2_VARIABLE,
        }
    }



# --------------------------------------------------------------------------- #
# Sentinel-3
# --------------------------------------------------------------------------- #
S3_SYNERGY_MEMBERS = (("NDVI.nc", "NDVI"), ("sm.nc", "SM"))


def build_s3_synergy(source: str, out_path: Path, *, window: int) -> dict[str, Any]:
    """Cut a window out of a Synergy VG1/V10 tile into a small STORED zip.

    Keeps everything the pipeline reads: the ``.SEN3`` directory layout, the
    member names, the byte packing (NDVI is ``raw*0.004-0.08`` with 255 as
    fill), the flag table on the status variable, the 1/112 degree grid, and a
    manifest carrying the product type, baseline, times and footprint.

    Members are stored uncompressed, like the real products, so the reader's
    range-read path is exercised rather than bypassed.
    """
    from pysent._sen3 import extract_member, open_sen3_variable, read_sen3_manifest, sen3_root

    root = sen3_root(source)
    manifest = read_sen3_manifest(root)
    product_name = str(manifest.get("productName") or "S3A_SY_2_VG1.SEN3")
    inner = Path(product_name).stem + ".SEN3" if not product_name.endswith(".SEN3") else product_name

    with tempfile.TemporaryDirectory() as work:
        scratch = Path(work)
        members: dict[str, Path] = {}
        written: dict[str, Any] = {}
        offset_x = offset_y = None
        for member, variable in S3_SYNERGY_MEMBERS:
            dataset = open_sen3_variable(extract_member(root, member, scratch), variable)
            band = dataset.GetRasterBand(1)
            if offset_x is None:
                # A window with real data in it, not the corner of the tile:
                # a Synergy tile is mostly sea and cloud.
                offset_x, offset_y = _pick_window(dataset, window)
            raw = band.ReadAsArray(offset_x, offset_y, window, window)
            transform = list(dataset.GetGeoTransform())
            transform[0] += offset_x * transform[1]
            transform[3] += offset_y * transform[5]

            # Read everything off the band before the dataset is released: a
            # band whose dataset has been collected is invalid.
            data_type = band.DataType
            scale, offset, nodata = band.GetScale(), band.GetOffset(), band.GetNoDataValue()
            # The flag table has to survive: the pipeline selects by name.
            attributes = {
                key: value
                for key, value in band.GetMetadata().items()
                if key.startswith("flag_") or key in ("units", "long_name")
            }
            dataset = None

            # Written through the multidimensional API with CF coordinate
            # variables, because the classic netCDF writer names every variable
            # "Band1" and the pipeline opens members by their real name. The
            # lat/lon arrays are also what makes GDAL derive a geotransform.
            path = scratch / member
            _write_cf_grid(
                path=path,
                variable=variable,
                data=raw,
                data_type=data_type,
                transform=transform,
                scale=scale,
                offset=offset,
                nodata=nodata,
                attributes=attributes,
            )
            members[member] = path
            written[member] = {"variable": variable, "dtype": gdal.GetDataTypeName(data_type)}

        manifest_xml = _synergy_manifest_xml(manifest, transform, window)
        out_path.unlink(missing_ok=True)
        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for member, path in members.items():
                archive.write(path, f"{inner}/{member}")
            archive.writestr(f"{inner}/xfdumanifest.xml", manifest_xml)

    return {
        "synergy_window": {
            "path": str(out_path.relative_to(REPO_ROOT)) if out_path.is_relative_to(REPO_ROOT) else str(out_path),
            "size": [window, window],
            "dtype": "mixed",
            "bytes": out_path.stat().st_size,
            "members": written,
            "sen3_directory": inner,
        }
    }


def _synergy_manifest_xml(manifest: dict[str, Any], transform: list[float], window: int) -> str:
    """A minimal xfdumanifest.xml carrying what the reader looks for."""
    west, north = transform[0], transform[3]
    east = west + transform[1] * window
    south = north + transform[5] * window
    # posList is latitude then longitude, closed, as the real manifests write it.
    ring = [(north, west), (north, east), (south, east), (south, west), (north, west)]
    positions = " ".join(f"{lat:.6f} {lon:.6f}" for lat, lon in ring)
    times = manifest.get("time", {})
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<xfdu:XFDU xmlns:xfdu="urn:ccsds:schema:xfdu:1" '
        'xmlns:sentinel3="http://www.esa.int/safe/sentinel/sentinel-3/1.0" '
        'xmlns:gml="http://www.opengis.net/gml">\n'
        "  <metadataSection>\n"
        f"    <sentinel3:productName>{manifest.get('productName', '')}</sentinel3:productName>\n"
        f"    <sentinel3:productType>{manifest.get('productType', 'SY_2_VG1___')}</sentinel3:productType>\n"
        f"    <sentinel3:baselineCollection>{manifest.get('baselineCollection', '002')}"
        "</sentinel3:baselineCollection>\n"
        f"    <sentinel3:startTime>{times.get('start', '')}</sentinel3:startTime>\n"
        f"    <sentinel3:stopTime>{times.get('end', '')}</sentinel3:stopTime>\n"
        f"    <gml:posList>{positions}</gml:posList>\n"
        "  </metadataSection>\n"
        "</xfdu:XFDU>\n"
    )



def _write_cf_grid(*, path: Path, variable: str, data: Any, data_type: int,
                   transform: list[float], scale, offset, nodata, attributes: dict[str, str]) -> None:
    """One variable on a regular lat/lon grid, as a CF netCDF file."""
    import numpy as np

    height, width = data.shape
    longitudes = transform[0] + (np.arange(width) + 0.5) * transform[1]
    latitudes = transform[3] + (np.arange(height) + 0.5) * transform[5]

    dataset = gdal.GetDriverByName("netCDF").CreateMultiDimensional(str(path))
    root = dataset.GetRootGroup()
    dim_y = root.CreateDimension("lat", "HORIZONTAL_Y", "NORTH", height)
    dim_x = root.CreateDimension("lon", "HORIZONTAL_X", "EAST", width)
    double = gdal.ExtendedDataType.Create(gdal.GDT_Float64)

    axis_y = root.CreateMDArray("lat", [dim_y], double)
    axis_y.SetUnit("degrees_north")
    axis_y.Write(latitudes.astype(np.float64).tobytes())
    axis_x = root.CreateMDArray("lon", [dim_x], double)
    axis_x.SetUnit("degrees_east")
    axis_x.Write(longitudes.astype(np.float64).tobytes())

    array = root.CreateMDArray(variable, [dim_y, dim_x], gdal.ExtendedDataType.Create(data_type))
    # Packing and fill value before the data: netCDF refuses to define a fill
    # value once the variable holds anything.
    if nodata is not None:
        array.SetNoDataValueDouble(nodata)
    if scale not in (None, 1.0):
        array.SetScale(scale)
    if offset not in (None, 0.0):
        array.SetOffset(offset)
    for key, value in attributes.items():
        attribute = array.CreateAttribute(key, [], gdal.ExtendedDataType.CreateString())
        attribute.Write(str(value))
    array.Write(data.tobytes())
    dataset = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory (default: tests/data)")
    parser.add_argument("--days", type=int, default=7, help="how far back to search the catalogue (default: 7)")
    parser.add_argument("--window", type=int, default=512, help="extract size in pixels (default: 512)")
    parser.add_argument("--platform", choices=["s1", "s2", "s5p", "s3", "both", "all"], default="both")
    parser.add_argument("--s3-source", metavar="PATH_OR_URL",
                        help="a Synergy VG1/V10 product to cut the test window from")
    parser.add_argument("--s5p-source", metavar="PATH_OR_URL",
                        help="an S5P NO2 granule to cut the test window from (local path or /vsicurl/ URL)")
    parser.add_argument("--s5p-scanlines", type=int, default=60,
                        help="how many scanlines to keep in the S5P window (default: 60)")
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
