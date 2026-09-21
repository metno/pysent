# Bulk SAFE → GeoTIFF processing: audit and plan

**Goal:** let users convert many Sentinel-1/2 SAFE products to GeoTIFF reliably and fast, using
multiprocessing, with a ready-to-use `examples/` directory. First fix the library bugs and costs that
break or slow that use case.

| | |
|---|---|
| Branch / worktree | `feature/bulk-processing` → `../pysent.worktrees/bulk-processing` |
| Base | `main` @ `c660cde` (2026-09-14) |
| Status | **Complete.** Phases 1-5 and the audit shipped in PRs #4-#10. |
| Audit evidence | [`bulk_processing_audit/`](bulk_processing_audit/): `run.sh checks`, `run.sh bench <DATA_DIR>` |

## Kick-off prompt

*Kept for reference: the plan is finished, so a new session only needs this if the
work is reopened.* Copy it into a new agent session; it can run from any directory.

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
3. Read PLANNING/PLANNING_bulk_processing.md in full. Do the next unchecked phase, in order.
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
- [x] Read memory, enter the worktree, rebase on `origin/main` (see kick-off prompt).
- [x] Build the audit image: `PLANNING/bulk_processing_audit/run.sh checks`. It should reproduce B1, B4, B5, B7, B8 and B9. B6 is masked there by `HOME=/tmp`: to see it, run `docker run --rm --user 12345:12345 -v $PWD:/src:ro pysent-audit:gdal38 python3 -c "import pysent.s1"`.

### Phase 1: Library robustness (PR 1)
Each item needs a regression test in `tests/`, using synthetic data like `tests/test_s2_processing.py`.
- [x] **B2/B3:** add a `work_dir` processing option (default `output_dir`). Create intermediates in `tempfile.mkdtemp(dir=work_dir)` and remove them in `finally` on every path. Write finals to a temp name in `output_dir`, then `os.replace` into place.
- [x] **B1:** make the numba call safe in threads: serialize it with a module lock, or use it only when not running in threads. Test: two threads, `use_numba=True`, run in a subprocess and assert exit code 0.
- [x] **B6:** don't fail at import. Compile lazily, and fall back to `cache=False` when no cache locator is available.
- [x] **B4/P3:** add `_available_cpus()` (`os.sched_getaffinity`, `os.process_cpu_count()` on 3.13+). Treat `parallel_mode="processes"` as serial in *any* child process. Apply `gdal_num_threads` as a thread-local `GDAL_NUM_THREADS` so JP2 decoding respects it too.
- [x] **B8:** replace the no-op with `gdal.SetCacheMax()`, driven by a new `gdal_cachemax_mb` option or the env var.
- [x] **B7/B10/B11:** add `pysent.errors` with `EmptySceneError` and a `PartialFailure(RuntimeError)` that carries `.results` (completed products) and `.errors`. Wait for all futures before raising. Run GDAL calls under `gdal.ExceptionMgr()` (GDAL ≥ 3.7, with a fallback for older versions) so the real cause ends up in the exception.
- [x] **B12:** give the internal process pool a `forkserver`/`spawn` context.
- [x] Full suite green in the CI container. PR opened.

### Phase 2: `examples/` bulk processing (PR 2)
Stdlib only, plus pysent. Must run on Python 3.10+; use `max_tasks_per_child` only when available (3.11+).
- [x] `examples/README.md`: which script to use when; sizing guide built from the tables above; environment variables; failure modes and how to resume.
- [x] `examples/bulk_convert.py`, the reference runner (CLI plus an importable `run_bulk()`):
  - **Inputs:** `--input-dir` (recursive `*.zip`/`*.SAFE`), `--input-list` (local paths or `/vsizip//vsicurl/` URLs). Platform detected from the basename (`pysent.profiles`).
  - **Presets:** `--preset quicklook|full`, plus S2 `--products` and S1 polarisations (auto-detected by default).
  - **Execution:** `ProcessPoolExecutor(mp_context=forkserver, max_tasks_per_child=…)`. A worker initializer sets `GDAL_NUM_THREADS`, `GDAL_CACHEMAX`, `NUMBA_NUM_THREADS`, `OMP_NUM_THREADS` and `NUMBA_CACHE_DIR`. The library is called with `parallel_mode="serial"`, an explicit `gdal_num_threads` and `work_dir=--scratch`.
  - **Worker count:** `--workers auto` applies the sizing rule from `--threads-per-worker` and `--mem-per-worker-gb`.
  - **Idempotent and resumable:** layout `<out>/<platform>/<product>/…`, plus a sidecar `<product>.json` written last (options, stats, versions, timings). Skip when the sidecar and all outputs exist; `--force` overrides.
  - **Failure isolation:** a per-scene status of `ok|skipped|empty|failed` goes to a JSONL results log with traceback. Rebuild the pool after `BrokenProcessPool` (OOM kill) and mark only the scenes that were in flight. Retry network errors with backoff (`--retries`).
  - **Operations:** progress with scenes/hour and ETA, `--dry-run`, clean Ctrl-C (cancel pending, finish or terminate running), exit code ≠ 0 if anything failed.
- [x] `examples/bulk_from_catalogue.py`: UUIDs or a CSW query → `pysent.archive.resolve_safe_archive_from_uuid` → local archive path, falling back to the remote `/vsizip//vsicurl/` URL → `run_bulk()`.
- [x] `examples/slurm/bulk_array.sbatch`: splits an input list by `SLURM_ARRAY_TASK_ID` and sizes workers from `SLURM_CPUS_PER_TASK` and `SLURM_MEM_PER_NODE`.
- [x] `tests/test_examples_bulk.py`: runner logic with a fake processor (no GDAL). Cover discovery, skip/resume, an interrupted write leaving no sidecar, one failing scene not stopping others, a worker `os._exit` → pool rebuilt, exit codes. Add it to CI.
- [x] Link the examples from `README.md`. PR opened.

### Phase 3: Output correctness and option plumbing (PR 3)
- [x] **B5:** stretch valid pixels to `[1,255]` and keep 0 for NoData, or write an alpha/mask band. Decide with the user (see open questions).
- [x] **B9:** expose `use_tps` and harmonise the stretch options. Accept both S1 and S2 spellings with a `DeprecationWarning`, and warn when `stretch_percentiles` has no effect.
- [x] **B13/B14:** document the S1 target CRS and fix the tuning doc.

### Phase 4: Performance (PR 4)
Re-measure every item with `run.sh bench` and put before/after numbers in the PR.
- [x] **P2:** uncompressed intermediates (`intermediate_compression`, default none). *Bypassing the warp when no reprojection is needed is still open.*
- [x] **P1:** a `quicklook` preset in the library (`processing_options={"preset": "quicklook"}`), used by the examples.
- [x] **P4:** decode each needed S2 band once per scene (one warp per output grid; products are cut from that stack).
- [x] **P6/P7:** percentiles from a decimated read (phase 3). P6 measured: peak RSS tracks the thread budget (8 threads 0.99 GB, 4 threads 0.63, 2 threads 0.58) and `warpMemoryLimit` barely moves it, so the lever is `gdal_num_threads`, which callers already set.
- [x] **P8:** threaded compression, via the serial default rather than a creation option (which would re-arm the crash in threads mode). COG evaluated and not adopted.
- [x] **P5:** measured - polynomial is 1.54 output px RMS off the product's own GCPs, so **the TPS default stays**.

### Phase 5: Throughput sweep, docs and close-out (PR 5)
- [x] Sweep workers × threads per worker on 8 and 16 cores for S2 (quicklook and full) and S1. Recorded in `examples/README.md`; `--workers auto` keeps 2 threads per worker and now budgets 1.5 GB per worker.
- [x] Update `README.md` "Known tuning work" and `docs/tuning-and-roadmap.md`.
- [x] Update memory. Rename this file to `PLANNING_bulk_processing.md`.

## Open questions for the user
Recommendations in *italics*. Ask before the phase that needs the answer.
1. **S2 stretch default** (Phase 3): **answered 2026-09-21 - percentile 0.5-99.5 with gamma 0.7**, chosen by the user from a visual comparison.
2. **S2 NoData** (B5): **answered 2026-09-21 - `[1,255]`, 0 is NoData only.**
3. **S2 default resolution** (P1): **answered - 10 m stays the default; `preset="quicklook"` is the opt-in.**
4. **S1 polynomial transform** (P5): **answered 2026-09-21 - keep TPS.** Measured 1.54 output px RMS (61.7 m) against the product's own GCPs, above the ~1 px bar; `use_tps=False` stays an opt-in.
5. **Examples location:** **answered - plain `examples/` scripts** (phase 2). Promote to a console entry point later if people use them.

## Session log
- **2026-09-14, audit session.** Worktree and branch created from `main` @ `c660cde`. Ran the audit and wrote this plan. Evidence scripts are in `bulk_processing_audit/`. The benchmark products were downloaded into the session scratchpad; get them again from the URLs in `tests/data/manifest.json`. No library code changed.
- **2026-09-14, phase 0 + phase 1 session.** Branch was already at `origin/main` (`85661e3`), no rebase needed. `run.sh checks` reproduced B1, B4, B5, B6 (with `--user`), B7, B8 and B9.
  - **What landed.** New private `pysent/_runtime.py` (CPU budget, GDAL settings, scratch dir, atomic output, product fan-out shared by S1 and S2) and public `pysent/errors.py`. Regression tests are in `tests/test_bulk_robustness.py`: 25 tests, 24 of which fail on `main` for the intended reason (the other checks the S1 success path). CI now installs `numba==0.60.0 llvmlite==0.43.0 --no-deps` so the B1/B6 tests run instead of skipping. Suite: 68 passed in the CI container with numba, 66 passed + 2 skipped without. All four docs notebooks execute against the branch (`pysent-docs:ci` with `src` mounted over `/opt/pysent/src`).
  - **Deviation, B4/P3: `GDAL_NUM_THREADS` is process-wide for the call, not thread-local.** Measured in GDAL 3.8: a thread-local `GDAL_NUM_THREADS=1` does not reach the I/O thread of a `multithread=True` warp (JP2 decode CPU/wall stayed 4.4), while the global option does (1.07). `gdal_runtime()` sets it and restores the old value when the call returns (from the calling thread in serial/threads mode, in each worker in processes mode). A `GDAL_NUM_THREADS` the user already configured is kept unless `gdal_num_threads` is passed. Concurrent calls in one process with *different* settings interfere; this is documented.
  - **Deviation, default threads per product = all usable CPUs, not CPUs ÷ product workers.** With the plan's split, fixing the CPU count made the defaults slower wherever `os.cpu_count()` had overcounted (containers): on 8 pinned cores S1 went 15.4 → 20 s, because `main` had silently used 16 ÷ 2 = 8 warp threads per product. Products keep their threads only partly busy, so sharing beats splitting. Wall time, same outputs (S1 VV+VH / S2 three products):

    | Setting | `main` | split (cpus ÷ workers) | shared (landed) |
    |---|---:|---:|---:|
    | 8 pinned cores (`--cpuset-cpus`) | 15.4 / 64.2 s | 19.8 / 73.0 s | **12.6 / 46.6 s** |
    | `--cpus=8` quota | 15.6 / 67.4 s | 20.0 / 73.2 s | **12.5 / 51.8 s** |
    | 16 cores, unpinned | 13.7 / 55.0 s | 11.6 / 45.8 s | **8.7 / 32.9 s** |

    Peak RSS: S1 about equal (2.4–3.3 GB vs 3.0–3.6 GB); **S2 three products up from ~4.2 to ~5.0 GB**. Outputs were checked pixel-for-pixel against `main` (pixels, profile, overviews, tags; S2 at 16 threads and on 8 cores, S1 on 8 cores): identical. Bulk runners must pass an explicit `gdal_num_threads` (Phase 2 does). Phase 5's sweep should revisit this.
  - **B4 extras.** `available_cpus()` also reads the cgroup v2 `cpu.max` (own cgroup and ancestors) and the v1 CFS quota, because `docker --cpus` and Kubernetes limits don't change affinity. `processes` → `serial` whenever `multiprocessing.parent_process()` is set.
  - **B8.** `gdal_cachemax_mb` option, else `GDAL_CACHEMAX` parsed with GDAL's rules (`%`, MB below 100000, else bytes), applied via `SetCacheMax` and restored after the call.
  - **B1/B6.** Numba calls are serialised with a module lock (the kernel is parallel itself). The kernel is compiled on first use with `cache=True`, falling back to `cache=False` when numba raises. The B6 test simulates "no locator" by emptying `numba.core.caching.CacheImpl._locator_classes`, which also works as root. The real scenario (`--user 12345`, read-only package) was checked by hand. Observation, not changed: numba and numpy stretches differ by ≤1 grey level on ~6 ppm of pixels (float32 vs float64 rounding); the test allows it.
  - **B7/B10/B11 decisions.** Every product runs, in serial mode too. A multi-product call raises `PartialFailure(.results, .errors)`; a single-product call re-raises the product's own exception, so `EmptySceneError` can be caught directly. `PartialFailure` pickles (runner workers will send it across processes). GDAL exceptions are enabled with `gdal.ExceptionMgr()` only around warp, translate, BuildVRT, overviews and min/max, not globally: probing `gdal.Open` calls keep their `None` semantics and the caller's GDAL exception state is untouched. Consequence: GDAL 3.8's `FutureWarning` is still printed once per process (it goes away only by calling `UseExceptions()`/`DontUseExceptions()` globally). `EmptySceneError` covers only S2 with `histogram_stretch`. **S1 with no valid pixels still writes a fully transparent file** (stats all 0); Phase 2/3 should decide whether to raise there too.
  - **B12.** `forkserver` where available, else `spawn`. Scripts using `parallel_mode="processes"` now need an `if __name__ == "__main__":` guard (documented). S2's `processes` still means threads, as before.
  - **Kept for compatibility.** `_warp_sentinel_s1_safe_amplitude`/`_warp_sentinel_s2_rgb` signatures are unchanged (the notebooks call them). `run.sh checks`' `cachemax` check now handles both old and new code.
  - Benchmark products were reused from the audit session's scratchpad (`/tmp/claude-1000/-home-ubuntu-dev-services-pysent/249b80ef-…/scratchpad/data`); `/tmp` may be wiped.
- **2026-09-18, memory report + batch tuning (PR #5 follow-up, commit `32c411e`).**
  - **User report: "8 GB RAM for one S2 scene" on the old code (all 3 default products).** Plausible on a large node. Reproduced on `S2C_MSIL2A_20260901T100021_…_T34VEP`: 4.8 GB on 16 cores / 39 GB RAM. Peak memory scales with the thread count (the old code sizes it from `os.cpu_count()`) and with GDAL's default cache (5 % of RAM). Emulating the thread counts of a 64-core host gave 6.3 GB, plus a 256 GB-host cache 7.3 GB; `GDAL_CACHEMAX=256` gave 2.6 GB.
  - **Crash found and fixed:** with `GDAL_NUM_THREADS` set and several products written from threads of one process, GDAL 3.8.4 segfaults in `GDALRasterBlock::Internalize()`; multi-threaded GeoTIFF DEFLATE workers were active (backtrace from the core). In a 2-worker batch, the branch crashed after 2 scenes and `main` with `GDAL_NUM_THREADS=8` in the environment after 4. `main` without it and the branch in serial mode each completed 16 of 16. **Fix:** threads mode no longer sets `GDAL_NUM_THREADS` and warns if the user set it; serial and process modes keep it. After the fix the same batch completed 16 of 16. A synthetic stress test did not reproduce the crash; it needs the full pipeline. The regression test covers the mechanism (no `GDAL_NUM_THREADS` while products run in threads).
  - **This removes the phase 1 default speed-ups:** they came mostly from multi-threaded compression in threads mode. Defaults are now the same as `main` (8 pinned cores: S1 16.1 vs 15.9–18.7 s, S2 ×3 64.6 vs 63.8 s). The earlier "shared" table in this log is superseded.
  - **Batch throughput** (16 cores / 39 GB, 2 real S2 scenes repeated, zips in page cache, 3 default products per scene):

    | Code | Workers × mode × threads, cache | Scenes/h | Peak total RAM | Per worker |
    |---|---|---:|---:|---:|
    | PR #5 | 1 × parallel products × 16, default | 65 | 5.1 GB | 5.1 GB |
    | PR #5 | 4 × parallel × 4, 512 MB | 150 | 12.5 GB | 3.5 GB |
    | PR #5 | 4 × serial × 4, 256 MB | 92 | 5.4 GB | 1.5 GB |
    | PR #5 | 8 × serial × 2, 256 MB | 133 | 8.3 GB | 1.1 GB |
    | PR #5 | 16 × serial × 1, 128 MB | 148 | 6.9 GB | 0.5 GB |
    | union prototype | 1 × 16, 256 MB | 92 | 2.7 GB | 2.7 GB |
    | union prototype | 2 × 8, 256 MB | 153 | 3.5 GB | 1.9 GB |
    | union prototype | 4 × 4, 256 MB | 220 | 5.1 GB | 1.5 GB |
    | union prototype | 8 × 2, 256 MB | **280** | 7.6 GB | 1.1 GB |
    | union prototype | 16 × 1, 128 MB | 261 | 6.8 GB | 0.5 GB |

  - **Union-warp prototype (P2+P4 pulled forward, not yet in the library):** warp the 5-band union once per scene, uncompressed, then cut the 3 products from it. Outputs pixel-identical to the current pipeline (pixels, profile, overviews, colour interpretation). Single scene, serial, 16 threads: 39.7 s vs 103.8 s at the same ~1.8 GB. A bigger cache no longer helps (the 2 GB cache only paid off because the products re-decoded shared bands). Waiting for the user's go-ahead to implement it.
  - **For Phase 2:** default the runner to serial products per worker, `gdal_cachemax_mb=256`, 2 threads per worker and workers ≈ cores / 2 (memory ~1.1 GB per worker). Use `max_tasks_per_child`: one process running 8 scenes in a row peaked at 7.8 GB vs 6.4 GB for one scene.
- **2026-09-21, phase 1 merged; P2+P4 implemented out of order (user's request).** PR #5 was rebase-merged (`c972d28`, `2b74dfa`, `b87ae67`).
  - **S2 now warps once per scene.** The bands the requested products need are collected once, warped once per output grid into an uncompressed stack in the call's scratch directory, and each product is cut from it with a band-subset VRT. `_warp_sentinel_s2_rgb` keeps its old signature (the notebooks call it) and delegates to the new `_warp_sentinel_s2_bands`. New option `intermediate_compression` (default none).
  - **Grouping rule, found the hard way.** Sharing one stack changed the output when `target_resolution` was coarser than the scene: GDAL then warps from source overviews, and *which* overview it reads depends on the bands in the stack (at 60 m: every pixel differed, max 3210 DN; with `overviewLevel="NONE"` both agreed, but that costs 11.5 s instead of 1.5 s, which would undo P1). So products whose request is coarser than their native resolution keep a stack of their own. Verified: not the chunking (`warpMemoryLimit` made no difference), not the VRT grid (all 10 m, same origin), not compression or interleave (byte-identical).
  - **Outputs verified identical** to the pre-change code on both real scenes, all three products, for: defaults (10 m), `histogram_stretch=False`, and `target_resolution=60` - pixels, profile, overviews, colour interpretation, tags and overview reads.
  - **Speed** (8 pinned cores, serial products, cache 256 MB): a three-product scene 127.9 → 48.8 s (T34VEP) and 126.9 → 49.1 s (T35VPE), CPU 382 → 194 s, peak RSS ~1.5 GB either way. 60 m quicklook 6.2 → 5.1 s. Batch (16 cores, 16 scenes, no failures): 8 workers × 2 threads **282 scenes/h** at 7.6 GB (was 133/h), 16 × 1 thread 262/h at 6.6 GB (was 148/h), 4 × 4 threads 222/h at 5.4 GB.
  - **Also fixed:** `gdal_runtime` restored a `GDAL_NUM_THREADS` that came from the environment as an explicit config option, so it outlived the env var and could trigger the threads-mode warning in an unrelated later call.
  - Still open from phase 4: P1 (quicklook preset), P5 (S1 polynomial), P6/P7 (percentiles from a decimated read), P8 (COG). Phases 2, 3 and 5 are untouched; **phase 2 (the `examples/` runner) is next**.
- **2026-09-21, phase 2: the `examples/` bulk runner (PR #7).** PR #6 was rebase-merged (`2f1c66c`) first.
  - `examples/bulk_convert.py`: one worker process per scene, products serial inside the worker, `forkserver`/`spawn` pool with `max_tasks_per_child`, a worker initializer pinning `GDAL_NUM_THREADS`/`GDAL_CACHEMAX`/`NUMBA_NUM_THREADS`/`OMP_NUM_THREADS`/`OPENBLAS_NUM_THREADS`/`NUMBA_CACHE_DIR`, `--workers auto` from the CPU and memory budget (Slurm, cgroup, machine), `<out>/<platform>/<scene>/` layout with a sidecar written last, a JSONL results log, retries with backoff for network-like errors, pool rebuild after a dead worker, `--dry-run`, Ctrl-C handling and a non-zero exit code when anything failed.
  - `examples/bulk_from_catalogue.py` (UUIDs, `--uuid-file`, or an OWSLib `--query`; falls back to `/vsizip//vsicurl/` when the archive copy is absent) and `examples/slurm/bulk_array.sbatch` (splits the list by `SLURM_ARRAY_TASK_ID`, node-local scratch, `--workers auto` from the allocation).
  - `tests/test_examples_bulk.py`: 22 tests, no GDAL needed, fake processors in **real** worker processes. Covers discovery, naming/family, sizing, serial options, presets, skip/resume, an interrupted write leaving no sidecar, a deleted output, empty scenes, one failing scene, a dead worker, retries, dry-run and exit codes.
  - **Bug the tests caught:** the scene whose worker died was popped from the in-flight map before its `BrokenProcessPool` surfaced, so it vanished from the report instead of being marked failed.
  - **Decisions.** Products run serially per worker: it is the best throughput per GB and it keeps off the GDAL 3.8 concurrent-write crash path. An `empty` scene gets a sidecar so a resume skips it. `run_bulk()` takes the parsed `args` namespace, so the CLI and the importable API cannot drift. `--preset quicklook` = 60 m (S2) / 160 m (S1); presets change nothing but resolution.
  - **Verified on the three real products** (2 S2 + 1 S1, 8 pinned cores): full preset 99 s for 3 scenes (109 scenes/h) with 2 workers × 4 threads, exit 0, scratch left empty; re-running skipped all three; `--preset quicklook` with 3 workers × 2 threads took 24 s (456 scenes/h). Sidecars carry options, per-product stretch stats, timings and pysent/GDAL/Python versions.
  - Phases 3 and 5 remain, plus phase 4's P1, P5, P6/P7 and P8.
- **2026-09-21, phase 3: output correctness and option plumbing (PR #8).** PR #7 was rebase-merged (`4a0ede5`) first. **The user answered the two open questions** after reviewing a visual comparison of six renderings of two real scenes (artifact, private): percentile 0.5–99.5 with gamma 0.7, and valid pixels mapped to `[1,255]`.
  - **B5 + open questions 1 and 2.** The S2 stretch is now `stretch_method="percentile"` (0.5–99.5) with `stretch_gamma=0.7`, scaled into `[1,255]`; **0 means NoData and nothing else**. Measured at 60 m: a hazy scene goes from mean 17/255 to 44/255, a bright one from 72 to 101, clipping 0.6 % of pixels (cloud tops). Verified on the real stack that the output's zero mask is *identical* to the source's fill mask and that the minimum on valid data is 1. Per-band fill at a swath edge still renders 0 in that band alone - a colour fringe, not transparency, and it reflects the source honestly.
  - **Performance, twice over.** GDAL's exact `GetHistogram` cost about as much as the warp, so percentiles come from a **decimated read** (2048², nearest, NoData dropped; P7 partly done). And `gdal.Translate`'s `-exponent` runs a `pow()` per pixel: +28 s per scene. Replaced by a **per-band lookup table applied strip by strip** (`_write_stretched_with_lut`), which matches GDAL's own exponent within 1 DN and is *faster than the previous linear path*: a three-product scene is **39.4 s against 45.4 s on main**, peak RSS 1.37 vs 1.58 GB. A `Translate` fallback remains for source types a 16-bit LUT cannot cover. Benchmarked per 10980² 3-band raster: linear scale 4.2 s, scale+exponent 13.6 s, LUT-in-VRT 9.2 s, numpy LUT 3.4 s.
  - **B9.** `use_tps` reaches `process_sentinel_s1_safe` (jobs carry per-input extras). Both stretch spellings work on both platforms - `stretch_percentiles` is canonical, `histogram_stretch={"percentiles": ...}` raises a `DeprecationWarning`. A `RuntimeWarning` fires when `stretch_percentiles` is passed with `minmax`, and when any stretch option is passed while `histogram_stretch` is off. An unknown `stretch_method` raises `ValueError`.
  - **B13/B14.** The S1 target CRS (EPSG:32661 UPS North at 40 m, Nordic-specific) is documented in the docstring and README; `docs/tuning-and-roadmap.md` and the generated `03_sentinel2.ipynb` no longer describe a min/max stretch or a JPEG production default. Notebooks regenerated from `_build_notebooks.py` and all four still execute.
  - **Defaults changed on purpose:** regenerated S2 products will not match older ones byte for byte. `stretch_method="minmax"`, `stretch_gamma=1.0` restores the old *range selection*, though valid pixels still start at 1.
  - Suite: 108 tests. Remaining: phase 5, and phase 4's P1, P5, P6 and P8.
- **2026-09-21, phase 4: the rest of performance (PR #9).** PR #8 was rebase-merged (`36b4c02`) first.
  - **P1, the `quicklook` preset.** `processing_options={"preset": "quicklook"}` renders S2 at 60 m and S1 at 160 m; explicit options still win (`apply_preset` in `_runtime`). One three-product S2 scene: **5.0 s instead of 38.6 s**. The examples now pass the preset name through instead of keeping their own table, so the two cannot drift.
  - **P5, answered: keep TPS.** Against the product's own 210 GCPs, the polynomial transform is **61.7 m RMS (1.54 output px) and 194.7 m max (4.87 px)**; TPS interpolates them exactly. The two disagree by 1.34 px RMS over a dense grid. The plan's bar was "under ~1 px", so the default stays; `use_tps=False` remains a documented opt-in worth about 23 % of wall time.
  - **P8, and a default change.** Threaded GeoTIFF compression takes the final write from **15.6 s to 5.4 s** per product. Passing `NUM_THREADS` as a creation option would also switch it on in threads mode, which is exactly the GDAL 3.8 crash condition - so instead **`parallel_mode` now defaults to `serial`**, where the scoped `GDAL_NUM_THREADS` already gives each write every core. Measured per S2 scene: 38.6 s serial vs 42.8 s threads on 8 cores, 32.8 vs 38.0 on 16. S1 is a wash on time but **1.22 GB instead of 1.82 GB**. Outputs are unchanged, byte for byte. The COG driver was evaluated and **not adopted**: 8.0 s vs 5.4 s, same size, no structural gain for this output (tiled with overviews already).
  - **P6, no free win.** Peak RSS during the warp tracks the thread budget almost linearly (8 threads 0.99 GB / 21.9 s, 4 threads 0.63 GB / 47.0 s, 2 threads 0.58 GB / 52.7 s); `warpMemoryLimit` changes it by less than 0.1 GB. The lever is `gdal_num_threads`, which the runner already sizes, so nothing was changed and the relationship is documented instead.
  - **Batch, with everything in:** 8 workers x 2 threads, 16 scenes: **313 scenes/h at 7.8 GB** (282/h at the end of phase 3), no failures. Suite: 116 tests.
  - Only phase 5 is left: the sweep, the docs refresh and the close-out rename.
- **2026-09-21, phase 5: sweep, docs and close-out (PR #10).** PR #9 was rebase-merged (`ef614a0`) first.
  - **Sweep:** 18 configurations, all completing without failures - S2 full, S2 quicklook and S1, at 2/4/8/16 workers, on 16 cores and on 8 pinned cores. Table in `examples/README.md`.

    | scenes/hour | 8 cores | 16 cores | peak RAM (16c) |
    |---|---:|---:|---:|
    | S2 full, 2 thr/worker | **165** | 312 | 7.6 GB |
    | S2 full, 1 thr/worker | 152 | **349** | 9.5 GB |
    | S2 full, 4 thr/worker | 131 | 253 | 4.8 GB |
    | S2 quicklook, 2 thr/worker | **1106** | **2090** | 2.6 GB |
    | S1, 2 thr/worker | **307** | **598** | 8.5 GB |

  - **`--workers auto` keeps 2 threads per worker**, which measured fastest on 8 cores and within 11 % of the best on 16 while using 20 % less memory. `--mem-per-worker-gb` raised 1.2 → 1.5, the measured worst case (S1 ~1.4 GB; quicklook only ~0.4 GB).
  - **A benchmark-environment trap worth remembering:** the first sweep reported 16 workers "finishing" 16 scenes at 469 scenes/h with `mean_scene_s` 0.0. Every scene had failed with `PartialFailure` because the host disk was 97 % full (45 GB of accumulated benchmark output). A throughput number with no successful scenes looks like a *win* in a table - always print the error count next to it.
  - Docs: `README.md` gained a performance table and lost the stale "unexplored wins"; `docs/tuning-and-roadmap.md` records what shipped (quicklook preset, decimated percentiles, LUT stretch, threaded compression) and what was measured and rejected (COG, polynomial S1 transform).
  - Suite: 116 tests. **The plan is complete; this file is renamed to `PLANNING_bulk_processing.md`.**
