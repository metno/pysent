"""Audit benchmarks on real S1/S2 products. Driven by run.sh bench <DATA_DIR>; see TODO_PLANNING_bulk_processing.md."""
import sys, time, zipfile, tempfile, resource, os
from pathlib import Path
from osgeo import gdal

S2 = "/data/S2C_MSIL2A_20260810T092031_N0512_R093_T35VPE_20260810T124910.zip"
S1 = "/data/S1D_IW_GRDH_1SDV_20260810T052300_20260810T052325_004059_007650_5857.zip"
out = Path(tempfile.mkdtemp(dir="/out"))


def report(label, t0, c0):
    ru = resource.getrusage(resource.RUSAGE_SELF)
    cpu = ru.ru_utime + ru.ru_stime - c0
    wall = time.perf_counter() - t0
    print(f"{label}: wall {wall:.1f}s | cpu {cpu:.1f}s | cpu/wall {cpu / wall:.1f}x | peak RSS {ru.ru_maxrss / 1024:.0f} MB")


def clock():
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return time.perf_counter(), ru.ru_utime + ru.ru_stime


def s2(products, **opts):
    from pysent.s2 import S2_DEFAULT_PRODUCTS, process_sentinel_s2_safe
    t0, c0 = clock()
    res = process_sentinel_s2_safe(
        input_dataset=S2, output_dir=out,
        product_bands={p: S2_DEFAULT_PRODUCTS[p] for p in products},
        output_names={p: f"{p}.tif" for p in products},
        processing_options={"histogram_stretch": True, "compression": "DEFLATE", **opts},
    )
    report(f"s2 {products} {opts}", t0, c0)
    for r in res:
        ds = gdal.Open(r["path"])
        print("  ", r["output_file"], ds.RasterXSize, "x", ds.RasterYSize, f"{os.path.getsize(r['path']) / 2**20:.0f} MB")


def s2_one():
    s2(["true_color_vegetation"])


def s2_one_60m():
    s2(["true_color_vegetation"], target_resolution=60)


def s2_three():
    s2(["true_color_vegetation", "false_color_glacier", "false_color_vegetation"])


def jp2_decode():
    from pysent.s2 import _collect_sentinel_s2_band_sources
    src = _collect_sentinel_s2_band_sources(S2)["B4"]
    t0, c0 = clock()
    ds = gdal.Translate(str(out / "b4.tif"), src["subdataset_name"], bandList=[src["band_index"]])
    ds = None
    report(f"decode B4 10m (GDAL_NUM_THREADS={os.environ.get('GDAL_NUM_THREADS')})", t0, c0)


def warp_vs_translate():
    from pysent.s2 import _build_sentinel_s2_stack_vrt
    co = ["COMPRESS=LZW", "TILED=YES", "BLOCKXSIZE=256", "BLOCKYSIZE=256", "BIGTIFF=IF_SAFER", "INTERLEAVE=PIXEL"]
    # Decode once to a plain local stack so the comparison isolates warp cost, not JP2 decode.
    vrt = out / "stack.vrt"
    epsg, res, _ = _build_sentinel_s2_stack_vrt(S2, ("B4", "B3", "B2"), vrt)
    gdal.Translate(str(out / "stack.tif"), str(vrt), creationOptions=["TILED=YES"])
    t0, c0 = clock()
    gdal.Warp(str(out / "w.tif"), str(out / "stack.tif"), dstSRS=epsg, xRes=res, yRes=res, srcNodata=0, dstNodata=0,
              multithread=True, resampleAlg="bilinear", outputType=gdal.GDT_UInt16,
              warpOptions=["NUM_THREADS=8"], creationOptions=co)
    report("warp (same CRS, same res) LZW", t0, c0)
    t0, c0 = clock()
    gdal.Translate(str(out / "t.tif"), str(out / "stack.tif"), creationOptions=co)
    report("translate (no resampling) LZW", t0, c0)
    t0, c0 = clock()
    gdal.Translate(str(out / "t2.tif"), str(out / "stack.tif"), creationOptions=["TILED=YES", "BLOCKXSIZE=256", "BLOCKYSIZE=256"])
    report("translate uncompressed intermediate", t0, c0)


def zip_members():
    with zipfile.ZipFile(S2) as z:
        jp2 = [i for i in z.infolist() if i.filename.endswith(".jp2")]
        kinds = {i.compress_type for i in jp2}
        print("S2 jp2 members:", len(jp2), "compress types:", kinds, "(0=stored, 8=deflate)")
    if Path(S1).exists():
        with zipfile.ZipFile(S1) as z:
            tif = [i for i in z.infolist() if i.filename.endswith(".tiff")]
            print("S1 tiff members:", len(tif), "compress types:", {i.compress_type for i in tif})


def s1(**opts):
    from pysent.s1 import process_sentinel_s1_safe
    t0, c0 = clock()
    res = process_sentinel_s1_safe(
        input_dataset=S1, output_dir=out,
        output_names={"Amplitude_VV": "vv.tif", "Amplitude_VH": "vh.tif"},
        processing_options=opts,
    )
    report(f"s1 VV+VH {opts}", t0, c0)
    for r in res:
        print("  ", r["output_file"], r["width"], "x", r["height"])


def s1_default():
    s1()


def s1_polynomial():
    # use_tps is not reachable through processing_options; patch the default to measure it.
    from pysent import s1 as mod
    orig = mod._warp_sentinel_s1_safe_amplitude
    mod._warp_sentinel_s1_safe_amplitude = lambda *a, **k: orig(*a, use_tps=False, **k)
    s1()


if __name__ == "__main__":
    globals()[sys.argv[1]]()
