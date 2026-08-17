# Tuning notes and roadmap

Where the processing stands, what it costs, and which changes are worth making
next. Written for whoever picks this up: the measurements are real, taken on
actual products, and every open item below is measurable with
[04_benchmarks.ipynb](notebooks/04_benchmarks.ipynb).

> This was the handoff document of the `test_nbs_pipeline/` QA bundle in the
> FastAPI-mapserver repository. That bundle has been retired: its notebook is now
> [04_benchmarks.ipynb](notebooks/04_benchmarks.ipynb) and its helpers are
> `pysent.qa`. Section numbering is kept so older references still resolve.

## 3. How it maps to production code

The routines the notebook calls are the ones the FastAPI ingestion jobs run —
literally the same objects, imported from the `pysent` package (§1). The headings
below name the library module to edit when pushing a change back (§6).

### Sentinel-1 — `pysent.s1`
- `_warp_sentinel_s1_safe_amplitude()` — opens the SAFE `manifest.safe`, picks the
  polarisation band, warps to `EPSG:32661` @ 40 m, **Float32**, `resampleAlg=average`,
  using **`tps=True`** (thin-plate-spline geolocation — accurate but the dominant
  cost). The NetCDF variant `_warp_sentinel_s1_amplitude()` uses `geoloc=True`.
- `stretch_sentinel_s1_grayscale()` — percentile (default 2–98) clip → uint8 gray
  + alpha. Has an optional **numba** fast path (`use_numba`, env `S1_USE_NUMBA`).
- Supported variables: `S1_SUPPORTED_AMPLITUDE_VARIABLES` now covers all four —
  `("Amplitude_VH", "Amplitude_VV", "Amplitude_HH", "Amplitude_HV")` — so **VV/VH (IW SDV)**,
  **HH/HV (EW SDH)** and single-pol products are all handled. The warp/band-index code matches the
  band's `POLARIZATION` metadata, and `pysent.s1.detect_sentinel_s1_polarizations()` reads
  the manifest to list what's actually present. The router (`add_dataset_from_nbs_sentinel_safe_csw`)
  defaults the selection to the detected polarisations; `process_sentinel_s1_safe()` also filters to
  present ones (clear error if none). The **notebook** mirrors this via
  `pysent.qa.list_s1_polarizations(safe_input)` (delegates to the library detect, plus a filename-mode
  fallback for offline use).
- Job entry: `process_sentinel_s1_safe()` (and `_netcdf`), driven from
  `core/jobs.py::_create_datasets_from_sentinel_s1_config`.

### Sentinel-2 — `pysent.s2`
- `_warp_sentinel_s2_rgb()` — builds a 3-band stacked VRT (`_build_sentinel_s2_stack_vrt`,
  one VRT per band then `BuildVRT(separate=True, resolution="highest")`), warps to
  the bands' native UTM, **UInt16**, `resampleAlg=bilinear`.
- `stretch_sentinel_s2_rgb()` — per-band percentile (2–98) clip → uint8 RGB.
  **The notebook uses this.**
- `S2_DEFAULT_PRODUCTS` — the 3 shipped combos (see §5).
- `S2_SUPPORTED_BANDS` — the validation allow-list used by
  `normalize_sentinel_s2_product_map()`.
- Job entry: `process_sentinel_s2_safe()`, driven from
  `core/jobs.py::_create_datasets_from_sentinel_s2_config`.

### UUID → SAFE resolution — `pysent.archive`
`resolve_safe_archive_from_uuid()` combines `pysent.csw.get_record_by_id` with
`extract_download_url` / `map_archive_download_to_local`. `routers/datasets.py`
calls the same helpers, so the path the notebook gets is the path the ingestion
pipeline feeds to GDAL. Archive layout:
`<ARCHIVE_ROOT>/<platform>/YYYY/MM/DD/<PRODUCT>.zip`.

### ⚠️ Two deliberate deviations from production (know these)
1. **S2 stretch method.** The *active* ingestion path
   (`_write_stretched_sentinel_s2_rgb`) stretches with **per-band min/max**, **not**
   percentiles. (As of 2026-06-02 it computes the min/max explicitly and writes a
   **tiled + compressed** 8-bit GeoTIFF returning the src range — previously it was a
   bare `scaleParams=[[]]` that dropped tiling/compression and returned no stats; that
   bug plus a `NameError` in the non-stretch branch are fixed. The percentile
   reference impl lives in `_write_stretched_sentinel_s2_rgb_percentile`.) Min/max is
   outlier-sensitive; percentile is almost always better. **A/B it in the notebook via
   `S2_STRETCH_METHOD="minmax"` vs `"percentile"`** before switching production (§4.2).
2. **Compression.** Notebook writes final as lossless **DEFLATE** so JPEG artifacts
   don't confound QA; production defaults to lossy **JPEG**. Flip `compress=` in the
   processing cell to compare delivery output.

---

## 4. The three goals — where to push

### Measured baseline (real products, this machine, serial, gdal_num_threads=2)

| Product | Raster | warp | read | stretch | write+ovr | total |
|---------|--------|-----:|-----:|--------:|----------:|------:|
| S2 L1C true-colour (B4/B3/B2) | 10980² @10 m | **19.8 s** | 5.6 s | 8.0 s | 8.2 s | ~42 s |
| S1 GRD VV amplitude | 7490×5523 @40 m | **26.8 s** | 0.9 s | 1.1 s | 2.5 s | ~31 s |

**Takeaway: the warp dominates.** S2 `stretch` is also non-trivial (full-res
`np.percentile` over 3×10980²). Use the benchmark table in each notebook to
re-measure after any change — that's the whole point of the harness.

### 4.1 Performance
Levers, roughly in impact order:
- **Warp threading / memory.** `gdal_num_threads` (→ `NUM_THREADS` warp option) and
  `warp_memory_limit_mb` (→ `warpMemoryLimit`). Already `multithread=True`. Sweep these
  in the notebook params and read the `warp_raw` row.
- **Product-level parallelism.** Production `process_sentinel_s2_safe` /
  `_s1` fan out across products with a `ThreadPoolExecutor`
  (`S2_PRODUCT_WORKERS` / `S1_PRODUCT_WORKERS`, `*_PARALLEL_MODE`). The **notebooks
  run products serially** for readable timing — parallelise the run loop if you want
  end-to-end wall-clock closer to production.
- **S1 GCP warp transform.** SAFE path uses `tps=True` (thin-plate-spline, expensive). The SAFE
  band VRT is **GCP-based** (not a geolocation array — so `geoloc=True` does NOT apply here; that's
  the NetCDF path), so the fast alternative is GDAL's **polynomial GCP** transform. Likely the
  biggest single S1 speed win. **Now benchmarkable in the notebook:** set `S1_USE_TPS=False` (the
  warp gained a `use_tps` param, default True=tps, False=polynomial GCP) and
  compare the `warp_raw` `seconds`/`rss_peak_mb`.
- **Resampling cost.** `average` > `bilinear` > `near`. Coarsening `target_resolution`
  cuts warp cost ~quadratically — quantify the quality cost before shipping.
- **Stretch cost (S2).** `np.percentile` on the full array is the cost. Estimating
  percentiles from a **decimated read** (`rasterio` `out_shape=` / overview) or a random
  subsample is a large speedup at negligible accuracy loss — `raster_stats()` already
  subsamples and is a good template. The S1 numba path (`use_numba=True`) is the
  analogous win for S1.
- **Output format.** Consider Cloud-Optimized GeoTIFF (`driver="COG"`) and tune
  overview factors / compression; measure `write_stretched` + `out_size_mb`.

### 4.2 Output quality
- **Standardise S2 on percentile stretch** (vs the active min/max) — see §3 deviation 1.
  Min/max blows out with a few bright pixels (clouds, sunglint). **A/B now via
  `S2_STRETCH_METHOD`**; to ship, wire `_write_stretched_sentinel_s2_rgb_percentile` into
  `_process_sentinel_s2_product`.
- **SAR should likely be stretched in dB.** S1 amplitude is currently stretched
  **linearly**; SAR backscatter spans orders of magnitude, so `20*log10(amplitude)`
  (or `10*log10(intensity)`) before percentile clip typically gives far better
  contrast. **Now available in the notebook: `S1_DB_SCALE=True`** (uses
  `squ.stretch_s1_grayscale_db`); validate the `range_used_%` gain, then port to
  `pysent.s1.stretch_sentinel_s1_grayscale`.
- **Speckle.** Optional Lee / refined-Lee filter on S1 before stretch.
- **Per-band vs joint stretch.** Per-band (current) maximises contrast but can shift
  colour balance; a shared/luminance-preserving stretch keeps truer colour. Offer both.
- **Gamma / tone curve** after the linear clip for perceptual brightness.
- **Resampling for band-resolution mixing** (20 m → 10 m): `cubic` vs `bilinear`.
- **Bit depth.** Keep a 16-bit analysis product alongside the 8-bit display product?
- **Nodata / partial tiles.** Real scenes are partial (valid frac ~0.54 S2, ~0.67 S1);
  make sure stretch percentiles ignore fill and edges (they currently mask `<=0`).

### 4.3 More products (mostly S2 band combinations) — the main ask
**Adding a combo is intentionally trivial:**
- *In the notebook:* edit the `PRODUCTS` dict (Inputs/params cell). Any band in
  `S2_SUPPORTED_BANDS` works.
- *In production:* add to `S2_DEFAULT_PRODUCTS` in `pysent/s2.py`, and/or pass
  `products_json` to the `nbs-sentinel-safe-csw` endpoint. `normalize_sentinel_s2_product_map`
  validates names/bands and upper-cases them.

**Sentinel-2 band reference** (native resolution; combos resample to the finest used):

| Band | λ (nm) | Res | Name | | Band | λ (nm) | Res | Name |
|------|-------|-----|------|-|------|-------|-----|------|
| B1 | 443 | 60 m | coastal aerosol | | B7 | 783 | 20 m | red edge 3 |
| B2 | 490 | 10 m | blue | | B8 | 842 | 10 m | NIR (wide) |
| B3 | 560 | 10 m | green | | B8A | 865 | 20 m | NIR (narrow) |
| B4 | 665 | 10 m | red | | B9 | 945 | 60 m | water vapour |
| B5 | 705 | 20 m | red edge 1 | | B10 | 1375 | 60 m | cirrus (**L1C only**) |
| B6 | 740 | 20 m | red edge 2 | | B11 | 1610 | 20 m | SWIR 1 |
|    |     |      |             | | B12 | 2190 | 20 m | SWIR 2 |

**Shipped today** (`S2_DEFAULT_PRODUCTS`):
`true_color_vegetation` = B4/B3/B2 · `false_color_glacier` = B12/B8A/B3 ·
`false_color_vegetation` = B8A/B4/B3.

**Good candidates to add** (R/G/B):
- `color_infrared` = **B8/B4/B3** — classic vegetation NIR composite
- `agriculture` = **B11/B8/B2**
- `healthy_vegetation` = **B8/B11/B2**
- `geology` = **B12/B11/B2**
- `swir` = **B12/B8A/B4**
- `bathymetric` = **B4/B3/B1**
- `shortwave_ir` = **B12/B8/B4**
- red-edge composites using **B5/B6/B7** for vegetation stress

Caveats when adding combos:
- **L1C vs L2A**: B10 exists only in L1C; some scenes are L2A (atmospherically
  corrected) — bands present differ. The product name in the archive encodes it
  (`MSIL1C` vs `MSIL2A`). Validate the band is present before promoting a combo.
- **Resolution mixing**: any 20 m/60 m band upsamples the composite's coarse channel;
  watch the `res_m`/size columns and pick resampling accordingly (§4.2).
- **Index products** (NDVI, NDWI, NDMI…) are *not* RGB composites; they'd need a new
  single-band code path + colormap, not just a `PRODUCTS` entry. Flag as a larger task.

---

## 5. Verifying changes
- **Fast / offline:** `python smoke_test.py` (from inside this directory) — proves
  imports + read/stretch/write/overviews on synthetic data. Run after any helper edit.
- **Real data:** set `SAFE_PATH_OVERRIDE` (or a UUID) to a product under the archive
  and Run All; compare the benchmark table and imagery before/after.
- Sample products confirmed present on the lustre mount:
  `S2A/2022/03/19/S2A_MSIL1C_20220319T132851_N0400_R024_T31XEK_20220319T135444.zip`,
  `S1A/2022/10/25/IW/S1A_IW_GRDH_1SDV_20221025T050406_20221025T050431_045594_057398_B5C2.zip`.

## 6. Pushing improvements back into production
The notebook is for iterating; the shipping code is the [`pysent`](https://github.com/metno/pysent)
library (`pysent/src/pysent/s1.py`, `s2.py`). The service consumes it through
thin shims (`core/sentinel_s1.py`, `core/sentinel_s2.py`), so **there is no
second copy to update** — a change in the library reaches both the notebook and
the pipeline. Once a change proves out here:

1. Install a checkout of the library over the pinned one so the notebook
   benchmarks your edits:
   ```bash
   pip install --no-deps -e /path/to/pysent   # then RESTART THE KERNEL
   ```
2. Port the stretch/warp/param change into the corresponding `pysent` function
   and add a test under `pysent/tests/` (the processing tests build synthetic
   rasters — no SAFE product needed). Run `pytest` in the library repo.
3. For S2, decide whether to switch the active writer from min/max to percentile
   (`_write_stretched_sentinel_s2_rgb` vs the reference
   `_write_stretched_sentinel_s2_rgb_percentile` + `stretch_sentinel_s2_rgb`).
4. New S2 combos → `S2_DEFAULT_PRODUCTS` in `pysent/s2.py` (+ tests in the app's
   `tests/test_datasets_api.py`, which already mocks `process_sentinel_s2_safe`).
5. Re-run the notebook against the same UUID to confirm parity.
6. Ship it: tag the library, bump the pin in the app's `requirements.txt` /
   `Dockerfile` (`PYSENT_SPEC`) and in this bundle's `requirements.txt` /
   `environment.yml`, then run the app's `pytest` suite.
