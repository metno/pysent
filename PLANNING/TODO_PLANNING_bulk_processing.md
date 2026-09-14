# Bulk SAFE → GeoTIFF processing: audit and plan

**Goal:** let users convert many Sentinel-1/2 SAFE products to GeoTIFF reliably and fast, using
multiprocessing, with a ready-to-use `examples/` directory. First fix the library bugs and costs that
break or slow that use case.

| | |
|---|---|
| Branch / worktree | `feature/bulk-processing` → `../pysent.worktrees/bulk-processing` |
| Base | `main` @ `c660cde` (2026-09-14) |
| Status | Audit done (phase A). Phases 0–5 not started. |
| Audit evidence | [`bulk_processing_audit/`](bulk_processing_audit/): `run.sh checks`, `run.sh bench <DATA_DIR>` |

## Kick-off prompt

Copy this into a new agent session. It can run from any directory.

```text
You are continuing the bulk-processing plan for the pysent library (github.com/metno/pysent).

Repository rules (mandatory):
1. Before anything else, read the repository memory:
   /home/ubuntu/.claude/projects/-home-ubuntu-dev-services-pysent/memory/MEMORY.md
   and every file it links. Use that absolute path: a session started inside the
   worktree has a different default memory directory.
2. Work only in the plan's git worktree:
   /home/ubuntu/dev/services/pysent.worktrees/bulk-processing  (branch feature/bulk-processing)
   If it is missing, run this from /home/ubuntu/dev/services/pysent:
   git worktree add ../pysent.worktrees/bulk-processing feature/bulk-processing
   (add `-b` and `main` if the branch does not exist either). Rebase on origin/main before starting.
3. Read PLANNING/TODO_PLANNING_bulk_processing.md in full. Do the next unchecked phase, in order.
   Tick its checkboxes in the file as you complete them, and record every decision and
   deviation in the "Session log" section.
4. Verify the way CI does: run the full test suite in ubuntu:24.04 with apt GDAL 3.8
   (see .github/workflows/ci.yml). Every bug fix needs a regression test that fails on main.
   PLANNING/bulk_processing_audit/run.sh reproduces the audit findings and benchmarks.
5. Commit once per phase and open one PR per phase against main. Do not merge without the
   user's go-ahead. Never add Claude attribution (no Co-Authored-By trailer, no
   "Generated with Claude Code" footer) to commits or PRs.
6. Do not change default outputs (stretch method, resolution, S1 transform) without asking
   the user first. Add options or presets instead. See "Open questions".
7. At the end of the session, update the memory directory from rule 1 with what this session
   learned: plan status, decisions, gotchas. When every phase is done, rename the file to
   PLANNING/PLANNING_bulk_processing.md.
```

---

## A. Audit report

Scope: `src/pysent/s1.py`, `s2.py`, `_safe.py`, `archive.py`, `csw.py`, `profiles.py`. `pysent.qa` (the
benchmark harness) was only skimmed because bulk runs don't use it.

How findings were checked: **✅ verified** means reproduced in `ubuntu:24.04` with apt GDAL 3.8.4 (the CI
stack), using `bulk_processing_audit/`. **📖 code reading** means found by reading the code, not
reproduced. Line numbers refer to `c660cde`.

### A.1 Bugs

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| B1 | P0 | **The numba S1 stretch aborts the whole process when called from threads.** `_stretch_sentinel_s1_numba` is `parallel=True`. Numba's default `workqueue` threading layer is not thread-safe, and the default `parallel_mode` is `threads`. With `use_numba=True` (or `S1_USE_NUMBA=1`) and ≥2 polarisations, both threads enter the kernel together. ([s1.py:50](../src/pysent/s1.py#L50), [:310](../src/pysent/s1.py#L310), [:590](../src/pysent/s1.py#L590)) | ✅ `Numba workqueue threading layer is terminating: Concurrent access has been detected`, then core dump |
| B2 | P0 | **Outputs are not written atomically.** Final GeoTIFFs are written directly to their final name ([s1.py:459](../src/pysent/s1.py#L459), [s2.py:433](../src/pysent/s2.py#L433)). A worker killed mid-write (OOM killer, Slurm time limit, Ctrl-C) leaves a truncated `.tif` that any "skip if exists" resume logic treats as done. | 📖 |
| B3 | P1 | **Intermediate files leak on failure.** `.warp.tif`/`.vrt` files sit next to the outputs and are named after them. S1 has no `try/finally` around the stretch step ([s1.py:493-517](../src/pysent/s1.py#L493-L517), [:542-566](../src/pysent/s1.py#L542-L566)). S2 only cleans up when the warp succeeds. No scratch-dir option exists, and two jobs that use the same output name in one directory overwrite each other's intermediates. | ✅ S2 on a corrupt JP2 raises, and `o.warp.tif` stays in `output_dir` |
| B4 | P1 | **Nested pools and CPU oversubscription.** (a) `parallel_mode="processes"` only falls back when `current_process().daemon` ([s1.py:591](../src/pysent/s1.py#L591)). `ProcessPoolExecutor` workers are not daemonic, so a bulk runner built on it gets a second process pool per scene. (b) Worker and thread counts come from `os.cpu_count()` ([s1.py:240](../src/pysent/s1.py#L240), [:255](../src/pysent/s1.py#L255); same in s2), which ignores cgroup quotas, CPU affinity and Slurm allocations. (c) JP2 decoding adds its own threads (see P3). | ✅ inside a `ProcessPoolExecutor` worker the mode stays `processes`; with `--cpus=4` it picks 8 GDAL threads per product |
| B5 | P1 | **S2 min/max stretch turns valid pixels into NoData.** Each band is scaled `[min,max]→[0,255]` and the output keeps `NoData=0` per band ([s2.py:428](../src/pysent/s2.py#L428), [:433-449](../src/pysent/s2.py#L433-L449)), so the darkest valid pixels become transparent, or get colour fringes where only one band hits 0. | ✅ synthetic scene: 383 valid pixels with ≥1 band = NoData, 1 fully transparent |
| B6 | P1 | **`import pysent.s1` fails** when numba is installed, the package directory is read-only and `$HOME` is not writable (hardened containers, service accounts). `@njit(cache=True)` needs a cache locator when the module is imported ([s1.py:50](../src/pysent/s1.py#L50)). | ✅ `RuntimeError: cannot cache function '_stretch_sentinel_s1_numba': no locator available` |
| B7 | P1 | **An S2 scene with no valid pixels fails with a cryptic error.** `ComputeRasterMinMax` fails ([s2.py:425](../src/pysent/s2.py#L425)) and the error surfaces as `TypeError in method 'TranslateInternal'`. A bulk runner can't tell "empty scene" from a real failure. | ✅ |
| B8 | P2 | **`_configure_gdal_runtime()` does nothing.** `SetConfigOption("GDAL_CACHEMAX")` after the block cache is initialised has no effect ([s1.py:272](../src/pysent/s1.py#L272), [s2.py:150](../src/pysent/s2.py#L150)). The default cache is 5 % of RAM *per process*, so N workers can use N × 5 % for cache alone. | ✅ 2005 MB before and after setting 64 |
| B9 | P2 | **Some options silently do nothing.** (a) S2 `stretch_percentiles` is ignored by the active min/max writer ([s2.py:400-458](../src/pysent/s2.py#L400-L458)). (b) `use_tps` can't be reached from `process_sentinel_s1_safe` ([s1.py:386](../src/pysent/s1.py#L386) vs [:544](../src/pysent/s1.py#L544)), although the README recommends it. (c) S1 reads percentiles from `histogram_stretch={"percentiles": …}`, while S2 takes a bool plus `stretch_percentiles` ([s1.py:581](../src/pysent/s1.py#L581) vs [s2.py:579-580](../src/pysent/s2.py#L579-L580)). Passing the S2-style options to S1 silently falls back to the defaults. | ✅ (a) a single outlier pixel gives red mean 8.7/255, and (2,98) vs (10,90) give identical output; ✅ (b) |
| B10 | P2 | **One failed product discards the others' results.** The first failing future raises, so the result dicts of products that did finish are lost while their files stay on disk ([s2.py:652-654](../src/pysent/s2.py#L652-L654), [s1.py:710-713](../src/pysent/s1.py#L710-L713)). | 📖 |
| B11 | P2 | **GDAL exceptions are never enabled.** Failures surface as a generic "warp failed", and the real cause (e.g. `opj_get_decoded_tile() failed`) goes only to stderr, which bulk logs lose. Every process also emits GDAL's `FutureWarning`. | ✅ seen in the corrupt-input test |
| B12 | P3 | **Risky fork start method.** The internal `ProcessPoolExecutor` uses the platform default (fork on Linux, Python ≤ 3.13) after GDAL/numba threads exist, which can deadlock ([s1.py:653](../src/pysent/s1.py#L653), [:710](../src/pysent/s1.py#L710)). | 📖 |
| B13 | P3 | **S1 always reprojects to UPS North** (`EPSG:32661`, [s1.py:42](../src/pysent/s1.py#L42)). That's right for the Nordic archive but wrong for scenes elsewhere. It needs documenting, or an automatic UTM option. | 📖 |
| B14 | P3 | **Docs don't match the code.** [tuning-and-roadmap.md](../docs/tuning-and-roadmap.md) says production S2 defaults to JPEG (the code uses DEFLATE) and describes the S2 stretch as percentile-based (the active stretch is min/max). | 📖 |

These looked like problems but checked out fine:

- **S1 JPEG with an alpha band:** alpha stays exactly {0, 255}.
- **Corrupt JP2 input:** raises an error (only the leftover file is a problem, see B3).
- **Zipped products:** members are *stored*, not deflated, so reading through `/vsizip/` is cheap and needs no unzipping.
- **`archive.py`, `profiles.py`, `csw.py`:** nothing that affects bulk runs.

### A.2 Performance (measured)

Setup: GDAL 3.8.4, 8 pinned cores (`--cpuset-cpus=0-7`) on a 16-core, 39 GB VM, products on local disk. Each figure is a single run, so treat it as indicative. Products are the ones in
`tests/data/manifest.json`: S2C L2A T35VPE (1.1 GB) and S1D IW GRDH 1SDV (1.8 GB).

| Run | Wall | CPU | Peak RSS | Output |
|---|---:|---:|---:|---|
| S2 true colour, default (10 m) | 58.0 s | 115 s | 1.98 GB | 10980², 175 MB |
| S2 all 3 default products (10 m) | 62.7 s | 289 s | 3.84 GB | 3 × 10980² |
| S2 true colour, `target_resolution=60` | **2.3 s** | 8.5 s | 0.25 GB | 1830², 6 MB |
| S1 VV+VH, default (TPS warp) | 15.5 s | 79 s | 3.17 GB | 7391×4979 |
| S1 VV+VH, polynomial GCP warp | 11.9 s | **38 s** | 3.31 GB | 7383×4982 |
| JP2 decode of one 10 m band, GDAL default threads | 10.2 s | 26.2 s | | |
| same, `GDAL_NUM_THREADS=1` | 27.9 s | 28.1 s | | |
| Stack → GTiff, `gdal.Warp` (same CRS), LZW | 14.8 s | 20.4 s | | |
| Stack → GTiff, `gdal.Translate`, LZW | 13.8 s | 13.7 s | | |
| Stack → GTiff, `gdal.Translate`, uncompressed | **1.9 s** | 1.9 s | | |

| ID | Finding | Lever |
|---|---|---|
| P1 | **Output resolution is the biggest lever.** S2 warps to the finest selected band (10 m, [s2.py:243](../src/pysent/s2.py#L243)). At 60 m GDAL reads the JP2 overview levels: **25× faster, 8× less memory**. | Add a `quicklook` preset (e.g. 60 m S2) for bulk runs. Default unchanged (see open questions). |
| P2 | **The intermediate warp file is LZW-compressed** ([s1.py:362](../src/pysent/s1.py#L362), [:411](../src/pysent/s1.py#L411), [s2.py:314](../src/pysent/s2.py#L314)) and then deleted. Encoding it costs about 12 s of a 14.8 s step; resampling itself is minor. | Write intermediates uncompressed (S2 10 m ≈ 690 MB of scratch space). For S2 without `target_epsg`, skip the warp and stretch the stack VRT directly. |
| P3 | **Threads multiply.** JP2 decoding uses all CPUs by default (2.6× CPU/wall), on top of warp `NUM_THREADS` and per-product threads. One call already uses 2–5 cores. N bulk workers that each assume all cores will thrash. | A per-call CPU budget that sets `GDAL_NUM_THREADS` (thread-local), warp `NUM_THREADS` and product workers together. The bulk runner gives each worker a fixed share. |
| P4 | **Shared bands are decoded again for every product.** Each S2 product re-opens the SAFE and decodes its bands from scratch. B3 is used by all 3 defaults; B4 and B8A by 2. Three products cost 289 CPU-s against 115 for one. | Decode the union of needed bands once per scene, then pick bands per product. |
| P5 | **The S1 TPS warp is expensive.** The polynomial transform halves CPU (79 → 38 s) and cuts wall time by 23 %, but the output grid moves by about 8 px. | Expose `use_tps` (B9b) and measure geolocation error before recommending it. |
| P6 | **Memory per call is high**: ~2 GB per S2 10 m product, ~3.2 GB per S1 dual-pol scene. Full float32 reads plus percentile copies ([s1.py:439](../src/pysent/s1.py#L439), [:298-321](../src/pysent/s1.py#L298-L321)). | Compute percentiles from a decimated read or an overview; stretch in blocks. Sizing rule for the examples: `workers ≤ min(cpus / threads_per_worker, (RAM − headroom) / peak_rss)`. |
| P7 | **The S2 min/max writer reads the raster twice.** An exact `ComputeRasterMinMax` pass, then `Translate`, then overviews. | Get min/max or percentiles from an overview or a histogram. |
| P8 | **Final outputs are compressed on one thread**, with overviews built in a separate pass. | Set the `NUM_THREADS` creation option; try the COG driver (tiling, overviews and compression in one pass). |

---

## Plan

Phases run in order, one PR each. Phase 2 depends on the Phase 1 APIs (`work_dir`, CPU budget,
error types). If Phase 1 slips, the examples can work around them, but that is not the plan.

### Phase 0: Session setup (every session)
- [ ] Read memory, enter the worktree, rebase on `origin/main` (see kick-off prompt).
- [ ] Build the audit image: `PLANNING/bulk_processing_audit/run.sh checks`. It should reproduce B1, B4, B5, B7, B8 and B9. B6 is masked there by `HOME=/tmp`: to see it, run `docker run --rm --user 12345:12345 -v $PWD:/src:ro pysent-audit:gdal38 python3 -c "import pysent.s1"`.

### Phase 1: Library robustness (PR 1)
Each item needs a regression test in `tests/`, using synthetic data like `tests/test_s2_processing.py`.
- [ ] **B2/B3:** add a `work_dir` processing option (default `output_dir`). Create intermediates in `tempfile.mkdtemp(dir=work_dir)` and remove them in `finally` on every path. Write finals to a temp name in `output_dir`, then `os.replace` into place.
- [ ] **B1:** make the numba call safe in threads: serialize it with a module lock, or use it only when not running in threads. Test: two threads, `use_numba=True`, run in a subprocess and assert exit code 0.
- [ ] **B6:** don't fail at import. Compile lazily, and fall back to `cache=False` when no cache locator is available.
- [ ] **B4/P3:** add `_available_cpus()` (`os.sched_getaffinity`, `os.process_cpu_count()` on 3.13+). Treat `parallel_mode="processes"` as serial in *any* child process. Apply `gdal_num_threads` as a thread-local `GDAL_NUM_THREADS` so JP2 decoding respects it too.
- [ ] **B8:** replace the no-op with `gdal.SetCacheMax()`, driven by a new `gdal_cachemax_mb` option or the env var.
- [ ] **B7/B10/B11:** add `pysent.errors` with `EmptySceneError` and a `PartialFailure(RuntimeError)` that carries `.results` (completed products) and `.errors`. Wait for all futures before raising. Run GDAL calls under `gdal.ExceptionMgr()` (GDAL ≥ 3.7, with a fallback for older versions) so the real cause ends up in the exception.
- [ ] **B12:** give the internal process pool a `forkserver`/`spawn` context.
- [ ] Full suite green in the CI container. PR opened.

### Phase 2: `examples/` bulk processing (PR 2)
Stdlib only, plus pysent. Must run on Python 3.10+; use `max_tasks_per_child` only when available (3.11+).
- [ ] `examples/README.md`: which script to use when; sizing guide built from the tables above; environment variables; failure modes and how to resume.
- [ ] `examples/bulk_convert.py`, the reference runner (CLI plus an importable `run_bulk()`):
  - **Inputs:** `--input-dir` (recursive `*.zip`/`*.SAFE`), `--input-list` (local paths or `/vsizip//vsicurl/` URLs). Platform detected from the basename (`pysent.profiles`).
  - **Presets:** `--preset quicklook|full`, plus S2 `--products` and S1 polarisations (auto-detected by default).
  - **Execution:** `ProcessPoolExecutor(mp_context=forkserver, max_tasks_per_child=…)`. A worker initializer sets `GDAL_NUM_THREADS`, `GDAL_CACHEMAX`, `NUMBA_NUM_THREADS`, `OMP_NUM_THREADS` and `NUMBA_CACHE_DIR`. The library is called with `parallel_mode="serial"`, an explicit `gdal_num_threads` and `work_dir=--scratch`.
  - **Worker count:** `--workers auto` applies the sizing rule from `--threads-per-worker` and `--mem-per-worker-gb`.
  - **Idempotent and resumable:** layout `<out>/<platform>/<product>/…`, plus a sidecar `<product>.json` written last (options, stats, versions, timings). Skip when the sidecar and all outputs exist; `--force` overrides.
  - **Failure isolation:** a per-scene status of `ok|skipped|empty|failed` goes to a JSONL results log with traceback. Rebuild the pool after `BrokenProcessPool` (OOM kill) and mark only the scenes that were in flight. Retry network errors with backoff (`--retries`).
  - **Operations:** progress with scenes/hour and ETA, `--dry-run`, clean Ctrl-C (cancel pending, finish or terminate running), exit code ≠ 0 if anything failed.
- [ ] `examples/bulk_from_catalogue.py`: UUIDs or a CSW query → `pysent.archive.resolve_safe_archive_from_uuid` → local archive path, falling back to the remote `/vsizip//vsicurl/` URL → `run_bulk()`.
- [ ] `examples/slurm/bulk_array.sbatch`: splits an input list by `SLURM_ARRAY_TASK_ID` and sizes workers from `SLURM_CPUS_PER_TASK` and `SLURM_MEM_PER_NODE`.
- [ ] `tests/test_examples_bulk.py`: runner logic with a fake processor (no GDAL). Cover discovery, skip/resume, an interrupted write leaving no sidecar, one failing scene not stopping others, a worker `os._exit` → pool rebuilt, exit codes. Add it to CI.
- [ ] Link the examples from `README.md`. PR opened.

### Phase 3: Output correctness and option plumbing (PR 3)
- [ ] **B5:** stretch valid pixels to `[1,255]` and keep 0 for NoData, or write an alpha/mask band. Decide with the user (see open questions).
- [ ] **B9:** expose `use_tps` and harmonise the stretch options. Accept both S1 and S2 spellings with a `DeprecationWarning`, and warn when `stretch_percentiles` has no effect.
- [ ] **B13/B14:** document the S1 target CRS and fix the tuning doc.

### Phase 4: Performance (PR 4)
Re-measure every item with `run.sh bench` and put before/after numbers in the PR.
- [ ] **P2:** uncompressed intermediates. Bypass the warp for S2 when no reprojection is needed.
- [ ] **P1:** a `quicklook` preset in the library (e.g. `processing_options={"preset": "quicklook"}`), used by the examples.
- [ ] **P4:** decode each needed S2 band once per scene.
- [ ] **P6/P7:** percentiles or min/max from a decimated read; lower peak RSS.
- [ ] **P8:** `NUM_THREADS` creation option; evaluate the COG driver.
- [ ] **P5:** measure S1 polynomial geolocation error against TPS on the benchmark scene and report it.

### Phase 5: Throughput sweep, docs and close-out (PR 5)
- [ ] Sweep workers × threads per worker on 8 and 16 cores for S2 (quicklook and full) and S1. Record scenes/hour and peak RSS in `examples/README.md`, and set the `--workers auto` defaults from the results.
- [ ] Update `README.md` "Known tuning work" and `docs/tuning-and-roadmap.md`.
- [ ] Update memory. Rename this file to `PLANNING_bulk_processing.md`.

## Open questions for the user
Recommendations in *italics*. Ask before the phase that needs the answer.
1. **S2 stretch default** (Phase 3): min/max (current) or percentile? *Percentile, since min/max breaks on any cloud or sunglint pixel. It's a visible output change, though.*
2. **S2 NoData** (B5): map valid pixels to `[1,255]` (same file layout) or add an alpha band (4-band RGBA)? *`[1,255]`: minimal change, and map servers keep working.*
3. **S2 default resolution** (P1): keep 10 m and offer `quicklook` as an opt-in preset? *Yes.*
4. **S1 polynomial transform** (P5): switch the default only if geolocation error is under ~1 output pixel. *Decide after measuring.*
5. **Examples location:** plain `examples/` scripts (not installed), or also a `pysent-bulk` console entry point? *Scripts first; promote later if people use them.*

## Session log
- **2026-09-14, audit session.** Worktree and branch created from `main` @ `c660cde`. Ran the audit and wrote this plan. Evidence scripts are in `bulk_processing_audit/`. The benchmark products were downloaded into the session scratchpad; get them again from the URLs in `tests/data/manifest.json`. No library code changed.
