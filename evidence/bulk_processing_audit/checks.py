"""Audit bug checks (GDAL 3.8, synthetic data). Driven by run.sh checks; see PLANNING_bulk_processing.md."""
import sys, os, tempfile, threading, json
from pathlib import Path
import numpy as np
import rasterio
from rasterio.transform import from_origin
from osgeo import gdal

tmp = Path(tempfile.mkdtemp())


def synth(path, count, dtype, data, nodata=0, crs="EPSG:32633"):
    h, w = data.shape[-2:]
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=count, dtype=dtype,
                       crs=crs, transform=from_origin(0, 1000, 10, 10), nodata=nodata) as dst:
        dst.write(data if data.ndim == 3 else data[None])
    return path


def s2_nodata_collision():
    from pysent.s2 import _write_stretched_sentinel_s2_rgb
    rng = np.random.default_rng(0)
    d = rng.integers(1000, 5000, size=(3, 256, 256)).astype("uint16")
    d[:, :10, :] = 0  # fill strip
    d[:, 100, 100] = 1000  # a valid pixel at the band minimum
    w = synth(tmp / "w.tif", 3, "uint16", d)
    out = tmp / "o.tif"
    _write_stretched_sentinel_s2_rgb(w, out, percentiles=(2, 98), block_size=256, overview_factors=(2,))
    with rasterio.open(out) as src:
        a = src.read()
        print("output nodata:", src.nodata)
        valid = np.any(d != 0, axis=0)
        holes = int(np.sum(np.all(a == 0, axis=0) & valid))
        partial = int(np.sum(np.any(a == 0, axis=0) & valid))
        print("valid input pixels rendered as nodata (all bands 0):", holes, "| with >=1 band 0:", partial)


def s2_all_nodata():
    from pysent.s2 import _write_stretched_sentinel_s2_rgb
    w = synth(tmp / "e.tif", 3, "uint16", np.zeros((3, 64, 64), "uint16"))
    try:
        _write_stretched_sentinel_s2_rgb(w, tmp / "eo.tif", percentiles=(2, 98), block_size=64, overview_factors=(2,))
        print("no error")
    except Exception as e:
        print("RAISED", type(e).__name__, e)


def cachemax():
    print("initial", gdal.GetCacheMax() // 2**20, "MB")
    gdal.Open(str(synth(tmp / "c.tif", 1, "uint16", np.ones((64, 64), "uint16")))).ReadAsArray()
    os.environ["GDAL_CACHEMAX"] = "64"
    try:
        from pysent.s2 import _configure_gdal_runtime  # before the phase 1 fix
        _configure_gdal_runtime()
        print("after _configure_gdal_runtime(GDAL_CACHEMAX=64):", gdal.GetCacheMax() // 2**20, "MB")
    except ImportError:
        from pysent._runtime import gdal_runtime, resolve_gdal_runtime
        with gdal_runtime(**resolve_gdal_runtime({}, None, "1")):
            print("during a processing call with GDAL_CACHEMAX=64:", gdal.GetCacheMax() // 2**20, "MB")


def numba_threads():
    from pysent.s1 import stretch_sentinel_s1_grayscale
    d = np.random.default_rng(0).random((4000, 4000), dtype=np.float32) + 0.1
    errors = []
    def run():
        try:
            for _ in range(5):
                stretch_sentinel_s1_grayscale(d, nodata=0.0, use_numba=True)
        except Exception as e:
            errors.append(e)
    ts = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    print("completed, errors:", errors)


def s1_jpeg_alpha():
    from pysent.s1 import _write_quicklook_from_warped
    rng = np.random.default_rng(0)
    d = (rng.random((512, 512), dtype=np.float32) * 300 + 10)
    d[:, :200] = 0  # swath edge
    w = synth(tmp / "s1w.tif", 1, "float32", d)
    out = tmp / "s1o.tif"
    _write_quicklook_from_warped(w, out)  # default compression="jpeg"
    with rasterio.open(out) as src:
        alpha = src.read(2)
        print("compression:", src.compression, "| alpha unique count:", len(np.unique(alpha)),
              "| alpha values not in {0,255}:", int(np.sum((alpha != 0) & (alpha != 255))))


def jp2_threads():
    drv = gdal.GetDriverByName("JP2OpenJPEG")
    print("JP2OpenJPEG present:", drv is not None)
    print("GDAL_NUM_THREADS config:", gdal.GetConfigOption("GDAL_NUM_THREADS"))
    opts = drv.GetMetadataItem("DMD_OPENOPTIONLIST") or ""
    print("NUM_THREADS in open options:", "NUM_THREADS" in opts)
    print([l.strip() for l in opts.splitlines() if "THREAD" in l])


def nested_pools():
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing import Pool
    from pysent.s1 import _resolve_sentinel_s1_processing_settings as r
    opts = {"parallel_mode": "processes"}
    with ProcessPoolExecutor(1) as ex:
        s = ex.submit(r, opts, output_count=2).result()
        print("inside ProcessPoolExecutor worker ->", s["parallel_mode"], "workers", s["product_workers"], "gdal threads", s["gdal_num_threads"])
    with Pool(1) as p:
        s = p.apply(r, (opts,), {"output_count": 2})
        print("inside multiprocessing.Pool worker ->", s["parallel_mode"], "workers", s["product_workers"], "gdal threads", s["gdal_num_threads"])


def s1_tps_option():
    import inspect
    from pysent import s1
    print("use_tps reachable from process_sentinel_s1_safe:", "use_tps" in inspect.getsource(s1._process_sentinel_s1_safe_product))


def s2_percentiles_option():
    from pysent.s2 import _write_stretched_sentinel_s2_rgb
    rng = np.random.default_rng(1)
    d = rng.integers(1000, 5000, size=(3, 128, 128)).astype("uint16")
    d[0, 0, 0] = 60000  # one bright outlier (cloud / glint)
    w = synth(tmp / "p.tif", 3, "uint16", d)
    a = _write_stretched_sentinel_s2_rgb(w, tmp / "p1.tif", percentiles=(2, 98), block_size=128, overview_factors=(2,))
    b = _write_stretched_sentinel_s2_rgb(w, tmp / "p2.tif", percentiles=(10, 90), block_size=128, overview_factors=(2,))
    print("p_high with (2,98):", a["p_high"], "| with (10,90):", b["p_high"])
    with rasterio.open(tmp / "p1.tif") as src:
        print("red band mean after stretch (outlier present):", float(src.read(1).mean()))


def partial_warp_on_failure():
    from pysent import s1
    rng = np.random.default_rng(0)
    d = rng.random((64, 64), dtype=np.float32) + 1
    w = synth(tmp / "fw.tif", 1, "float32", d)
    out = tmp / "fo.tif"
    try:
        s1._write_quicklook_from_warped(w, out, compression="NOT_A_CODEC")
    except Exception as e:
        print("raised:", type(e).__name__)
    print("files left:", sorted(p.name for p in tmp.iterdir()))


if __name__ == "__main__":
    globals()[sys.argv[1]]()
