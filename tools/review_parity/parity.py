"""Re-render a review study's default pictures with this checkout of pysent, and compare.

The Sentinel-3 and Sentinel-5P review studies (``review-study/1`` manifests: a
``manifest.json`` and an ``images/`` tree) were rendered once, by one pysent
commit on one GDAL. Their defaults were then approved from those pictures. Before
a service ships those defaults on another pysent and another GDAL, this checks
that the same request still draws the same map:

``render``
    For every N-th scene of each product, run the scene's **default** request
    exactly as the study did and keep the styled GeoTIFF, a lossless WebP made
    the study's way (longest side 1024 px, nearest neighbour, the same crop) and
    the statistics the study prints under the picture. Sentinel-3: the archive
    frame as it is (truecolor with the ``quicklook`` preset). Sentinel-5P: the
    study's along-track window of the OFFL orbit, cut into a small NetCDF with
    the source's layout and packing.

``compare``
    Line each run up against the study (and, given two runs, against each other,
    to tell a pysent change from a GDAL change): grid, cells with a value,
    p1/p50/p99, and the pictures decoded pixel by pixel.

Run it where both GDAL and the archive are, e.g. in the service image::

    docker run --rm -v /lustre/storeB/NBS2:/lustre/storeB/NBS2:ro -v local_data_store:/data:ro \\
        -v "$PWD:/src:ro" -v /mnt/data/parity:/out -e PYTHONPATH=/src/src <image> \\
        python3 /src/tools/review_parity/parity.py render --manifest /data/review/s3-l2/manifest.json \\
        --out /out/s3-gdal313 --every 10

Nothing here writes outside ``--out``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from multiprocessing import Pool
from pathlib import Path
from typing import Any

ARCHIVE = "/lustre/storeB/NBS2/sentinel/production/NorwAREA/nbsArchive"
IMAGE_MAX = 1024
S5P_CONTEXT = ("/PRODUCT/latitude", "/PRODUCT/longitude", "/PRODUCT/qa_value")


# --------------------------------------------------------------------------- the study


def load_study(manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    family = "s3" if manifest["study"]["id"].startswith("s3") else "s5p"
    keys = {}
    for product in manifest["products"]:
        match = re.search(r"pysent product '([^']+)'", product.get("notes", ""))
        keys[product["id"]] = match.group(1) if match else product["id"]
    defaults = {r["scene"]: r for r in manifest["renders"] if r["decision"] is None}
    return {"manifest": manifest, "family": family, "keys": keys, "defaults": defaults}


def pick_scenes(study: dict[str, Any], products: list[str] | None, every: int) -> list[dict[str, Any]]:
    """Every ``every``-th scene of each product that has a default render, in the study's order."""
    by_product: dict[str, list[dict[str, Any]]] = {}
    for scene in study["manifest"]["scenes"]:
        if scene["id"] in study["defaults"]:
            by_product.setdefault(scene["product"], []).append(scene)
    picked = []
    for product, scenes in by_product.items():
        if products and product not in products:
            continue
        picked += scenes[::every]
    return picked


# --------------------------------------------------------------------------- rendering


def _image(styled: Path, out: Path, window: list[int] | None) -> tuple[int, int]:
    """The study's picture: lossless WebP, longest side ``IMAGE_MAX``, nearest neighbour."""
    from osgeo import gdal

    source = gdal.Open(str(styled))
    x0, y0, width, height = window or [0, 0, source.RasterXSize, source.RasterYSize]
    scale = min(1.0, IMAGE_MAX / max(width, height))
    w, h = max(1, round(width * scale)), max(1, round(height * scale))
    options: dict[str, Any] = {"format": "WEBP", "srcWin": [x0, y0, width, height], "width": w, "height": h,
                               "resampleAlg": "nearest", "creationOptions": ["LOSSLESS=YES"]}
    if source.RasterCount == 1:
        options["rgbExpand"] = "rgba"
    gdal.Translate(str(out), source, **options)
    source = None
    Path(f"{out}.aux.xml").unlink(missing_ok=True)
    return w, h


def _cut(array, window):
    if window is None:
        return array
    x0, y0, w, h = window
    return array[y0:y0 + h, x0:x0 + w]


def float_stats(float_path: Path, window: list[int] | None, hide_marginal: bool) -> dict[str, Any]:
    """Grid, cells drawn and their p1/p50/p99 (Sentinel-3 hides marginal cloud in the picture)."""
    import numpy as np
    from osgeo import gdal

    dataset = gdal.Open(str(float_path))
    transform = list(dataset.GetGeoTransform())
    stats: dict[str, Any] = {"grid_width": dataset.RasterXSize, "grid_height": dataset.RasterYSize,
                             "transform": transform, "epsg": None}
    from osgeo import osr

    srs = osr.SpatialReference(wkt=dataset.GetProjection())
    srs.AutoIdentifyEPSG()
    stats["epsg"] = int(srs.GetAuthorityCode(None)) if srs.GetAuthorityCode(None) else None
    values = _cut(dataset.GetRasterBand(1).ReadAsArray(), window)
    drawn = np.isfinite(values)
    if hide_marginal and dataset.RasterCount == 2:
        from pysent._output import _matches_codes
        from pysent.s3 import BROWSE_HIDDEN_REASONS

        codes = _cut(dataset.GetRasterBand(2).ReadAsArray(), window)
        drawn &= ~_matches_codes(codes, BROWSE_HIDDEN_REASONS)
    stats["cells"] = int(values.size)
    stats["drawn_cells"] = int(drawn.sum())
    if dataset.RasterCount == 3:  # truecolor: red, green, blue
        bands = [_cut(dataset.GetRasterBand(i + 1).ReadAsArray(), window) for i in range(3)]
        drawn = np.isfinite(bands[0])
        stats["drawn_cells"] = int(drawn.sum())
        if drawn.any():
            for label, band in zip(("red", "green", "blue"), bands):
                p1, p50, p99 = (float(x) for x in np.percentile(band[drawn], [1, 50, 99]))
                stats.update({f"{label}_p1": p1, f"{label}_p50": p50, f"{label}_p99": p99})
            stats["p50"] = stats["red_p50"]
    elif drawn.any():
        p1, p50, p99 = (float(x) for x in np.percentile(values[drawn], [1, 50, 99]))
        stats.update({"p1": p1, "p50": p50, "p99": p99})
    dataset = None
    return stats


def _s3_request(study: dict[str, Any], scene: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    """The study's default request: the pysent table, the range only where the study's default differs."""
    from pysent.s3 import S3_PRODUCTS

    key = study["keys"][scene["product"]]
    settings = study["defaults"][scene["id"]]["settings"]
    table = S3_PRODUCTS[key]
    request: dict[str, Any] = {}
    if "gamma" in settings:  # truecolor, rendered with the quicklook preset
        return key, request, "quicklook"
    if tuple(float(x) for x in settings["value_range"]) != tuple(float(x) for x in table["value_range"]):
        request["value_range"] = [float(x) for x in settings["value_range"]]
    return key, request, None


def _render_s3(study: dict[str, Any], scene: dict[str, Any], work: Path, archive: str) -> dict[str, Any]:
    from pysent import s3

    key, request, preset = _s3_request(study, scene)
    processing: dict[str, Any] = {"on_empty": "write", "thumbnail": False, "overview_factors": [1],
                                  "gdal_num_threads": "1", "work_dir": str(work), "context": "bulk"}
    if preset:
        processing["preset"] = preset
    result = s3.process_sentinel_s3_safe(input_dataset=f"{archive}/{scene['frame']['path']}", output_dir=work,
                                         products={key: request}, output_names={key: "default.tif"},
                                         processing_options=processing)[0]
    return {"key": key, "request": request, "preset": preset, "path": result["path"],
            "styled_path": result["styled_path"], "hide_marginal": preset is None}


def cut_window(source: str, scan0: int, scan1: int, variables: list[str], out_path: Path,
               times: tuple[str, str]) -> None:
    """The study's S5P scene: rows ``scan0:scan1`` of the orbit, the source's layout and packing kept."""
    from osgeo import gdal
    from pysent._swath import open_array, open_root

    gdal.UseExceptions()
    root = open_root(source)
    windows = {}
    for path in dict.fromkeys(list(S5P_CONTEXT) + variables):
        array = open_array(root, path)
        raw = array.ReadAsArray()
        if raw.ndim >= 3 and raw.shape[0] == 1:
            raw = raw[0]
        windows[path] = {"data": raw[scan0:scan1], "nodata": array.GetNoDataValueAsDouble(),
                         "scale": array.GetScale(), "offset": array.GetOffset()}
    rows, columns = windows[S5P_CONTEXT[0]]["data"].shape
    out_path.unlink(missing_ok=True)
    dataset = gdal.GetDriverByName("netCDF").CreateMultiDimensional(str(out_path))
    top = dataset.GetRootGroup()
    for name, value in zip(("time_coverage_start", "time_coverage_end"), times):
        attribute = top.CreateAttribute(name, [], gdal.ExtendedDataType.CreateString())
        attribute.WriteString(value)
    group = top.CreateGroup("PRODUCT")
    scanline = group.CreateDimension("scanline", None, None, rows)
    ground_pixel = group.CreateDimension("ground_pixel", None, None, columns)
    types = {"float32": gdal.GDT_Float32, "float64": gdal.GDT_Float64, "uint8": gdal.GDT_Byte}
    for path, window in windows.items():
        data = window["data"]
        array = group.CreateMDArray(path.rsplit("/", 1)[-1], [scanline, ground_pixel],
                                    gdal.ExtendedDataType.Create(types[str(data.dtype)]))
        if window["nodata"] is not None:  # netCDF refuses a fill value once the variable holds data
            array.SetNoDataValueDouble(window["nodata"])
        if window["scale"] not in (None, 1.0):
            array.SetScale(window["scale"])
        if window["offset"] not in (None, 0.0):
            array.SetOffset(window["offset"])
        array.Write(data.tobytes())
    dataset = None


def _render_s5p(study: dict[str, Any], scene: dict[str, Any], work: Path, archive: str) -> dict[str, Any]:
    from pysent.s5p import process_sentinel_s5p_netcdf

    key = study["keys"][scene["product"]]
    settings = study["defaults"][scene["id"]]["settings"]
    window = scene["window"]
    nc = work / "window.nc"
    cut_window(f"{archive}/S5P/{window['path']}", window["scanlines"][0], window["scanlines"][1],
               [settings["variable"]], nc, (scene["time"], window["time_end"]))
    request = {"qa_min": settings["qa_min"], "value_range": list(settings["value_range"]),
               "colormap": settings["colormap"], "resampling": "average", "grid": {"epsg": "auto"}}
    result = process_sentinel_s5p_netcdf(input_dataset=str(nc), output_dir=work, products={key: request},
                                         output_names={key: "default.tif"},
                                         processing_options={"on_empty": "write", "thumbnail": False,
                                                             "overview_factors": [], "gdal_num_threads": "1",
                                                             "work_dir": str(work)})[0]
    nc.unlink(missing_ok=True)
    return {"key": key, "request": request, "preset": None, "path": result["path"],
            "styled_path": result["styled_path"], "hide_marginal": False}


def render_one(job: dict[str, Any]) -> dict[str, Any]:
    os.environ["GDAL_NUM_THREADS"] = "1"
    from osgeo import gdal

    gdal.UseExceptions()
    study, scene, out, archive = job["study"], job["scene"], Path(job["out"]), job["archive"]
    product_dir = out / scene["product"]
    product_dir.mkdir(parents=True, exist_ok=True)
    record_path = product_dir / f"{scene['id']}.json"
    if record_path.exists():
        return json.loads(record_path.read_text(encoding="utf-8"))
    work = out / ".work" / scene["id"]
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    os.environ["CPL_TMPDIR"] = str(work)
    started = time.time()
    record: dict[str, Any] = {"scene": scene["id"], "product": scene["product"]}
    try:
        run = (_render_s3 if study["family"] == "s3" else _render_s5p)(study, scene, work, archive)
        crop = study["defaults"][scene["id"]].get("crop")
        styled = product_dir / f"{scene['id']}_styled.tif"
        shutil.copy(run["styled_path"], styled)
        _image(styled, product_dir / f"{scene['id']}.webp", crop)
        record.update({"key": run["key"], "request": run["request"], "preset": run["preset"], "crop": crop,
                       "stats": float_stats(Path(run["path"]), crop, run["hide_marginal"])})
    except Exception as exc:  # one failed scene is reported, never fatal
        record["error"] = f"{type(exc).__name__}: {exc}"
    record["seconds"] = round(time.time() - started, 1)
    shutil.rmtree(work, ignore_errors=True)
    record_path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    return record


def cmd_render(args: argparse.Namespace) -> int:
    from osgeo import gdal

    import pysent

    study = load_study(args.manifest)
    scenes = pick_scenes(study, args.products, args.every)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "run.json").write_text(json.dumps({
        "manifest": str(args.manifest), "study": study["manifest"]["study"]["id"],
        "study_commits": study["manifest"]["study"]["produced_by"]["commits"],
        "study_gdal": study["manifest"]["study"]["produced_by"]["versions"]["gdal"],
        "gdal": gdal.__version__, "pysent": getattr(pysent, "__version__", "?"),
        "pysent_commit": os.environ.get("PYSENT_COMMIT", ""), "every": args.every,
        "scenes": [s["id"] for s in scenes]}, indent=1), encoding="utf-8")
    jobs = [{"study": study, "scene": s, "out": str(args.out), "archive": args.archive} for s in scenes]
    print(f"{len(jobs)} scenes, {args.workers} workers, GDAL {gdal.__version__}", flush=True)
    with Pool(args.workers, maxtasksperchild=4) as pool:
        for count, record in enumerate(pool.imap_unordered(render_one, jobs), start=1):
            print(f"[{count}/{len(jobs)}] {record['scene']} {record['seconds']} s {record.get('error', '')}", flush=True)
    return 0


# --------------------------------------------------------------------------- comparing


def _picture(path: Path):
    import numpy as np
    from osgeo import gdal

    dataset = gdal.Open(str(path))
    array = np.stack([dataset.GetRasterBand(i + 1).ReadAsArray() for i in range(dataset.RasterCount)])
    dataset = None
    return array.astype(np.int16)


def picture_diff(a: Path, b: Path) -> dict[str, Any]:
    """Share of picture pixels that differ at all, differ in coverage (alpha), and by more than 8 levels."""
    import numpy as np

    pa, pb = _picture(a), _picture(b)
    if pa.shape != pb.shape:
        return {"same_shape": False, "shape_a": list(pa.shape), "shape_b": list(pb.shape)}
    diff = np.abs(pa - pb)
    any_ = diff.max(axis=0) > 0
    out = {"same_shape": True, "pixels": int(any_.size), "differ": float(any_.mean()),
           "differ_gt8": float((diff.max(axis=0) > 8).mean())}
    if pa.shape[0] == 4:
        out["coverage_differs"] = float(((pa[3] > 0) != (pb[3] > 0)).mean())
    return out


def _rel(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    if a == b:
        return 0.0
    return abs(a - b) / max(abs(a), abs(b), 1e-30)


def study_stats(study: dict[str, Any], scene_id: str) -> dict[str, Any]:
    stats = study["defaults"][scene_id]["stats"]
    drawn = stats.get("drawn_cells", stats.get("valid_cells"))
    return {"grid_width": stats["grid_width"], "grid_height": stats["grid_height"], "drawn_cells": drawn,
            "p1": stats.get("p1", stats.get("red_p1")), "p50": stats.get("p50"), "p99": stats.get("p99", stats.get("red_p99")),
            "epsg": stats.get("epsg")}


def compare_pair(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = {"same_grid": (a["grid_width"], a["grid_height"]) == (b["grid_width"], b["grid_height"]),
           "drawn_a": a["drawn_cells"], "drawn_b": b["drawn_cells"],
           "drawn_rel": _rel(a["drawn_cells"], b["drawn_cells"])}
    for key in ("p1", "p50", "p99"):
        out[f"{key}_rel"] = _rel(a.get(key), b.get(key))
    return out


def cmd_compare(args: argparse.Namespace) -> int:
    study = load_study(args.manifest)
    runs = [Path(r) for r in args.runs]
    rows = []
    for record_path in sorted(runs[0].glob("*/*.json")):
        record = json.loads(record_path.read_text(encoding="utf-8"))
        scene_id, product = record["scene"], record["product"]
        row: dict[str, Any] = {"scene": scene_id, "product": product}
        if "error" in record:
            row["error"] = record["error"]
            rows.append(row)
            continue
        image = study["defaults"][scene_id]["image"]
        row["vs_study"] = compare_pair(study_stats(study, scene_id), record["stats"])
        row["vs_study"]["picture"] = picture_diff(args.study_root / image, record_path.with_suffix(".webp"))
        for other in runs[1:]:
            other_path = other / product / f"{scene_id}.json"
            if not other_path.exists():
                continue
            other_record = json.loads(other_path.read_text(encoding="utf-8"))
            if "error" in other_record:
                row["vs_other"] = {"error": other_record["error"]}
                continue
            pair = compare_pair(other_record["stats"], record["stats"])
            pair["picture"] = picture_diff(other_path.with_suffix(".webp"), record_path.with_suffix(".webp"))
            pair["styled"] = picture_diff(other_path.with_name(f"{scene_id}_styled.tif"),
                                          record_path.with_name(f"{scene_id}_styled.tif"))
            row["vs_other"] = pair
        rows.append(row)
    args.out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(summary(rows))
    return 0


def summary(rows: list[dict[str, Any]]) -> str:
    """One line per product: scenes, identical pictures, worst differences."""
    lines = ["| product | scenes | errors | same grid | pictures identical | worst picture differ | worst drawn rel | worst p50 rel |"
             + (" vs other: identical styled | worst styled differ |" if any("vs_other" in r for r in rows) else ""),
             "|---|---:|---:|---:|---:|---:|---:|---:|" + ("---:|---:|" if any("vs_other" in r for r in rows) else "")]
    products = sorted({r["product"] for r in rows})
    for product in products:
        group = [r for r in rows if r["product"] == product]
        ok = [r for r in group if "vs_study" in r]
        pics = [r["vs_study"]["picture"] for r in ok]
        identical = sum(1 for p in pics if p.get("same_shape") and p["differ"] == 0)
        worst_pic = max((p.get("differ", 1.0) if p.get("same_shape") else 1.0 for p in pics), default=0)
        worst_drawn = max((r["vs_study"]["drawn_rel"] or 0 for r in ok), default=0)
        worst_p50 = max((r["vs_study"]["p50_rel"] or 0 for r in ok), default=0)
        line = (f"| {product} | {len(group)} | {len(group) - len(ok)} | {sum(r['vs_study']['same_grid'] for r in ok)} "
                f"| {identical} | {worst_pic:.4%} | {worst_drawn:.4%} | {worst_p50:.4%} |")
        others = [r["vs_other"] for r in ok if "vs_other" in r and "styled" in r["vs_other"]]
        if others:
            styled = [o["styled"] for o in others]
            same = sum(1 for s in styled if s.get("same_shape") and s["differ"] == 0)
            worst = max((s.get("differ", 1.0) if s.get("same_shape") else 1.0 for s in styled), default=0)
            line += f" {same}/{len(others)} | {worst:.4%} |"
        lines.append(line)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render", help="re-render the study's default for every N-th scene")
    render.add_argument("--manifest", type=Path, required=True)
    render.add_argument("--out", type=Path, required=True)
    render.add_argument("--archive", default=ARCHIVE)
    render.add_argument("--products", type=lambda s: [p for p in s.split(",") if p])
    render.add_argument("--every", type=int, default=10, help="take every N-th scene of each product")
    render.add_argument("--workers", type=int, default=4)
    compare = sub.add_parser("compare", help="compare runs with the study (and the first run with the others)")
    compare.add_argument("--manifest", type=Path, required=True)
    compare.add_argument("--study-root", type=Path, required=True, help="the study directory holding images/")
    compare.add_argument("--runs", nargs="+", required=True)
    compare.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    return cmd_render(args) if args.command == "render" else cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
