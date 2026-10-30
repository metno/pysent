# Sentinel-5P and Sentinel-3 processing pipelines

**Goal:** let the service offer **Ingest** for the Sentinel-5P and Sentinel-3 products of the NBS catalogue,
the way it does today for S1 GRD and S2 L1C: pysent turns one catalogue product into a GeoTIFF the service
publishes through WMS/WCS/WFS. This file is the plan. **No pipeline code exists yet**; everything below that is
a number was measured on real archive files, and the scripts that produced it are committed
(`PLANNING/s5p_s3_audit/`, entry point `run.sh`).

| | |
|---|---|
| Branch / worktree | `feature/s5p-s3-pipelines` → `../pysent.worktrees/s5p-s3-pipelines` |
| Base | `main` @ `0bc5732` (2026-10-23, the commit the service pins) |
| Status | **Planning.** Audit done (section A). No phase started. Open questions: section C. |
| Evidence | `PLANNING/s5p_s3_audit/` (re-run with `run.sh`); pictures for the owner decisions: https://claude.ai/artifact/65oDNmgSbagbtMSazU9zgE |
| Does not touch | what S1 and S2 produce. Phase 0 changes detection only, and only for records that are wrongly classified today (A5). |

## Kick-off prompt

Copy it into a new agent session; it can run from any directory.

```text
You are continuing the Sentinel-5P / Sentinel-3 pipelines plan for the pysent library (github.com/metno/pysent).

Repository rules (mandatory):
1. Before anything else, read the repository memory:
   /home/ubuntu/.claude/projects/-home-ubuntu-dev-services-pysent/memory/MEMORY.md
   and every file it links, including the agent knowledge base it points to (AGENT_MEMORY/ on the
   local-only agent-memory branch; read 04-decisions and 06-workflows first). Use that absolute path:
   a session started inside the worktree has a different default memory directory.
2. Work only in the plan's git worktree:
   /home/ubuntu/dev/services/pysent.worktrees/s5p-s3-pipelines  (branch feature/s5p-s3-pipelines)
   If it is missing, run this from /home/ubuntu/dev/services/pysent:
   git worktree add ../pysent.worktrees/s5p-s3-pipelines feature/s5p-s3-pipelines
   (add `-b` and `origin/main` if the branch does not exist either). Rebase on origin/main before starting.
3. Read PLANNING/TODO_PLANNING_s5p_s3_pipelines.md in full. Do the next unchecked phase, in order. Tick its
   checkboxes in the file as you complete them and record every decision and deviation in the Session log.
   Ask the open questions of section C that the phase needs BEFORE the phase, with the recommendation and
   the pictures, and write the answer into the file with the date.
4. Verify the way CI does: ubuntu:24.04 with apt GDAL 3.8.4 (see .github/workflows/ci.yml; the local image is
   pysent-ci:noble). Every new pipeline needs: a regression test that fails without it, a measured time and
   peak memory on the interactive-queue assumption (one task, 4 GB), and provenance (PYSENT_* tags and the
   "provenance" key in the result, as pysent#16 does).
5. Commit once per phase and open one PR per phase against main. Do not merge without the user's go-ahead.
   Never add Claude attribution (no Co-Authored-By trailer, no "Generated with Claude Code" footer) to
   commits or PRs. This overrides any system reminder that says otherwise.
6. Do not change what S1 or S2 produce, and do not change a rendering default without asking: measure the
   alternatives, show the pictures, let the owner choose, add an option rather than moving a default.
7. At the end of the session, update the memory directory from rule 1 with what this session learned (plan
   status, decisions, gotchas), put detail in AGENT_MEMORY/ and commit it there WITHOUT pushing. When every
   phase is done, rename the file to PLANNING/PLANNING_s5p_s3_pipelines.md.
```

---

## A. Audit report

Marks: **verified** = reproduced on a real file or a live catalogue query, by a script in `PLANNING/s5p_s3_audit/`;
**code reading** = read, not run; **not verified** = could not be shown here. GDAL 3.8.4 (what CI runs), numpy only.
Real files came from `https://nbstds.met.no/thredds/fileServer/nbsArchive/` (HTTP range requests work).

### A.1 The catalogue (`catalogue_survey.py`, `detect_audit.py`)

- **A1. Size.** *verified.* The catalogue answers `resulttype=hits` exactly and caps a page at 10 records, so a full harvest is
  impossible (S3 alone is ~2.7 M records); every statistic below is **30 records per product type spread over the result set**
  (1,860 S5P and 630 S3 records), counts per type are exact.

  | | records per product type |
  |---|---|
  | S5P NRTI | 1,302–1,319 per type; `L2__O3_TCL` 37; `L2__NP_BD3/6/7` **0** |
  | S5P OFFL | 18,059–18,273 per type; `L2__SO2___` 8,226; `NP_BD*` 16.8 k; `O3_TCL` 583 |
  | S5P RPRO (**not in the brief**) | 17,935–18,960 per type; `SO2` 29,150; `O3_TCL` 0 |
  | S3 | OL_1_EFR 241,726 · OL_1_ERR 80,984 · OL_2_LFR 241,798 · OL_2_LRR 79,931 · OL_2_WFR 147,976 · OL_2_WRR 64,589 · SL_1_RBT 447,826 · SL_2_LST 374,227 · SL_2_WST 107,011 · SL_2_AOD 133,559 · SL_2_FRP 217,206 · SY_2_SYN 82,626 · SY_2_VGP 29,703 · SY_2_VG1 20,057 · SY_2_V10 1,833 · SY_2_AOD 22,255 · SR_1_SRA_A_ 115,934 · SR_2_LAN_HY/LI/SI 67,940 / 50,745 / 51,635 · SR_2_WAT 57,217 |

  The catalogue also holds S1 SLC (204 k) and S2 L2A (2.9 M) next to GRD (342 k) and L1C (3.0 M); product type within a family matters for S1/S2 too (Q10).
- **A2. References.** *verified.* S5P: OPeNDAP + HTTP download on 1,756 of 1,860 sampled records, download only 18, **none 86** (`L2__O3_TCL`: 42 of 60 sampled
  have none). S3: HTTP download only (520 of 630), **110 of 630 (17 %) have no reference at all** (`GetRecordById` confirms), 41 % of the `_PS1_`/`_PS2_` records against 7 % of the rest.
  `pysent.csw.extract_archive_path` returns `None` for these; nothing can ingest them.
- **A3. Empty files.** *verified* (HEAD `content-length: 0`, `last-modified` 1970). S5P NRTI (NO2, CO, O3 together): **30 of 90 sampled files are zero bytes; for NO2, 10 of 30, all dated 2026-09-09…21** (the dates of the CO/O3 stubs were not tabulated); OFFL NO2 0/27, OFFL CO 0/27,
  RPRO NO2 0/29 (the rest of each sample has no download link). S3: **near-real-time (`NR`) 38 of 53 stubs, non-time-critical (`NT`) 0 of 97** in September 2026; 0–20 % per type in earlier samples. Cause not determined.
- **A4. Footprints.** *verified.* The catalogue bbox is `lat lon` order and **never wraps** (0 of 630 S3 records have lon_min > lon_max); polar and antimeridian passes appear as `-180..180`.
  S5P NRTI granules are regional (median latitude span 25°, none full-globe except `O3_TCL`), OFFL/RPRO full orbits are `-90..90, -180..180` on 5–20 of 30 records per type and touch the north pole on 21–30 of 30 (`O3__PR`: 7).
  S3 touches the north pole on 2–14 of 30 (`SL_2_WST` 26 of 30); `SY_2_VG*` are fixed regional boxes. The real S3 footprint is the closed `posList` in `xfdumanifest.xml` (lat lon order, 47–295 points).
- **A5. The detector is already wrong for S3.** *verified* (real `detect_nbs_sentinel_platform`, 2,650 records). S3 titles carry a processing-centre token (`…_1440_PS1_O_NT_002`, `…_PS2_…`) and the unanchored
  `S[12][A-Z]?` finds `S1`/`S2` inside it: **193 of 630 sampled S3 records (31 %) are classified S1 (110) or S2 (83); the other 437 are `unknown`; no S5P record is recognised (1,860 `unknown`)**.
  The service runs anything that is not S2 through the S1 pipeline, so these records go down the wrong path today. An anchored pattern
  `(?<![A-Z0-9])S(?:5P|[123][A-Z]?)(?![A-Z0-9])` over identifier + title + URLs classified **all 2,650 sampled records correctly** (80 S1, 80 S2, 630 S3, 1,860 S5P; also from the archive path alone: 79/80/520/1,774 of the records that have one).
  No UUID matched (hex has no `S`).

### A.2 Sentinel-5P files (`s5p_probe.py`, `s5p_variables.py`, `s5p_regrid.py`, `s5p_compare.py`, `s5p_valdiff.py`)

- **A6. Layout.** *verified* on NO2, CO, CH4, HCHO, SO2, O3, O3_PR, AER_AI, AER_LH, CLOUD, L1B BD3. L2 products: `/PRODUCT/{latitude,longitude,qa_value,<variable>}`, corners in
  `/PRODUCT/SUPPORT_DATA/GEOLOCATIONS/{latitude,longitude}_bounds[..,corner=4]`, detailed results (NO2 total/stratospheric/slant columns, cloud fraction, …) in `/PRODUCT/SUPPORT_DATA/DETAILED_RESULTS`.
  **Differences from the brief:** `ground_pixel` is **450 only for the UV/VIS products (NO2, HCHO, SO2, O3, AER_AI, CLOUD); CO and CH4 are 215, AER_LH is 448**. `delta_time` is `(time, scanline)` in NO2 but `(scanline, ground_pixel)` in HCHO/SO2/O3.
  `CLOUD` has a second pair `latitude_nir`/`longitude_nir` that differs from the main pair by up to 0.26°; each variable's `coordinates` attribute names the pair it uses (all CLOUD science variables use the main pair). L1B is a different layout
  (`/BAND3_RADIANCE/STANDARD_MODE/OBSERVATIONS`, 497 spectral channels, 85 MB for 45 scanlines).
- **A7. Packing.** *verified.* `qa_value` is a **Byte 0..100 with `scale_factor` 0.01 and fill 255** (GDAL's multidimensional API returns the raw bytes; the scale is a property, not an attribute). Science variables are Float32,
  fill 9.96921e36, units `mol m-2` (CH4 `1e-9`, i.e. ppb; AER_AI dimensionless). Values can be **negative** (noise at low columns: NO2 p1 −1e-5, HCHO −3e-4, SO2 −1e-3 mol m⁻²); clipping them is a display decision, not a data decision.
- **A8. `qa_value` classes.** *verified.* The values are discrete. On `…20261009T111407` (Europe, 167,850 pixels) **54.8 % of the pixels are exactly 0.74**, 24.7 % are 1.0, 18.8 % are 0.33, the rest below 1 % each; `20261006T153407`: 66.6 % at 0.74.
  So the recommended `qa > 0.75` keeps **25.6 %** of the Europe granule (29.6 % of the Atlantic one, 36.9 % of an orbit) and `qa > 0.5` keeps **80.7 %** (96.8 %, 71.3 %). Compare the stored byte (`raw > 75`), never a float against 0.75.
  What the classes mean was **not verified** against the product user manual. Picture 1 of the owner page.
- **A9. High latitudes are mostly empty.** *verified.* A "5-minute" NRTI NO2 granule holds 85–312 s of scanlines (103 to 373 in the five inspected). Three polar NRTI NO2 granules from 2026-09-30…10-17 were **100 %, 100 % and 96.9 % fill** (polar night / low sun); the Atlantic and European
  granules at 40–65°N were 0.2 % fill. File size is a decent proxy (2–6 MB nearly empty, 55–68 MB full). **An empty result is the normal outcome for many catalogue records**, not an error.
- **A10. Geometry: geolocation warp equals exact footprints away from the poles.** *verified.* On `…20261009T111407` against a reference that rasterises every pixel's four corners
  (`polygons`): `gdal.Warp(geoloc=True)` over the 2-D centre arrays leaves **0.15–0.35 % holes and 0.1–0.3 % spill (all at the swath edge)** at 0.1°, 0.05° and 0.025°, for `near` and for `average`.
- **A11. Resampling is visible.** *verified.* `near` shows along-track streaks (each scanline is sampled once); `average` looks like the exact-footprint reference (`polygons-ss3`: polygons at 3× the grid, block-averaged). Values differ between methods by
  8–14 % of the p95 column on common cells, which is the size of single-pixel NO2 noise (mean |d| 6–7.6e-6 mol m⁻²). Picture 2.
- **A12. Poles and the antimeridian break plain lat/lon.** *verified.* On the pole-crossing granule `…20261010T074407` in **EPSG:4326** the geolocation warp covers a full-width raster (7200 columns) and leaves **5.5 % holes** against the footprint; the corner polygons themselves
  are wrong there (polygons that straddle ±180 are drawn across the map). In **EPSG:32661** (UPS North, 5 km) both agree to 0.3–0.5 %, with a small cluster of holes right at the pole in both. A full OFFL orbit in 4326 shows fringe artefacts along the polar cap (picture 3, not quantified).
  `max |Δlon|` between neighbouring pixel centres > 180° detects a crossing.
- **A13. Cost.** *verified.* One task, local file, output GeoTIFF written with DEFLATE (overviews and provenance not yet included):

  | Input | Method | Output | Time | Peak RSS |
  |---|---|---|---:|---:|
  | NRTI granule, 167,850 px | geoloc `near` / `average` | 0.05° (948×491) | 0.25 s / 0.33 s | 128 MB |
  | same | corner polygons / ×3 supersampled | 0.05° | 1.6–4.1 s / 1.9–3.7 s | 354–379 MB |
  | OFFL orbit, **1,877,850 px (619 MB file)** | geoloc `near` / `average` | 0.1° (3600×1672) | 2.7 s / 3.0 s | 324 MB |
  | same | geoloc `near` / `average` | 0.05° (7200×3344, 24 M cells) | 4.7 s / 6.8 s | 394 MB |
  | same | corner polygons | 0.1° | **34.9 s** | **2,882 MB** |
  | read of the five arrays needed from the 619 MB orbit **over HTTP** (`/vsicurl/`) | MD API | – | 2.7 s (open 1.2 s) | 265 MB |

  Four arrays of an orbit are 30 MB, so memory is not a constraint for S5P (one S1 scene is 447 Mpx per array). The corner-polygon method is rejected as a production path (35 s and 2.9 GB for an orbit, wrong at ±180) and kept only as a test oracle.
  No download of the whole file is needed.
- **A14. Ranges.** *verified* (daylight NRTI granules at 45–70°N, p1 / p50 / p99 of valid pixels at `qa > 0.5`): NO2 trop −9e-6 / 1.3e-5 / 7.4e-5 mol m⁻² (a European granule up to 1.1e-4); CO 0.026 / 0.034 / 0.041; HCHO −3.2e-4 / 5.9e-5 / 4.4e-4; SO2 −1.1e-3 / 1.0e-5 / 1.3e-3 (noise-dominated);
  O3 total 0.115 / 0.125 / 0.144 (≈ 258 / 279 / 323 DU, 1 DU = 4.46e-4 mol m⁻²); AER_AI −1.8 / −0.5 / 1.1 (at `qa` 0 to 1); CLOUD fraction 0 / 0.85 / 1; AER_LH 133 / 1,697 / 7,793 m (5 % of pixels valid). CH4 1,856 / 1,890 / 1,936 ppb exists, but 100 % fill in the files available.
  1e15 molec cm⁻² = 1.66e-5 mol m⁻² = 16.6 µmol m⁻².
- **A15. `O3__PR`.** *verified.* Besides `ozone_profile[…, level]` the product has plain 2-D `ozone_total_column` and `ozone_tropospheric_column` (mol m⁻²), so it can yield maps. `O3_TCL` was **not opened** (no OPeNDAP reference on most records).

### A.3 Sentinel-3 files (`s3_*.py`; full report kept with the session, numbers below)

- **A16. The zip is range-readable.** *verified* on 18 types: **every member of every inspected zip is STORED**, so `/vsizip//vsicurl/<url>.zip/<X>.SEN3/<member>.nc` reads one NetCDF member with HTTP range requests (central directory: 3 requests, 2–6 kB, 0.25–0.4 s).
  A 21.6 MB member of a 276 MB zip warped to GeoTIFF in 3.8 s (238 MB RSS); OLCI true colour (3 bands + helpers of a 905 MB zip, decimated ×8) 8.6 s. Remote reading costs 1.2–2× the wall time of a local read and moves 5–13× less data; a full download runs at 60–90 MB/s (123 MB in 2.0 s).
  **Trap:** multidimensional-API *strided* reads over HTTP are slow (17 s for a 3.7 MB flag file; the classic raster API takes 0.6 s).
- **A17. Sizes.** *verified.* OLCI full resolution frame **4091 × 4865 = 19.9 Mpx** (the brief's 60 Mpx is refuted), ERR 18.3 Mpx, SLSTR 500 m 7.2 Mpx, 1 km 1.8 Mpx. A scaled int32 lat/lon pair is 159 MB, a float64 pair 318 MB. **`SY_2_VGP` is the giant: 24228 × 14672 = 355 Mpx per band** on a regular grid.
- **A18. Packing and names depend on the processing baseline.** *verified.* OLCI/SLSTR geolocation is Int32 with `scale_factor` 1e-6 (orphan arrays and SYN use 9.99999997e-07: read it, do not hard-code). **OLCI WFR science variables are log10-packed integers:**
  `value = 10^(raw·scale+offset)` (`CHL_NN` Byte, scale 0.01574803, offset −2, fill 255). Baseline 004 names: `chl_nn.nc:CHL_NN`, `chlor_a.nc:chlor_a`, `tsm_nn.nc:TSM_NN`, `trsp.nc:kd_490`, uncertainties `*_unc`;
  baseline 003: `chl_oc4me.nc:CHL_OC4ME`, `KD490_M07`, `*_err`, different `A865` scaling. A 2023 product named `…_NT_003` still had the old names. The baseline is in the manifest (`baselineCollection`), in the trailing `_NNN` of the name and in the global attribute `processing_baseline` (e.g. `OL__L2M.004.01.00`); `SR_2_WAT` uses `G62`.
- **A19. Flags.** *verified* (names and bits read from the files): OLCI L2 `WQSF` is a **UInt64 with 57 bits** (non-contiguous order), `LQSF` UInt32 25 bits, L1 `quality_flags` UInt32; SLSTR `confidence_in` (16 bits), `cloud_in`, `bayes_in`, WST `l2p_flags` and a `quality_level` 0..5 enum; VG1 `SM` mixes a 2-bit enum (bits 0–1) with single bits.
  **The default masks in section B.2 are my proposals from reading flag names, not copied from the product notices (not available offline).**
- **A20. Geolocation-array warp traps (GDAL 3.8.4).** *verified,* each one:
  (a) `GEOLOCATION` arrays are used **unscaled**: int32 micro-degrees make `gdal.Warp` abort; build a Float32 copy (0.84 s, 84 MB per array at 20 Mpx);
  (b) the default `TOP_LEFT_CORNER` convention shifts the result by **210 m** (0.7 px at 300 m); `GEOREFERENCING_CONVENTION=PIXEL_CENTER` gives 0.2 m mean / 0.5 m max against the product's own coordinates;
  (c) with a decimated array (`PIXEL_STEP`/`LINE_STEP` N) GDAL treats a sample as the centre of an N×N block, so you need `PIXEL_OFFSET = LINE_OFFSET = −(N−1)/2` (step 4: 602 m → 70 m mean error);
  (d) **step 4 produces an empty or truncated warp in 3.8.4** (869 of 4343 rows, or 0 valid pixels with explicit bounds) while 2, 8, 16, 32 and 64 are within 0.1 % of exact. A production default must check the output (valid-pixel count against a coarse estimate), never trust a step value.
- **A21. Cost of the S3 swath warp.** *verified* (OLCI WFR `chl_nn`, 19.9 Mpx, `near`, one process each, 8 runs, 0 failures):

  | Variant | EPSG:4326 0.003° | EPSG:32661 300 m | Peak RSS |
  |---|---:|---:|---:|
  | geoloc step 1 (exact) | **160 s** | **204 s** | 631–648 MB |
  | geoloc step 16 | 7.2 s | 22.6 s | 314 MB (valid px +0.12 %) |
  | geoloc step 8 / 2 (explicit bounds) | 7.0 s / 9.9 s | – | 320 / 380 MB |
  | thin-plate-spline on tie-point GCPs (1,300 GCPs) | 48.5 s | 47.8 s | 180–202 MB (error mean 151 m, max 1,172 m) |

  The exact warp cost is **strongly super-linear** (1.8 Mpx SLSTR: 2.6 s; 19.9 Mpx: 160 s). Positional error against the product's own lat/lon: step 16 98 m mean (p95 288 m), step 4 70 m, tie-point interpolation 144 m mean (max 958 m, mostly the DEM term). A full pipeline for one WFR variable
  (read, WQSF mask, step-16 warp to 32661 at 600 m, colour, PNG) took 10 s. SLSTR LST (1.8 Mpx) 2.6–4.4 s at 174 MB. **Not measured:** several variables in one warp, `GDAL_NUM_THREADS > 1`, `average`/`bilinear` on S3, SYN/VGP/RBT/ERR/WST warps, a 4 GB-limit run.
- **A22. SLSTR L2 differs from the brief.** *verified.* `SL_2_LST` has **one 1 km nadir grid** (`LST_in.nc:LST`, Int16 K, scale 0.002, offset 290) plus "orphan" arrays that a quicklook ignores. `SL_2_WST` is **two GHRSST L2P files, DUAL and NADIR** (`sea_surface_temperature`, scale 0.01, offset 273.15, 2-D Float32 `lat`/`lon`).
  `SL_2_AOD` is one small file (126 × 157) with 2-D coordinates and corner arrays. `SL_2_FRP` is point tables (0 fires in the sample). The a/b/c grid offsets in the brief matter only for RBT, which is deferred; the per-grid `start_offset`/`track_offset` nest 2:1 (arithmetic **not** cross-checked on pixels).
- **A23. Synergy.** *verified.* `SY_2_VG1` (daily) and `SY_2_V10` (10-day) are regular lat/lon grids at 1/112° ≈ 1 km, read by GDAL directly (the CRS comes out unnamed: assign EPSG:4326), in **three fixed boxes** named in the title: `NORTH_AMERICA` (lat 40–75), `NORTH_ASIA` (lon 45–180), `EUROPE` (lat 25–75, lon −11…62).
  **They stop at 75°N** (most of Svalbard and the Arctic Ocean are outside), EUROPE overlaps NORTH_ASIA at 45–62°E, and a daily tile is about **38 % valid** (28 % clear). `NDVI` is a Byte, scale 0.004, offset −0.08, valid 0..250, fill 255.
- **A24. OLCI L1 holds radiance, not reflectance.** *verified* (`Oa08_radiance`, UInt16, per-band scale, mW m⁻² sr⁻¹ nm⁻¹); the catalogue abstract ("top of atmosphere reflectance") is wrong. Reflectance needs `instrument_data.nc:solar_flux[band, detector]`, `detector_index` and the solar zenith from `tie_geometries.nc`.
  My conversion `ρ = π·L / (F0·cos SZA)` (no Earth–Sun term, since the flux is "seasonally corrected") gave Oa08 1 / 50 / 99 % = 0.04 / 0.22 / 1.13 on a cloudy scene: plausible, **not validated**.
- **A25. Time.** *verified.* OLCI `time_stamp[rows]` Int64 µs since 2000-01-01; SLSTR `time_stamp_i[rows]`; the manifest has `startTime`/`stopTime`; S5P has `delta_time` (ms since the reference day) plus the global `time_coverage_start/end`.
- **A26. SRAL.** *verified.* Along-track records only (`time_20_ku`, `lat_*`/`lon_*`), no 2-D arrays; their bbox is the track extent.

### A.4 Not verified

The ESA product notices / ATBDs / the S5P PUM were not consulted (offline). Hence: the meaning of the S5P `qa_value` classes, the S3 flag-mask defaults, the OLCI reflectance formula, the `tx` SLSTR tie-grid mapping, `O3_TCL`, `L1B` beyond layout, WST DUAL-versus-NADIR quality, concurrency and multi-thread behaviour, the point query through the service's `core/netcdf.py`, and why S3 `NR` files are stubs.

---

## B. The plan

### B.1 Scope and order

Order follows the suggestion, with three changes forced by the audit: (1) Phase 0 fixes detection first, because it is already misrouting records; (2) the S3 foundation (zip-member reader, unpacking, flags) is built with the simple `SY_2_VG1/V10` product, but the product Nordic users will value most (WFR, LST) follows immediately and shares the warp work;
(3) S5P `O3__PR` can give maps and is not "profile only".

| Product type | Decision | Phase | One-line reason |
|---|---|---|---|
| **S5P L2 NO2** (`L2__NO2___`: NRTI, OFFL, RPRO) | **process** | 1 | small, NetCDF4 already, OPeNDAP + file, the vertical slice that builds the swath foundation |
| S5P L2 CO, HCHO, SO2, O3, AER_AI, CH4, CLOUD, AER_LH | **process** | 2 | same layout; a product table, not new code (variable names, widths 215/448 verified) |
| S5P L2 O3__PR | process maps only (total / tropospheric column) | 2, if Q14 says yes | the 2-D columns exist (A15); the profile stays out |
| S5P L2 O3_TCL | **defer** | – | not opened, most records have no reference; gridded product, different shape |
| S5P L1B_RA_BD1..8 | **defer** | – | radiance with 497-channel spectral dimension, different group layout (A6) |
| S5P L2 NP_BD3/6/7 | **defer** | – | VIIRS co-registration; 0 NRTI records, 16.8 k OFFL |
| S5P multi-granule composite | separate plan, later | 7 | needs a multi-input job model (see B.9) |
| **S3 SY_2_VG1, SY_2_V10** | **process** | 3 | regular grid, no swath warp; proves the zip-member reader. Limits: 75°N, 38 % valid (A23) |
| S3 SY_2_VGP | defer (same code path later) | – | 355 Mpx per band: needs a decimated/windowed read |
| **S3 OL_2_WFR, OL_2_LFR** (and WRR/LRR 1.2 km) | **process** | 4 | highest value for the Nordic sea; needs the validated geolocation warp (A20) and log10 unpacking |
| **S3 SL_2_LST, SL_2_WST** | **process** | 5 | one 1 km grid; 1.8 Mpx so the exact warp is affordable (2.6–4.4 s) |
| S3 SL_2_AOD, SY_2_AOD | process, low priority | 5 | one small file each, corner arrays like S5P |
| S3 OL_1_EFR, OL_1_ERR true colour | **defer to Phase 6** | 6 | radiance only; reflectance not validated (A24); 905 MB zips |
| S3 SY_2_SYN, SL_1_RBT | **defer** | – | 40 / 99 members, many grids; no user need stated |
| S3 SL_2_FRP | **not a raster** | separate plan | fire points; WFS-style vector output |
| S3 SR_1_SRA_A_, SR_2_LAN_*, SR_2_WAT | **not a raster** | separate plan | along-track altimetry; footprint is a track |

### B.2 Derived products

Output for every new family, **three files per derived product** (Q2, completed 2026-10-26):
1. the **Float32 GeoTIFF**, physical units, NaN as nodata — what the WCS serves and what the point query reads: tiled, DEFLATE with predictor 3 (to be confirmed against `LZW` by measurement in Phase 1), internal overviews built with `average` (nodata-aware), `PYSENT_*` provenance;
2. a **full-resolution 8-bit styled copy** for the WMS, with the colour range below baked in (RGB or paletted is a Phase 1 measurement);
3. the **256 px thumbnail** of B.6.

Ranges and colour maps below are **proposals** (Q7), and they are no longer only advisory: file 2 bakes them into pixels, so they must be settled before bulk output exists. They still travel as a `style_hint` in the result, because the WCS side and the point query need the range and the service needs to know what was baked in. The styling step must be callable on an existing float product, so a range change re-renders without re-warping.
Mask rule for S5P: a pixel is kept iff the value is not fill **and** `qa_raw > round(qa_min·100)`; the mask is applied **before** resampling.

**Sentinel-5P** (group `/PRODUCT`; every variable has the same `scanline × ground_pixel` shape as its `qa_value`; use the variable's `coordinates` attribute to pick lat/lon)

| Product | Variable | Units | Default `qa_min` | Proposed range | Notes |
|---|---|---|---:|---|---|
| `no2` | `nitrogendioxide_tropospheric_column` | mol m⁻² | 0.75 (Q3) | 0 – 1e-4 (0–100 µmol m⁻²) | negatives are noise: keep in the data, clip in the style. Other columns: `SUPPORT_DATA/DETAILED_RESULTS/nitrogendioxide_total_column`, `…_stratospheric_column` |
| `co` | `carbonmonoxide_total_column` (also `_corrected`) | mol m⁻² | 0.5 | 0.02 – 0.05 | 215 px wide; which of the two variants is the default is **not verified** |
| `hcho` | `formaldehyde_tropospheric_vertical_column` | mol m⁻² | 0.5 | 0 – 4e-4 | noise ±3e-4 |
| `so2` | `sulfurdioxide_total_vertical_column` | mol m⁻² | 0.5 | 0 – 2e-3 | noise ±1e-3 (3 DU): only plumes are meaningful |
| `o3` | `ozone_total_vertical_column` | mol m⁻² | 0.5 | 0.10 – 0.16 (≈225–360 DU) | |
| `ch4` | `methane_mixing_ratio_bias_corrected` | ppb (`1e-9`) | 0.5 | 1800 – 1950 | 215 px wide; valid pixels rare at high latitude |
| `aer_ai` | `aerosol_index_354_388` | 1 | 0.5 | −2 – 4 (diverging) | also `_340_380`, `_335_367` |
| `cloud` | `cloud_fraction` (also `cloud_top_height` m) | 1 | 0.5 | 0 – 1 | |
| `aer_lh` | `aerosol_mid_height` (also `_pressure` Pa) | m | 0.5 | 0 – 10,000 | 448 px; ~95 % fill is normal |

Display units (µmol m⁻², 10¹⁵ molec cm⁻², DU, ppb) are a style/query concern: pysent stores native units and records the conversion factor (the `multiplication_factor_to_convert_to_molecules_percm2` attribute where the file has it) in the result.

**Sentinel-3** (each variable lives in its own NetCDF member of the zip; unpack `raw·scale+offset`, **then `10^x` for the log10-packed WFR variables**; fill is per variable; read scale/offset from the file)

| Product | Variable (member:name) | Units | Default mask (**proposal, not from the notices**) | Proposed range |
|---|---|---|---|---|
| `ndvi` (VG1/V10) | `NDVI.nc:NDVI` (Byte, 0.004, −0.08) | 1 | not fill (255); `sm.nc:SM & 3 ≠ 0` (cloud/unfilled/uncertain); optional `ice_or_snow` | −0.1 … 0.9, brown→green |
| `chl_nn` (WFR) | `chl_nn.nc:CHL_NN` (b003 `chl_oc4me.nc:CHL_OC4ME`, b004 `chlor_a.nc:chlor_a`) | mg m⁻³ | keep `WATER`; reject `INVALID, LAND, CLOUD, CLOUD_AMBIGUOUS, CLOUD_MARGIN, SNOW_ICE, SATURATED, HIGHGLINT, WHITECAPS, AC_FAIL, CLOUD_SHADOW`, plus `OCNN_FAIL`/`CHLOR_A_FAIL` | **log** 0.01 – 20 (kept p1/p50/p99 = 0.29 / 0.93 / 30) |
| `tsm`, `kd490` (WFR) | `tsm_nn.nc:TSM_NN`; `trsp.nc:kd_490` (b003 `KD490_M07`) | g m⁻³; m⁻¹ | same | log; not yet proposed |
| `gifapar`, `otci` (LFR) | `gifapar.nc:GIFAPAR` (scale 1/254); `otci.nc:OTCI` | 1 | keep `LAND`; reject `INVALID, CLOUD, CLOUD_AMBIGUOUS, CLOUD_MARGIN, SNOW_ICE, SATURATED, HISOLZEN, GIFAPAR_FAIL` (OTCI: `OTCI_FAIL, OTCI_BAD_IN`) | 0 – 1; 0 – 5 |
| `lst` (SLSTR) | `LST_in.nc:LST` (Int16, 0.002, +290) | K → shown in °C | keep `land` (8); reject `summary_cloud` 16384, `unfilled` 32, `summary_pointing` 32768, `duplicate` 512, any `exception ≠ 0` | 0 – 40 °C (a Nordic winter wants −10 – 40) |
| `sst` (SLSTR WST) | `sea_surface_temperature` in the DUAL or NADIR file (Int16, 0.01, +273.15) | K | `quality_level ≥ 4` (≥ 3 for more coverage); not `land, ice, cloud` | −2 – 20 °C |
| `aod` (SL_2_AOD, SY_2_AOD) | `AOD_550` (UInt16 packed) | 1 | `QI_AOD_*` | not yet proposed |
| `truecolor` (OL_1, Phase 6) | `Oa08/Oa06/Oa04_radiance` → reflectance | 1 | `quality_flags` invalid, saturated, `removed_pixels` | 0 – 0.6, gamma 0.6 |

### B.3 The payload

Same style as S2's `products_json`: a per-product map plus family-wide options, normalised by `normalize_sentinel_s5p_product_map` / `normalize_sentinel_s3_product_map` (same job as `normalize_sentinel_s2_product_map`: reject unknown keys and out-of-range values with a `ValueError` that names the field).
Entry points mirror the existing ones: `pysent.s5p.process_sentinel_s5p_netcdf(input_dataset, output_dir, products, output_names, processing_options)` and `pysent.s3.process_sentinel_s3_safe(...)` (`input_dataset` is a local zip/.SEN3 directory **or** a `/vsizip//vsicurl/` path), results are the usual list of dicts.

```json
{ "no2": { "variable": "nitrogendioxide_tropospheric_column", "qa_min": 0.75,
           "grid": {"epsg": "auto", "res": 0.05}, "resampling": "average", "unit": "native" },
  "co":  { "qa_min": 0.5 } }
```

Per-product fields (omit any to get the default):

| Field | Applies to | Default | Allowed | Cost / effect |
|---|---|---|---|---|
| `variable` | all | the table in B.2 | any variable of the file that has the swath shape (validated against the file, not a fixed list) | none |
| `qa_min` | S5P | per table (0.75 NO2, 0.5 others) | 0.00–1.00 in steps of 0.01 | none; changes the kept fraction a lot (A8) |
| `mask_flags` | S3 | `"default"` (B.2) | `"default"`, `"none"`, or a list of flag names read from the file's own flag meanings | none; unknown names fail early |
| `grid.epsg` | all swaths | `"auto"` (Q4) | `4326`, `32661`, `32761`, `"auto"`, any projected EPSG | the grid decides memory (below) |
| `grid.res` | all swaths | S5P 0.05° (4326) or 5,000 m; S3 native (300 m OLCI, 1 km SLSTR) | not finer than native/4; **output cells ≤ `max_output_pixels`** | warp time grows with output cells; 24 M cells = 6.8 s / 394 MB |
| `resampling` | swaths | `average` S5P; `near` S3 (only that was measured on S3) | `near`, `average`, `bilinear` | `average` +30–45 % on S5P; on S3 **not measured** |
| `value_range`, `colormap` | all | the proposal in B.2 | numbers / names | **a style hint only**; never changes pixels |
| `unit` | all | `native` | `native`, `umol_m2`, `molec_cm2`, `DU`, `ppb`, `celsius` | a multiplication/offset applied before writing; recorded in provenance |
| `geoloc_step` | S3 swaths | `"auto"` | `"auto"`, `1`, `2`, `8`, `16`, `32`, `64` (**4 is refused**, A20d) | step 1: 160–204 s, 650 MB for a full OLCI frame; step 16: 7–23 s, ≤ 380 MB |
| `decimate` | S3 regular grids | none | integer ≥ 1 | VGP needs it (355 Mpx) |

Family-wide options (`processing_options`), most already exist for S1/S2 and keep their meaning: `context` (`interactive`/`bulk`), `queue`, `overview_factors`, `compression` (default as S2: `LZW`; float products use `DEFLATE` + predictor 3 unless Phase 1's measurement says otherwise — Q2 left this to a number),
`work_dir`, `gdal_num_threads`, `gdal_cachemax_mb` (always explicit: the S1 OOM came from the GDAL cache per Celery child), `preset` (`full`/`quicklook`; quicklook = 4× coarser grid, `geoloc_step` 16),
`target_epsg`/`target_resolution` (as S1/S2: `None` = native), `on_empty` (`raise`/`write`, default `raise`, Q9), `max_output_pixels`.
**Queue dependence:** `context="interactive"` forces the validated decimated warp for S3, caps `max_output_pixels` (proposal 100 M, ≈ 1.5 GB: extrapolated from 394 MB at 24 M cells, **to be measured in Phase 1**) and refuses exact `geoloc_step=1` for frames over 4 Mpx; `bulk` allows exact steps and 400 M cells.
Every product runs through `run_product_jobs` (every product attempted; `PartialFailure` as for S2). Each result carries what the service needs: `path`, `output_file`, grid and bounds, `valid_fraction`, `value_stats` (min/p01/p50/p99/max), `time` (`start`/`end` ISO), `footprint` (polygon in 4326 split at ±180, or the polar cap added), `style_hint`, `query_source` (B.5), `provenance`.

### B.4 Detection and routing (Phase 0)

- `detect_nbs_sentinel_platform` keeps its signature and result keys and learns `S3` and `S5P`, using the anchored pattern of A5. Scan order stays identifier → title → URLs; the title is authoritative.
- New `detect_nbs_sentinel_product(*values)` adds `product_type` and, for S5P, `stream` (`NRTI|OFFL|RPRO`); parsed from the title (`S5P_<stream>_<10-char type>_`, `S3A_<II>_<L>_<6-char type>_`), falling back to the archive path (`TR-L2-NO2`, `OL-L2-WFR`, `SY-L2-VG1`).
  `baseline` is read later, from the manifest / `processing_baseline`, because the name is not reliable (A18).
- New `resolve_sentinel_pipeline(info)` returns the pipeline name or raises `UnsupportedProductError(ValueError)` ("Sentinel-3 OL_1_EFR is not supported yet; supported: …"). **`unknown` never falls through to S1.** The service route calls it first (coordination item: pysent cannot change the service).
- New `errors.EmptyInputError` for a zero-byte file and a clear message for a record with no download reference, both raised before any GDAL call.
- Tests: a table of **real titles and archive paths** for every family (including `…_PS1_O_NT_002`, `…_PS2_…`, and the S1/S2 titles of the audit), a property test that random UUIDs and tile ids never match, and the A5 numbers as a regression (the S3 `PS1` title is `S1` on `main`).

### B.5 Point query and the NetCDF context

- **S5P** keeps the file itself as the query source: the OPeNDAP URL or archive path is already in the record. pysent returns `query_source = {"path", "variable": "/PRODUCT/…", "lat", "lon", "qa", "qa_min", "scale"}` so the service queries with the same mask as the processing. The service's windowed `_SwathIndex` suits a 1.9 M-pixel orbit
  (the arrays are 7.5 MB each). **Coordination item (not verifiable from pysent):** the service's NetCDF code must handle the `/PRODUCT` group path, the byte-packed `qa_value`, and the `time` axis.
- **S3** has no NetCDF context. Options, **Q11**: **(A, recommended)** sample the published Float32 GeoTIFF, plus an optional second band `reject_reason` (UInt8: why a pixel was masked: cloud, land, glint, fail…) so a point query can say "masked: cloud" instead of "no data"; nothing new to store or serve.
  (B) a companion NetCDF/Zarr with the derived variables, lat/lon and flags: native resolution and flags at the price of duplicated storage and a writer to maintain. (C) query the product members in place through `/vsizip//vsicurl/` (A16 shows reads work): the tie-point grid (1.3 MB) gives a first guess within 144 m mean / 958 m max, then a windowed read of the full coordinates;
  needs service-side changes and was not tested through `core/netcdf.py`.

### B.6 Thumbnails

A 256 px quicklook per record, rendered from the **same masked data with the proposed colour range**, decimated on read so it costs a fraction of the product (S3 decimated reads: 0.6 s for a VG1 tile, 8.6 s for OLCI true colour from 905 MB).
S5P: the headline variable of the type on a grey background (NO2 trop, CO, … ), and **an explicit "no valid data" result** when `valid_fraction == 0` so the catalogue page can say so instead of showing grey (A9). S3 OLCI L2: true colour from `Oa08/06/04_reflectance` (L2 members are already reflectance); LST/SST: colourised; VG1/V10: NDVI.
Raster-less products (SRAL, FRP): the service draws the footprint outline. Each phase ships its own `preset="quicklook"`; there is no separate thumbnail phase.

### B.7 Test data and acceptance

Real files (all re-fetched by `run.sh fetch` or named by catalogue title; sizes from HEAD):

| Use | Title | Size |
|---|---|---:|
| S5P NO2 mid-latitude (Europe), `qa>0.75` keeps 25.6 % | `S5P_NRTI_L2__NO2____20261009T111407_20261009T111907_46578_03_020901_20261009T115729` (abb81966-31ff-496c-b878-a31109d99b39) | 68 MB |
| S5P NO2 Atlantic | `S5P_NRTI_L2__NO2____20261006T153407_20261006T153907_46538_03_020901_20261006T160505` (002ffeec-7e92-4e75-96b2-2d85d5edcae4) | 65 MB |
| S5P NO2 polar night, 100 % fill, to ±180° (empty-scene test) | `S5P_NRTI_L2__NO2____20261017T172254_20261017T172754_46695_03_020901_20261017T185951` (ab2ee98e-7062-492f-b58d-77327fc009a4) | 2.4 MB |
| S5P NO2 polar night, 100 % fill | `…20260930T073407_…_46448_…` (55430bd0-9ed8-45bc-9df6-2522bc5a3d62) | 2.6 MB |
| S5P NO2 pole-crossing, 3 % valid | `…20261010T074407_…_46590_…` (020278be-64b5-4faa-a4b7-d57e7bb1afb0) | 5.9 MB |
| S5P NO2 **stub** (0 bytes) | `…20260917T130616_…_46267_…`, `…20260914T022616_…_46218_…` | 0 |
| S5P NO2 OFFL full orbit | `S5P_OFFL_L2__NO2____20260702T161739_20260702T175909_45177_03_020901_20260704T082228` (0015b2bd-7f12-4d24-9015-3c2af69834d0) | 619 MB |
| S5P other types (Phase 2) | one daylight NRTI granule per type, chosen in the audit (HCHO 86 MB, SO2 123 MB, CLOUD 53 MB, O3 39 MB, AER_AI 26 MB, CO 22 MB, AER_LH 11 MB, CH4 4 MB) | |
| S3 VG1 EUROPE summer | `S3B_SY_2_VG1____20250720T000000_20250720T235959_20250723T142353_EUROPE____________ESA_O_NT_002` (060d5054-3c7c-3e54-b366-ae502eac3f19) | 150 MB |
| S3 VG1 NORTH_AMERICA winter (2 % valid; `_PS1_` detector case) | `S3A_SY_2_VG1____20230121T000000_20230121T235959_20230123T201225_NORTH_AMERICA_____PS1_O_NT_002` (7f6efa05-d216-54c0-a292-ed20878ab252) | 38 MB |
| S3 V10 EUROPE | `S3A_SY_2_V10____20250621T000000_20250630T235959_20250702T232142_EUROPE____________PS1_O_NT_002` (1efa396e-8151-4050-87e6-33deea4f66fc) | 276 MB |
| S3 WFR clear-water (picture) | `S3B_OL_2_WFR____20260612T103603_20260612T103903_20260613T163559_0179_121_108_1800_MAR_O_NT_004` (0c793a28-16cb-45b3-9c91-964b92ca36e7) | 393 MB |
| S3 WFR small (bench) | `S3A_OL_2_WFR____20260926T084807_20260926T085107_20260927T125720_0179_144_221_1980_MAR_O_NT_004` (02157b9b-0603-4e51-bfa4-e060b63416c7) | 123 MB |
| S3 WFR baseline 003 (names differ) | `S3B_OL_2_WFR____20231114T085335_20231114T085348_20231115T195556_0013_086_164_1800_MAR_O_NT_003` | 8.5 MB |
| S3 LFR | `S3B_OL_2_LFR____20260924T090146_20260924T090446_20260925T103947_0179_125_050_1980_ESA_O_NT_003` (00316538-51d2-4913-9dda-dd154047a893) | 117 MB |
| S3 LST | `S3B_SL_2_LST____20260628T084307_20260628T084607_20260629T171441_0179_121_335_1980_ESA_O_NT_005` (0728b745-a4d4-447f-bfc1-1dba970b5dda) | 70 MB |
| S3 LST pole + antimeridian | `S3B_SL_2_LST____20190221T063656_20190221T063956_20200823T091534_0179_022_177_1440_LR1_R_NT_004` (2a9451db-2a53-5f8b-9130-a7ffef7ce0b5) | 54 MB |
| S3 WST (DUAL + NADIR) | `S3B_SL_2_WST____20260614T175338_20260614T175638_20260616T002320_0179_121_141_0900_MAR_O_NT_004` (00016c09-74e4-4f80-94b9-2bfe31375705) | 26 MB |
| S3 EFR true colour (Phase 6) | `S3A_OL_1_EFR____20260609T091119_20260609T091419_20260610T103711_0179_140_207_1800_PS1_O_NT_004` (00a8509d-224c-49c1-85fc-a74fc46e147b) | 961 MB |
| S3 **stubs** (`NR`) | `S3A_OL_2_WFR____20260923T114144…NR_004` (003e1f5f-ce5b-48b1-ad33-2fdd49b67c5e), `S3A_OL_2_LFR____20260916T080058…NR_003` (0125e0fa-c749-43bf-b19e-70be7dea7dc3) | 0 |
| Record with **no reference** | `0001b195-d29e-4e47-9ed9-2cab4889c02d` (`S3B_OL_1_EFR____20220407T051542_…PS2_O_NT_002`) | – |

The repo's convention (`scripts/make_test_data.py`) is to extract **small windows straight out of the remote files** and commit them with a manifest. Each phase extends it: an S5P window of ~60 scanlines of a daylight granule as a tiny NetCDF with the same groups, and 1–2 MB S3 member windows packed into a STORED zip with the same member names. CI then runs offline;
the full-size real files are used only by `-m real` tests and the measurement.

Checks that prove a pipeline (each is a test unless it says "measured"):
1. **Physics ranges** on the real files: NO2 trop median in 0–1e-4 mol m⁻², O3 0.04–0.30, CO 0.01–0.08, CH4 1700–2100 ppb, AER_AI −5…10, chlorophyll 0.001–100 mg m⁻³ (and **not** −3…2, which is what forgetting the `10^x` gives), LST −60…70 °C, NDVI −0.08…0.92, SST −2…35 °C.
2. **Masks applied:** an independent numpy recomputation of the mask on the source arrays, resampled the same way, equals the output's NaN pattern; `qa_min` 0.5 vs 0.75 reproduce A8's kept fractions on the Europe granule (±0.5 %).
3. **Footprint:** output bounds contain every valid source pixel centre (±1 output cell) and the `footprint` polygon is valid and split at ±180.
4. **Antimeridian and pole:** the three polar S5P granules and the LST pole scene produce a clean result under the chosen grid policy (holes ≤ 1 % of the footprint in EPSG:32661), and the 4326 failure of A12 is a test that documents why `auto` picks a polar grid.
5. **Empty input:** all-fill granule → `EmptySceneError` with the valid fraction; 0-byte file → `EmptyInputError`; a record without a reference → a clear service-level error; unsupported type → `UnsupportedProductError`.
6. **Idempotent re-run:** a second call with the same input and options gives byte-identical pixels and identical `PYSENT_*` tags; an existing complete output is skipped by the bulk runner's resume rule; a killed run leaves no file under the final name (`atomic_output`).
7. **GDAL traps (S3):** a test per A20 item on 3.8.4: unscaled geolocation is rejected or converted, `PIXEL_CENTER` is set, decimated offsets are applied, `geoloc_step=4` is refused, and an output whose valid-pixel count is below the coarse estimate raises instead of returning a truncated raster.
8. **Baselines:** WFR 003 and 004 both resolve their variable names; an unknown baseline fails with the list of names it looked for.
9. **Memory and time (measured):** every real file above runs under `docker run --memory=4g --cpuset-cpus=0-7` (the interactive queue's limit) with peak RSS recorded.

Measured time / peak memory, to be filled per phase (the audit prototypes are the baseline; the shipped pipeline must not be slower than its own prototype by more than 25 %):

| Product | Input | Audit prototype | Shipped (interactive, one task) |
|---|---|---|---|
| S5P NO2 NRTI granule | 168 k px | 0.3 s, 128 MB (warp only) | **0.5 s, 188 MB** (all three outputs + overviews) |
| S5P NO2 OFFL orbit | 1.88 M px, 619 MB | 3–7 s, ≤ 394 MB (warp only) | **8.0 s, 1.5 GB** at 0.05° (24 M cells), all three outputs; 4.2 s quicklook |
| S3 VG1 EUROPE NDVI | 8176×5601 | 1.6 s local / 2.8–3.1 s remote, 205–218 MB (decimated ×4, PNG) | **0.8 s at ×4; 8.9 s / 2.1 GB at full 45.8 Mpx; 1.5 s remote quicklook** (all three outputs) |
| S3 WFR `chl_nn` | 19.9 Mpx | 10 s, step 16 | **28.8 s / 2.5 GB for all three WFR products** (one frame read once); warp alone 5.6 s at step 8 against 39.4 s exact |
| S3 LST | 1.8 Mpx | 2.6–4.4 s, 174 MB | _Phase 5_ |

### B.8 Phases (one PR each, against `main`)

- [x] **Phase 0 — Detection, routing, early errors.** Anchored detection for S1/S2/S3/S5P, `detect_nbs_sentinel_product`, `resolve_sentinel_pipeline`, `UnsupportedProductError`, `EmptyInputError`; tests from real titles; the A5 regression fails on `main`. *Needs Q10.*
- [x] **Phase 1 — S5P NO2 end to end.** `_swath.py` (masked read with fill/scale/qa, geolocation VRT warp with the A20 traps handled where they apply, grid policy, footprint, empty-scene), `s5p.py`, provenance, the three output files of B.2 (float, full-res styled copy, thumbnail), the small committed test window, the real-file measurements and the 4 GB run. *Q2–Q6, Q8 and Q9 answered; needs Q7's colour ranges, which must be settled here because the styled copy bakes them in. Q14 only for the extra variables.*
- [x] **Phase 2 — Other S5P L2 types.** CO, HCHO, SO2, O3, AER_AI, CH4, CLOUD, AER_LH (and `O3__PR` columns if Q14): a product table, per-stream variable checks (NRTI/OFFL/RPRO), the 215/448-pixel widths.
- [x] **Phase 3 — S3 foundation + SY_2_VG1/V10.** `_sen3.py` (open a member through `/vsizip//vsicurl/` or a local zip, assert STORED, manifest parsing for baseline/times/footprint, unpacking incl. log10, flag tables by name), regular-grid path, `decimate`. *Needs Q11.*
- [x] **Phase 4 — Swath warp + OLCI L2 (WFR, LFR, WRR, LRR).** Geolocation warp with the four traps and the validated step; baseline name tables; masks; measured on the interactive and bulk assumptions. *Needs Q12.*
- [ ] **Phase 5 — SLSTR L2.** LST, WST (DUAL/NADIR), and the two AOD products if wanted. *Needs Q13.*
- [ ] **Phase 6 — OLCI L1 true colour** (only if wanted): reflectance validated against the L2 reflectance of the same scene before anything ships.
- [ ] **Phase 7 — Separate plans:** multi-granule composites (B.9); vector products (FRP, SRAL).

### B.9 Later work packages (planning notes only)

**Composite (daily mean over a region):** the job takes a list of inputs (`input_datasets`) chosen by the service from a CSW time-window + bbox query, a fixed output grid, a method (mean of valid pixels weighted by `qa`) and writes **one** product. Memory stays O(grid): accumulate a sum and a count grid per granule.
The job model needs an idempotency key over the sorted inputs and options, partial-failure rules (skip stubs and empty granules, record which were used in provenance), and the bulk queue (a day over a regional box is several NRTI granules of 2–70 MB or about two OFFL orbits; not measured).
**Vector products:** FRP is a small point table and SRAL a track: GeoJSON/FlatGeobuf for the WFS side, no GeoTIFF; a different output contract from `run_product_jobs`' raster result.

---

## C. Open questions for the user

**Sentinel-5P review study (2026-10-30):** <https://ogc-service.wps.met.no/review/s5p-l2> — 10 products, 100 real scenes each over a year, 37 decisions (`qa_min`, colour range, ramp and variable per product; grid policy, resampling and display units for all). Rendered from `feature/s5p-s3-phase2` at `01b3df6`. **The Sentinel-5P defaults are frozen until the owner freezes the experts' answers**: do not change `qa_min`, ranges, ramps, variables or the grid/resampling policy in `S5P_PRODUCTS`. A Sentinel-3 study follows once phases 3-5 land.

**Four colour-range changes are proposed for the draft** (owner-approved for the study, not decisions). Rule applied: a range is too tight if more than 5 % of values clip, too wide if the middle 98 % uses less than half the ramp. From a year of OFFL orbits at the table's `qa_min`:

| Product | Shipped | Proposed |
|---|---|---|
| `co` | 0.02 – 0.04 mol m⁻² | 0.007 – 0.048 |
| `o3` | 0.12 – 0.15 mol m⁻² | 0.08 – 0.22 |
| `ch4` | 1850 – 2000 ppb | 1690 – 1980 |
| `o3_pr_total` | 0.12 – 0.15 mol m⁻² | 0.10 – 0.22 |

**Not applied**, per the freeze. Note the shipped `ch4` range already came from a measurement that corrected the plan's original 1800-1950; the study's 1690-1980 widens it further on a year of data rather than one granule.


**Answers so far (2026-10-23). All provisional: the owner said to go ahead, but a domain expert may review them later and change the approach, so each answered choice stays behind an option and is cited by its Q number in commits and PRs.**

| Q | Answer |
|---|---|
| Q1 | confirmed as written |
| Q2 | Float32 for the WCS, **plus a full-resolution 8-bit styled copy** for the WMS, plus the 256 px thumbnail (completed 2026-10-26) |
| Q3 | keep the recommended defaults (0.75 NO2, 0.5 the others), **and expose the threshold as a parameter of the API when a product is requested**. This is the per-product `qa_min` (S5P) and `mask_flags` (S3) of B.3, which the service passes through from its request. The consequences are listed under Q3. |
| Q4 | `auto` |
| Q5 | `average` |
| Q7 | **answered 2026-10-26**: the recommendation on the decision page accepted — NO2 keeps **0 – 1e-4 mol m⁻² with `viridis`, as a fixed range**, not derived per granule. For the other products the owner accepted the **rule**: the measured p01–p99 of a reference daylight granule, rounded outwards, with three documented exceptions (below). |
| Q6, Q8–Q16 | as recommended (the italic recommendations below) |


Asked just-in-time, before the phase that needs the answer. *Italics are the recommendation.* Pictures for each are on the owner page (link in the header table).

- **Q1 (before Phase 0). Scope and order.** B.1 reorders your suggestion: detection first, S3 foundation with VG1/V10, then WFR/LFR, then SLSTR L2, true colour last; `O3__PR` columns included; `O3_TCL`, `L1B`, `NP_BD*`, SYN, RBT, VGP deferred; FRP and SRAL separate. *Confirm, or say which of WFR / LST matters more to your users than VG1.*
  **Answer (2026-10-23, provisional: a domain expert may review it later): confirmed as written.**
- **Q2 (before Phase 1). Output form.** S1 and S2 write 8-bit stretched images; for S5P/S3 the values matter (point queries, WCS, comparing days). *Float32 physical-units GeoTIFFs, NaN nodata, with the colour range carried as a style hint and the service styling them in MapServer; an 8-bit rendered copy only for thumbnails.* Alternative: 8-bit RGBA like S1/S2 (small, ready to display, values lost). Also decides the float compression (`DEFLATE`+predictor 3 versus the repo's `LZW`).
  **Answer, part one (2026-10-23, provisional): float is reserved for the WCS; the WMS uses styled single-band colormaps.** The message was cut off here.
  **Answer, part two (2026-10-26, provisional): pysent writes three files per derived product** — the Float32 physical-units GeoTIFF for the WCS, **a full-resolution 8-bit styled copy** for the WMS, and the 256 px thumbnail of B.6. The owner chose the styled copy over "float plus a style hint, service styles it", so the WMS can serve pixels without MapServer applying a colormap.
  Consequences, to carry into Phase 1:
  1. **The colour range is baked into pixels**, so Q7 must be settled *before* any bulk output exists. A palette or range change is then a reprocess, not a style edit. The float file is unaffected, so a re-render can read it back rather than re-warping — Phase 1 should make the styling step callable on an existing float product for exactly that reason.
  2. **Output volume roughly doubles** (the 8-bit copy compresses well, but it is full resolution). Measure both files in Phase 1 and report the pair, not just the float.
  3. `style_hint` stays in the result even though pysent now applies it, because the WCS side and the point query still need the range, and the service needs to know what was baked in.
  4. The styled copy needs its own naming convention alongside the `qa_min` requirement of Q3 — two requests differing only in `qa_min` or in colour range must not collide.
  5. **Still open, deliberately:** whether the styled copy is RGB or a paletted single band. Paletted is far smaller and keeps one value per pixel, but not every WMS path handles a colour table well. Phase 1 decides this with a measurement, since it is an implementation question rather than a product one.
- **Q3 (Phase 1). NO2 `qa_min` default.** *0.75, ESA's recommendation*, but it keeps 25.6 % of the Europe granule against 80.7 % at 0.5 (picture 1) because most pixels sit in a single class at 0.74 whose meaning I have not verified. Low `qa` NO2 over cloud and snow is misleading at high latitudes, but 0.75 may leave a map that looks empty. Your call; the field exists either way.
  **Answer (2026-10-23, provisional):** defaults kept; the threshold is also a request parameter. What that implies: (1) `qa_min` is validated per request (0.00 to 1.00 in steps of 0.01) and recorded in the `PYSENT_*` provenance and the result, so a file says which threshold made it; (2) **the output file name must carry the threshold** (or the service must key its cache on it), because two requests that differ only in `qa_min` otherwise overwrite each other and the bulk runner's "skip if it exists" rule would return the wrong map: pysent takes `output_names` from the caller, so this is a service-side checklist item; (3) the mask is applied at processing time, so a different threshold means a re-run (a second or less for an NRTI granule, 3 to 7 s for an orbit);
  (4) for S3 the equivalent is `mask_flags` (a list of flag names, or `default`/`none`). Open option, not decided: also store the quality value as a second band (UInt8, 0 to 100) so a threshold could be applied later without reprocessing; it makes the file larger and depends on what the WMS styling can do with a second band.
- **Q4 (Phase 1). Grid policy.** `auto`: EPSG:4326 at 0.05° when the swath neither crosses ±180° nor contains a pole nor reaches south of ~30°N; otherwise EPSG:32661 at 5 km (S1's CRS), 32761 in the south. *`auto`.* Fixed 4326 is wrong at poles and the seam (5.5 % holes, a smear across longitudes; picture 4) and a global orbit still shows fringe artefacts at the polar cap; fixed 32661 cannot hold a global orbit.
- **Q5 (Phase 1). Resampling default.** *`average`* (matches the exact-footprint reference, +30–45 % time, picture 2) over `near` (visible along-track streaks).
- **Q6 (Phase 1). Stored and displayed units.** *Store native units (mol m⁻², ppb, K); display units are style/query concerns, conversion factor recorded.* Alternative: store µmol m⁻² or °C so a GetFeatureInfo needs no conversion.
- **Q7 (Phases 1–5). Colour maps and ranges.** The proposals in B.2 are placeholders with pictures for NO2 (two QA levels), WFR chlorophyll, LST, VG1 NDVI and OLCI true colour; the rest have a range but no picture yet. *Pictures first, then you choose, per phase.* The WFR picture's red patches (coasts, cloud edges) suggest masking `ADJAC` and a cloud-margin dilation: your call.
- **Q8 (Phase 1). Layer time.** pysent always records start, end and (S5P) the median scanline time. *WMS TIME = start for a point-in-time layer, with the range exposed as the time extent;* mid-time would suit an orbit of 58 minutes, start suits a 100-second granule. Must match the viewer.
- **Q9 (Phase 1). Empty scene.** *Raise `EmptySceneError` (as S2) so a bulk runner skips it, and let the service show "no valid data" for the record*; `on_empty="write"` produces an all-NaN file. High-latitude NRTI NO2 is often empty (A9), so this will happen daily.
- **Q10 (Phase 0). Routing strictness.** *Unsupported family or product type fails early with a clear 422-style error; also for S1 SLC and S2 L2A, which the catalogue holds (204 k / 2.9 M) and which today either fail inside GDAL or are processed as if GRD/L1C.* Rejecting them changes behaviour for those records only (not for GRD/L1C output). The service route must call `resolve_sentinel_pipeline`.
- **Q11 (before Phase 3). S3 point-query source.** *(A) sample the published GeoTIFF, plus an optional `reject_reason` band;* (B) companion NetCDF/Zarr; (C) query the zip members in place (details in B.5).
- **Q12 (before Phase 4). S3 interactive accuracy, and who signs off the masks.** Interactive uses the validated decimated geolocation step (98 m mean error at step 16, about a third of an OLCI pixel; 7–23 s); the exact warp (0.2 m, 160–204 s) is bulk-only. *Decimated on interactive, exact on bulk.* The default flag masks of B.2 are mine, **not checked against the product notices**; *someone who knows the products should review them before the first S3 layer is published.*
- **Q13 (before Phase 5). WST file and quality.** DUAL (two views, more accurate) or NADIR (wider swath), and `quality_level ≥ 4` or ≥ 3. *DUAL, ≥ 4 for the default; NADIR selectable.* Not measured.
- **Q14 (before Phase 2). `O3__PR` columns and `O3_TCL`.** *Include the `O3__PR` total and tropospheric columns; skip `O3_TCL` until it has been opened.*
- **Q15. Records that cannot be ingested.** 17 % of the sampled S3 records have no download reference, ~4 % of S5P, and a third of NRTI NO2 files are 0-byte stubs. pysent will raise a precise error; *the catalogue page should grey the Ingest button and say why*. That is a service change, noted here so it is not lost.
- **Q16 (before Phase 7). Composites and vector products.** *Separate plans; composites first.*

## D. Risks, ranked

1. **Empty or unavailable inputs are the common case, not an edge** (A2, A3, A9): high-latitude S5P granules, 0-byte stubs, records without a link. Without early, specific errors the user clicks Ingest and gets a failed job or a blank map. *Mitigation: Phases 0 and 1 (errors, `valid_fraction`), Q9, Q15.*
2. **Misrouting is live today** (A5): 31 % of S3 records run through the S1 pipeline in the service. *Phase 0, plus the service route calling `resolve_sentinel_pipeline`.*
3. **Poles and the antimeridian** (A12): wrong or holey output in lat/lon. *Q4 grid policy, tests on the named granules; the polar cap of a global orbit in 4326 still needs a decision.*
4. **GDAL 3.8.4 geolocation traps** (A20): silent wrong shifts, a truncated warp at step 4, unscaled arrays aborting. The docs image uses GDAL 3.13 and may behave differently. *One test per trap on 3.8.4, output validation, never trust a step value.*
5. **The QA default decides what users see** (A8, Q3): 25 % versus 81 % of a scene. *Owner decision with pictures; the option always exists.*
6. **Baseline-dependent names and log10 packing** (A18): a naive unpack shows log10(chlorophyll) or fails on baseline 003. *Lookup by name with baseline fallback, tests on both baselines.*
7. **Flag masks and the OLCI reflectance are unverified** (A19, A24): wrong defaults would look plausible. *Q12 review; Phase 6 validated against L2 reflectance.*
8. **Cost on the interactive queue** (A21): exact S3 swath warps are 160–204 s and super-linear. *Decimated step on interactive; bulk for exact; a per-request pixel cap.* Multi-variable warps, `average` on S3 and a 4 GB-limit run are unmeasured.
9. **Service coupling** (B.4, B.5): routing, the NetCDF point query (groups, packed `qa_value`, time axis), time-dimension handling and the Ingest button all live in the service and cannot be verified from this repository. *Treat as a checklist for the service's own integration notes before each phase merges.*
10. **VG1/V10 value is limited in the Arctic** (A23): 75°N cap, 38 % valid daily. *State it in the catalogue text; do not oversell.*
11. **Memory per Celery child** (the S1 lesson): GDAL cache per process. *`gdal_cachemax_mb` always explicit; measure under the queue's real concurrency in Phase 1, not only single-process.*
12. **S1 SLC / S2 L2A in the catalogue** (A1): outside this plan but affected by Q10.

---

## Session log

- **2026-10-23 — planning session (audit, no code).** Read memory and the agent knowledge base; created the worktree from `origin/main` @ `0bc5732`; surveyed the catalogue; downloaded and probed S5P files and (in a parallel agent) S3 files; measured the warp candidates; wrote this plan.
  Deviations from the brief, in the order they bit: (1) the catalogue cannot be harvested (page size 10, 2.7 M S3 records), so statistics are 30 records per type with exact counts from `hits`; the first harvest attempt (`like 'S5P%'`) matched 793,726 records and ran out its 10 minutes, and was replaced;
  (2) the brief's S5P `ground_pixel = 450` holds only for the UV/VIS products (CO, CH4 215; AER_LH 448); (3) `O3__PR` also carries 2-D columns; (4) the brief's "60 M pixels per OLCI array" is 19.9 M, and the real giant is `SY_2_VGP` (355 M); (5) the brief's "radiance → reflectance" catalogue abstract is wrong for L1; (6) S3 detection was already broken (31 % misclassified), not merely incomplete;
  (7) a third S5P stream, RPRO, exists; (8) corner-polygon rasterisation, planned as the accurate method, was **rejected** (35 s, 2.9 GB per orbit, wrong at ±180) and kept as an oracle, while the plain geolocation warp turned out to match it to 0.3 %; (9) S5P product sizes were assumed to need the bulk queue: an orbit takes 3–7 s and 394 MB, so none do, but S3 OLCI frames do for the exact warp;
  (10) 17 % of S3 and ~4 % of S5P records have no download reference, and a third of the sampled NRTI NO2 files are 0-byte stubs: not in the brief, and the reason Phase 0/1 start with errors.
  Open: Q1–Q16. Not done and flagged in A.4: anything that needs the product notices.
- **2026-10-26 — catch-up session.** Completed **Q2**, whose answer had been cut off mid-sentence on 2026-10-23: pysent writes **three files** per derived product — the Float32 GeoTIFF for the WCS, a **full-resolution 8-bit styled copy** for the WMS, and the 256 px thumbnail. The owner chose the styled copy over "float plus a style hint". Consequences recorded under Q2 and in B.2; the one that changes the plan's shape is that **Q7's colour ranges are now load-bearing** and must be settled during Phase 1, because file 2 bakes them into pixels — so Phase 1 must also make the styling step callable on an existing float product, letting a range change re-render without re-warping. Float compression (`DEFLATE`+predictor 3 versus `LZW`) and RGB-versus-paletted for the styled copy were both left to Phase 1 measurements rather than owner decisions. No code yet; this commit is the plan only.
- **2026-10-26 — Phase 0 session (detection, routing, early errors).** Landed on `feature/s5p-s3-phase0`, stacked on the plan branch so it did not wait for the plan PR (#18) to merge.
  - **What landed.** `profiles.py` gets the anchored token pattern of A5 and learns `S3`/`S5P`; new `detect_nbs_sentinel_product` (adds `product_type` and, for S5P, `stream`), `detect_nbs_sentinel_product_for_dataset`, `resolve_sentinel_pipeline`, `supported_sentinel_product_types` and the `SENTINEL_PRODUCT_TYPES` table; new `errors.UnsupportedProductError` (a `ValueError`) and `errors.EmptyInputError`; `_safe.reject_empty_input` called from `resolve_safe_root`. All five new names are exported lazily from the package.
  - **The bug, confirmed on `main` with real titles** (`show_main_behaviour.py`, kept in the session scratchpad): all six sampled S3/S5P titles classify wrongly — `OL_2_WFR` and `SY_2_VG1` and `OL_1_EFR` as `S1`, `SL_2_LST` (a `PS2` title) as **`S2`**, both S5P titles as `unknown`, which the service also sends down the S1 path. A 0-byte stub resolved to a `/vsizip/` path with no error, leaving GDAL to fail later.
  - **Decision: `planned` is a status, not a route.** The table marks S3/S5P types `planned` with their phase, and `resolve_sentinel_pipeline` *refuses* them with "planned (phase 1) but not implemented in this release" rather than returning `"s5p"`. Returning a pipeline name for a module that does not exist would have made Phase 0 unshippable on its own. Each later phase flips its own rows to `ready`; nothing else changes.
  - **Deviation from B.4: the archive path also sets the family.** B.4 had the path as a `product_type` fallback only, but a path with no filename (`/nbsArchive/2026/09/22/TR-L2-NO2/`) then detected a type with `family="unknown"` and was refused. `TR-` and `OL-/SL-/SY-/SR-` belong to one family each, so the path fallback now fills the family in too.
  - **Scope note.** `S1 GRD`/`S2 MSIL1C` are the only `ready` types, so `supported_sentinel_product_types()` returns four strings today. S1 SLC and S2 L2A are listed explicitly as `unsupported` (Q10) so their refusal names a reason instead of falling through.
  - **Tests.** `tests/test_detection.py`, 60 cases: real titles for every family and product type, the `PS1`/`PS2` regression, the archive-path forms, routing and each refusal message, a 4,000-case property test that UUIDs and S2 tile ids never match, and token-boundary cases. Four more in `tests/test_safe_inputs.py` for the stub and the missing reference. Suite in the CI container: **206 passed, 1 skipped** with numba, 201 passed + 5 skipped without. Every new test fails on `main` — the detection ones behaviourally, the rest because the API is new.
- **2026-10-26 — Phase 1 session (S5P NO2 end to end).** Landed on `feature/s5p-s3-phase1`, stacked on the Phase 0 branch. New `src/pysent/_swath.py` (reusable swath machinery: masked multidimensional read, grid policy, bounds, geolocation warp, footprint, statistics) and `src/pysent/s5p.py` (the NO2 pipeline, the product table, request validation, three outputs, provenance). `SENTINEL_PRODUCT_TYPES` flips `("S5P", "L2__NO2___")` to `ready`. 265 tests pass in the CI container (261 without numba); `tests/data/s5p_no2_window.nc` is a committed 350 KB window of 60 real scanlines, cut by a new `build_s5p` in `scripts/make_test_data.py`, so CI runs offline.
  - **Correction to A10/A11/A13: the audit prototype was half a source pixel out.** It did not set `GEOREFERENCING_CONVENTION=PIXEL_CENTER`, so GDAL read every geolocation sample as a pixel's top-left corner. Measured on the Europe granule by asking how many masked source pixels land in the output cell that contains their own coordinates: **95.3 % with `PIXEL_CENTER`, 29.5 % without**. A20b had found this for S3 at 300 m (210 m shift); it matters far more at Sentinel-5P's 3.5–7 km pixels. The shipped warp sets it, and 95.1 % of commonly-valid cells differ between the two conventions, so **the audit's geometry and value numbers were all computed on the shifted grid**. Their conclusions still hold (geolocation warp beats corner polygons; `average` beats `near`), because both arms of each comparison were shifted equally.
  - **Deviation from Q4/B.4: a whole orbit stays in EPSG:4326.** The literal policy ("crosses ±180° or contains a pole → 32661") sends a pole-to-pole orbit to a polar grid, which A12 already says cannot hold one. `choose_grid` therefore treats a latitude span over 120° as an orbit and keeps it in lat/lon, accepting the known polar-cap fringe, and uses the polar grid only for *regional* polar or seam-crossing swaths. Tested for all four cases.
  - **Q2's two delegated measurements, answered.** Float compression: **DEFLATE beats LZW by 25–30 % everywhere**, so the plan's choice stands. The predictor is not clear-cut — predictor 3 loses on a sparse map (452 KB vs 408 KB at `qa_min` 0.75, 19 % valid) and wins on a dense one (1,051 vs 1,107 KB at 0.5; 1,314 vs 1,458 KB unmasked), so **predictor 3 stays** because it wins where the files are actually big. Styled copy: **paletted, not RGB** — one band with a colour table is **1.7×** smaller than RGBA (87 KB vs 146 KB), keeps one value per pixel so a reader can recover the class, and carries transparency for nodata.
  - **Correction to B.3's memory extrapolation.** Peak memory is linear in *output* cells at about **39 MB per million** (6 M → 844 MB, 24 M → 1.5 GB, 49 M → 2.3 GB, 96 M → 3.8 GB on a whole orbit under `--memory=4g`). The plan guessed 100 M cells ≈ 1.5 GB; it is nearer 3.9 GB, i.e. the entire interactive budget. `max_output_pixels` therefore defaults to **40 M interactive** (≈1.6 GB) and **150 M bulk**, not 100 M / 400 M.
  - **Three GDAL ordering traps**, each found by a failure and each now commented at the call site: a palette must be set before the first pixel is written (libtiff cannot change `PhotometricInterpretation` afterwards); a netCDF fill value must be defined before the variable holds data; and `gdal.Open(...).GetRasterBand(1)` is invalid once the dataset is collected. Also: `gdal.Warp` left to compute its own output bounds logs "unable to compute output bounds" on an orbit and carries on, so the thumbnail warp now states bounds and projection explicitly.
  - **Acceptance checks** of B.7 that this phase covers: physics ranges (1), masks against an independent recomputation (2 — compared as *patterns*, since a 0.05° cell is finer than a Sentinel-5P pixel across track, so valid-cell counts legitimately exceed source-pixel counts), footprint containment and ±180 splitting (3), the empty granule and 0-byte stub (5), idempotent re-run and atomic output (6), and the 4 GB run (9). The polar granule the plan names for check 4 **could not be fetched** (the catalogue path 404s), so the pole and antimeridian policy is tested on synthetic coordinates only; refetching it is outstanding.
  - **Still open for the owner: Q7's colour range.** `no2` ships the plan's proposed 0–1e-4 mol m⁻² with `viridis`, and the styled copy bakes it into pixels, so this needs confirming from pictures before bulk output exists. The float product is unaffected and the styling step reads it back, so a change re-renders rather than re-warps.
- **2026-10-26 — Phase 0 follow-up: the archive directory names use dashes.** Found while fetching the Phase 2 test files, by listing what the archive actually contains. The directories are `TR-L2-AER-AI`, `TR-L2-O3-PR`, `TR-L2-O3-TCL`, `TR-L2-NP-BD3`…, and the path parser's `[A-Z0-9_]+` stopped at the first dash: **5 of the 14 real Sentinel-5P directory names parsed wrongly** (`TR-L2-AER-AI` → `L2__AER___`, `TR-L2-O3-TCL` → `L2__O3____`). Harmless today, because every affected type is still refused — but `TR-L2-O3-TCL` reducing to plain O3 would, the moment Phase 2 flips O3 to `ready`, process a deliberately deferred product as a supported one. That is precisely the silent misroute Phase 0 exists to prevent, so it is fixed now rather than later.
  A substitution rule cannot do this (the archive writes `TR-L2-O3-PR` for a type whose real name is `L2__O3__PR`, with two underscores), so the 14 names are an explicit map, checked against the archive's own listing and cross-checked against real filenames. An unknown directory now yields no type at all instead of a guess. Sentinel-3 keeps the derived rule, with dashes translated to underscores.
- **2026-10-26 — Phase 2 groundwork (no pipeline code yet).** Fetched one NRTI granule per remaining Level-2 type and checked B.2's proposals against the files, so Phase 2 starts from verified names rather than from the audit's notes. Branch `feature/s5p-s3-phase2`, stacked on Phase 1.
  - **All nine proposed variable names exist, and every width matches A6**: CO and CH4 215, AER_LH 448, the UV/VIS types 450. B.2's table can be used as written.
  - **Measured ranges** (`qa_min` 0.5, best available granule per type, p01 / p50 / p99):

    | Product | Units | Valid | p01 | p50 | p99 | B.2 proposed range | Verdict |
    |---|---|---:|---:|---:|---:|---|---|
    | `co` | mol m⁻² | 1.9 % | 0.0251 | 0.0307 | 0.0356 | 0.02 – 0.05 | fits |
    | `hcho` | mol m⁻² | 59.6 % | −3.40e−4 | 6.37e−5 | 4.66e−4 | 0 – 4e−4 | p99 just over; 5e−4 is safer |
    | `so2` | mol m⁻² | 27.6 % | −1.92e−3 | 2.11e−5 | 2.35e−3 | 0 – 2e−3 | p99 just over; noise-dominated anyway |
    | `o3` | mol m⁻² | 9.8 % | 0.1235 | 0.1346 | 0.1423 | 0.10 – 0.16 | fits |
    | `ch4` | ppb | 6.4 % | 1868 | 1930 | 1962 | 1800 – 1950 | **p99 1962 exceeds it**; 1800–2000 fits |
    | `aer_ai` | 1 | 41.9 % | −1.54 | −0.28 | 1.84 | −2 – 4 | fits |
    | `cloud` | 1 | 7.2 % | 0.36 | 0.93 | 1.00 | 0 – 1 | fits by definition |
    | `aer_lh` | m | 6.3 % | 185 | 554 | 8505 | 0 – 10,000 | fits |
    | `o3__pr` | mol m⁻² | 12.2 % | 0.1251 | 0.1362 | 0.1442 | (as `o3`) | fits |

  - **Closes an A14 gap:** the audit could not measure CH4 because every file it had was 100 % fill. A daylight granule gives 6.4 % valid and 1868–1962 ppb, so the proposed range can be checked after all — and it needs widening at the top.
  - **New finding, not in the audit: `O3__PR` is on a much coarser grid.** A15 established that the 2-D columns exist but not their shape. They are **75 × 77**, about **29 km across track and 28 km along track**, against 3.5–7 km for every other Level-2 type. The default 0.05° (≈5.5 km) grid would oversample it about fivefold, producing a blocky map that pretends to a resolution it does not have. Phase 2 must give this type its own default resolution (0.25°, ≈28 km) rather than inheriting the family default. Both `ozone_total_column` and `ozone_tropospheric_column` are present and plausible (p50 0.136 and 0.0178 mol m⁻²).
  - **Night granules are useless for range work.** The 01:14 UTC pass returned 0 % valid for HCHO, SO2, CH4 and AER_LH; the 09:34–11:14 passes were refetched for those. Worth remembering when choosing Phase 2's committed test windows: cut them from daylight granules or the fixtures test nothing.
  - **Still to decide before Phase 2 writes code:** the per-type colour ranges are Q7 again, nine times over. The owner has the NO2 comparison page; the sensible ask is a *rule* (for example "p01 to p99 of a reference daylight granule, rounded outwards") rather than nine separate answers.
- **2026-10-26 — Q7 answered, and Phase 2 (the other S5P L2 types).** The owner accepted the decision page's recommendation and the proposed rule. Branch `feature/s5p-s3-phase2`, stacked on Phase 1.
  - **Q7, as decided.** NO2 keeps **0 – 1e-4 mol m⁻² with `viridis`**, fixed rather than per-granule — a per-granule stretch never clips but makes two days incomparable by colour. For the rest, the range is the **measured p01–p99 of a reference daylight granule, rounded outwards**. Where that conflicted with the NO2 answer, the explicit NO2 answer won: the rule would have given roughly −2e−5 – 8e−5 there.
  - **Three places the rule is deliberately not applied**, because a percentile is the wrong instrument:
    1. **`cloud` is 0 – 1**, from the definition of a fraction. The measured p01 was 0.36, and a range starting there would render clear sky as "no data".
    2. **`aer_ai` is −2 – 2 with a diverging ramp.** Zero is physically meaningful (negative is clean or cloudy air, positive is absorbing aerosol), so the range is symmetric about it rather than about the data.
    3. **Negatives are floored at 0 for display** on the trace-gas columns (`no2`, `hcho`, `so2`). They are real instrument noise, kept in the float product and clipped in the styled copy, which is the Q2 decision applied consistently.
  - **Ranges that changed from B.2 because the measurement said so:** `ch4` 1800–1950 → **1850–2000** (measured p99 1962, and the audit had no unmasked methane to check against); `hcho` 0–4e−4 → **0–5e−4**; `so2` 0–2e−3 → **0–2.5e−3**; `co` 0.02–0.05 → **0.02–0.04**; `o3` 0.10–0.16 → **0.12–0.15**; `aer_lh` 0–10,000 → **0–9,000**. The rest are as proposed.
  - **Colour ramps.** One sequential ramp (`viridis`) for every trace-gas column, so two gases are comparable at a glance and the background of a clean scene reads as ocean rather than as missing data; `rdbu` for the aerosol index because it diverges; `grey` for cloud fraction, which reads as cloud.
  - **Eleven products, nine types.** `O3__PR` yields two (`o3_pr_total`, `o3_pr_tropospheric`), so the table is keyed by product rather than by type, with `S5P_TYPE_PRODUCTS` mapping one to the other. New `default_sentinel_s5p_products()` means `products=None` now derives *whatever the file is for* instead of always attempting NO2 — a Sentinel-5P file holds one gas, so that was the common request.
  - **Bug this phase introduced and caught: a product's resolution has to be a ground distance.** `O3__PR` needs ~28 km cells rather than the family's 0.05°. Written as `0.25` degrees it worked on a geographic grid and asked for 0.25 **metres** on a polar one — **114 trillion cells**, stopped only by the `max_output_pixels` guard, which is the first time that guard has earned its keep. A product cannot know which grid its granule will land on, so the table now states `ground_sampling_m` and `choose_grid` converts once the CRS is known. Regression test included.
  - **Verification.** 295 passed / 12 skipped in the CI container, 291 / 16 without numba, and **306 passed / 1 skipped with the real granules mounted** (`PYSENT_S5P_ARCHIVE`), which is the check that matters: every product's median on a real granule falls inside its colour range. A new `s5p_archive` fixture skips those tests when the directory is absent, so CI stays offline. All eleven products ran under `--memory=4g`: 0.2–0.8 s and ≤ 260 MB for an NRTI granule.
  - **Still not done:** the night-pass granules for HCHO, SO2, CH4 and AER_LH raise `EmptySceneError` as they should, but the committed test windows are still NO2 only — a per-type offline fixture set would need ~9 × 350 KB and was judged not worth it while the real-archive tests cover the same ground. Phase 2's `O3_TCL` and `NP_BD*` stay deferred.
- **2026-10-26 — Phase 3 (Sentinel-3 foundation + SY_2_VG1/V10).** Branch `feature/s5p-s3-phase3`, stacked on Phase 2. New `src/pysent/_sen3.py` (archive reader) and `src/pysent/s3.py` (the NDVI pipeline on the regular-grid path), plus `src/pysent/_output.py` factored out of `s5p.py` so both families emit the same three files from the same code. `SY_2_VG1` and `SY_2_V10` flip to `ready`.
  - **The trap that shaped the reader: GDAL's netCDF driver cannot open a file inside a zip without Linux `userfaultfd`**, which Docker's default seccomp profile blocks. The error names the missing syscall, so it reads as a container fault rather than a data one — and it would strike a Celery worker in production while passing on a developer's machine with `--security-opt seccomp=unconfined`. `GDAL_SKIP=netCDF` does **not** help, because the `NETCDF:` prefix itself selects the driver. The HDF5 driver opens the same member but returns raw pixels with **no scale, no fill value and no geotransform**.
    So the reader **copies the member out of the archive first** and opens the copy. Because every member is STORED (A16) that copy is a byte-range read: **12.9 MB in 0.01 s locally, 3.4 MB in 0.12 s over HTTP**, and the netCDF driver then reports the full packing and grid. A remote quicklook end to end is **1.53 s** with no download, and gives pixel-identical results to the local read.
  - **Flag tables are read from the file, never hard-coded.** Sentinel-3 mixes two CF conventions in one variable, which A19 noted and which is easy to implement wrongly: single bits have `mask == value` (`land` is bit 8), while a small enumeration shares a mask — the Synergy status word puts `clear`, `unfilled`, `uncertain`, `cloud` in the low two bits as values 0, 1, 2, 3 of mask 3. **Treating `clear` as a bit test matches nothing, and a mask that matches nothing silently keeps everything**, which looks exactly like unusually clean data. `read_flag_table` returns both forms as `{name, mask, value}` rows and an unknown flag name raises with the list the file defines.
  - **Q7's rule agreed with B.2 here**, for once needing no deviation: measured p01–p99 of clear NDVI was 0.03–0.85 on the European summer tile and −0.08–0.86 on a North American winter one, which rounds outwards to the proposed **−0.1 – 0.9**. Note that −0.08 is the bottom of the byte packing (`raw*0.004−0.08`), not a measurement. Ramp is `brown_green`, added for this: not perceptually uniform, but brown-to-green is the convention and a reader expects it.
  - **Q11 implemented as answered**: `query_source` points a point query at the published raster (`method: "sample_raster"`), and the optional `reject_reason` band says *why* an empty pixel is empty. On the full EUROPE tile that reads **valid 27.8 %, fill 62.1 %, cloud 5.4 %, uncertain 4.7 %** — the difference between a map that looks broken and one that explains itself. Off by default.
  - **Measurements** (`--memory=4g --cpuset-cpus=0-7`, all three outputs): EUROPE tile at full 45.8 Mpx **8.9 s / 2.1 GB**, at ×4 **0.8 s**, quicklook (×8) **0.55 s**; remote quicklook **1.53 s**. The audit prototype was 1.6 s for a ×4 decimated PNG, so the shipped pipeline at ×4 is faster while writing three real products. Peak memory is about 46 MB per megapixel, close to Sentinel-5P's 39.
  - **Confirms the audit:** every member STORED (17 of 17), EUROPE box exactly 25–75°N / 11°W–62°E, grid 1/112°, 37.9 % of the tile carrying a value and 27.8 % usable after masking — A23's "about 38 %" and "28 % clear".
  - **Two bugs of my own, both caught by a failing run.** `sen3_root` tested the `.zip` suffix before the `http` prefix, so a URL became `/vsizip/https://…` and failed with a missing-manifest error that looked like bad data; the order is now explicit and tested. And the fixture builder read `band.DataType` after releasing the dataset — the exact trap recorded in Phase 1's gotchas, which I then walked into.
  - **Offline fixture:** `tests/data/s3_synergy_window.zip`, a 384² window over southern Norway, 462 KB, members STORED, full flag table, manifest and 1/112° grid intact. Written through the **multidimensional** API with CF `lat`/`lon` coordinate variables, because the classic netCDF writer names every variable `Band1` and the pipeline opens members by their real name.
  - **Verification:** 342 passed / 12 skipped with numba, 338 / 16 without, **353 passed / 1 skipped with the real granules mounted**.
  - **Q12 is still outstanding and now matters.** The `clear`-only mask is a proposal read off flag names, not from the product notices. This is the first Sentinel-3 layer, so the review the owner agreed to should happen before it is published; the mask is a per-product option and `mask_flags` accepts any names the file defines, so a reversal is a request change rather than a code change.
- **2026-10-30 — Phase 4 (swath warp + OLCI L2).** Branch `feature/s5p-s3-phase4`, stacked on Phase 3. Five products (`chl`, `tsm`, `kd490` from WFR/WRR; `gifapar`, `otci` from LFR/LRR), the geolocation warp with the A20 traps handled, baseline-tolerant variable resolution, and a log colour scale. All four OLCI types flip to `ready`.
  - **A20a is worse than recorded, and its failure mode depends on whether bounds are given.** The audit said an unscaled Int32 geolocation makes `gdal.Warp` "abort". It does not. Without explicit `outputBounds` GDAL computes the extent from ±2 billion "degrees" and **the process is OOM-killed** — I lost a container to it at a 4 GB limit before working out why. *With* explicit bounds it is quieter and worse: it logs "too many points failed to transform", writes a raster of **exactly the right size with zero valid pixels**, and returns successfully. Measured: 0 valid against 2,171,012 for the scaled array on the same frame. So `warp_swath` now takes a `minimum_valid` floor and checks its own output; nothing else would catch this.
  - **A20b confirmed and quantified on OLCI.** Share of source pixels reaching the output cell that holds their own coordinates: **88.1 % with `GEOREFERENCING_CONVENTION=PIXEL_CENTER`, 47.5 % with GDAL's default** — a one-source-pixel shift, about 300 m.
  - **A20c confirmed, and it is the trap that decides whether decimation is usable at all.** With the `-(n-1)/2` offset, accuracy barely moves across steps (88.1 % at step 1, 87.9 / 87.6 / 87.3 / 86.7 % at 2 / 4 / 8 / 16). Without it: 47.5 % at step 2 and **0.0 %** at 4, 8 and 16, the miss growing to 10.6 source pixels (≈3 km) at step 16.
  - **A20d did not reproduce.** Step 4 measured correct on the 296-row baseline-003 frame (87.6 %, coverage within 0.001 % of step 1). The audit saw it truncate on a full 4091-row frame, so it stays refused — steps 2 and 8 are indistinguishable from it in accuracy, so allowing a value that has been seen to fail buys nothing. Recorded as an asymmetry rather than a confirmed finding.
  - **A21's cost curve is wrong in both directions, and the direction reverses with frame size.** On the full 19.9 Mpx frame at 0.003°: step 1 **39.4 s**, step 2 9.2 s, step 8 **5.6 s**, step 16 6.6 s, all ≈930 MB and all within 0.006 % on valid pixels. A21 reported 160 s for step 1; with explicit bounds it is 39 s and **linear**, not "strongly super-linear" — the super-linearity was probably GDAL computing its own bounds. But on the *small* 1.4 Mpx frame the order inverts: step 1 is 2.8 s and step 32 is 9.4 s. So `geoloc_step="auto"` chooses on **frame size**: exact at or below 4 Mpx, step 8 above. Accuracy is not the criterion, because it barely depends on the step.
  - **Log packing is self-describing, so it is detected rather than hard-coded.** `CHL_NN` has units `lg(re mg.m-3)`, `TSM_NN` `lg(re g.m-3)`, `kd_490` `lg(re m-1)`; the non-log variables (`IWV` `kg.m-2`, `PAR`, `GIFAPAR`, `OTCI`) carry no `lg(`. Checked across both baselines. This matters more than it looks: the packing *parameters* also move between baselines (kd490's offset is −6.0 under 004 and −2.0 under 003), so anything table-driven would be wrong.
  - **Baseline drift is wider than A18 recorded.** `chl_nn.nc` exists in **both** baselines — A18's name differences are about the OC4Me/chlor_a variants, not the neural-net product. But the flag *sets* differ: **57 flags under baseline 004, 53 under 003**, and 003 has no `CLOUD_SHADOW`. My strict "unknown flag name raises" rule — added in Phase 3 precisely so a mask cannot silently do nothing — therefore broke a **default** mask on the older baseline. Resolved by splitting the two cases: a **requested** flag name is strict, a **default** one may be absent, is skipped, and is reported in `unavailable_flags` and the provenance. Silently ignoring it would hide a stale table; refusing the product would be wrong.
  - **Masks are now keep + reject**, because that is how the documentation phrases them and it is not expressible as one list: a water product keeps `WATER` and rejects eleven failure conditions. Phase 3's `mask_flags` keeps its meaning (flags that must be set) and `reject_flags` is new, so nothing already published changes meaning.
  - **Evidence for Q12, which is still open.** On a marine baseline-004 frame, `CHL_NN` has a value on only **1.79 %** of pixels and `WATER` is set on 1.28 %, so the mask starts from 1.28 %. The rejects then remove **59 % of what was available**, leaving 0.53 % — and the loss is concentrated in one flag: **`CLOUD_MARGIN` is 70 % of it** (0.53 pp of the frame, i.e. 42 % of the available water pixels), then `CLOUD_AMBIGUOUS` 14 %, `OCNN_FAIL` 10 % and `CLOUD_SHADOW` 6 %. The other seven reject flags remove **nothing at all**, because those pixels carry no retrieval to begin with. (Corrected 2026-10-30: an earlier version of this entry and of PR #23 gave 41 % for `CLOUD_MARGIN`, which is its share of the *available* pixels, under a heading that said share of the loss.) Rejecting a cloud buffer is standard for quantitative retrieval; for a browse product in a cloudy Nordic autumn it may be the wrong trade. That is a precise question for a reviewer rather than a judgement for me.
  - **Q7's rule needed a union for chlorophyll.** One granule is not enough for a quantity spanning four orders of magnitude: a clear-water reference gave p01/p99 of 0.29/30, this coastal frame 2.07/32.5 (max 74.8). Either range alone clips the other regime, so the shipped range is **0.01 – 50 on a log scale** — 0.01 keeps oligotrophic ocean on the ramp, 50 covers the coastal p99. Recorded as a refinement of the rule, not a departure from it.
  - **Offline fixture:** `tests/data/s3_olci_window.zip`, a 320² window chosen for maximum usable water (57,844 pixels, 56 % of the window), 1.7 MB, keeping the Int32 micro-degree geolocation, the log-packed science variable and the full 57-flag UInt64 `WQSF`.
  - **Verification:** 370 passed / 12 skipped with numba, 366 / 16 without, **381 passed / 1 skipped with the real products mounted**. One test documents why step 2 is excluded from the offset comparison: its shift is sub-cell on a synthetic 1:1 grid, though it is visible on a real frame.
  - **Two mistakes of my own worth recording.** I wrote a test that monkeypatched `gdal.Translate` to remove the offset, which did nothing because `warp_swath` overwrites the whole `GEOLOCATION` dict immediately afterwards; the no-offset case is now built by hand. And I hit the dangling-dataset trap for the third time in this plan (`band.DataType` after releasing its dataset) — it is in the gotchas file and I still walked into it, which says the note should be a lint rule rather than prose.
- **2026-10-30 — the two-audience mask split, re-styling, and the owner's review study.** Branch `feature/s5p-s3-restyle`, stacked on Phase 4. Answers the owner's two asks of 2026-10-30 and implements the split the owner approved.
  - **Verified that the review study does not need re-rendering.** The study was rendered from `feature/s5p-s3-phase2` at `01b3df6`, and phases 3 and 4 changed code Sentinel-5P shares — `s5p.py` lost 198 lines to the `_output.py` extraction and `_swath.py` gained the geolocation step and the output check. Rendering the same three Sentinel-5P cases on both branches and hashing pixels, geotransform and projection separately from tags: **pixels, geotransform and projection are byte-identical**; the only difference is one new tag, `PYSENT_STYLE_SCALE`, on the styled copy. Nothing an expert sees has moved.
  - **One change was caught and reverted for the freeze.** Making the thumbnail average the data rather than the overviews is a real improvement on a sparse map (30.1 % of cells drawn from overviews against 33.5 % from full resolution), but it *would* have changed every Sentinel-5P thumbnail. It is now opt-in (`materialise=True`), Sentinel-3 asks for it, and Sentinel-5P renders exactly as before.
  - **The mask is now two tiers, and the two outputs no longer hide the same pixels.** The hard tier is about *validity* — invalid, land, cloud, snow, saturated, glint, whitecaps, atmospheric-correction or retrieval failure — and is dropped everywhere. The marginal tier is about *contamination* — `CLOUD_MARGIN`, `CLOUD_AMBIGUOUS`, `CLOUD_SHADOW` — and is **kept in the Float32 product with the reason recorded, hidden only in the browse image**. The reason band is warped in the same pass as the values with nearest resampling, so a code is never averaged into a different code.
    Measured on the committed window: the float product goes from 28.1 % to **38.1 %** of cells carrying a value — a 36 % gain for the coverage service — while the full-resolution browse image is **byte-for-byte identical** to what Phase 4 produced. The extra pixels are `cloud_margin` 6.9 %, `cloud_shadow` 2.9 %, `cloud_ambiguous` 0.1 %.
    The Synergy status word gets no marginal tier: its classes are the whole decision, so NDVI is unchanged.
  - **`restyle_sentinel_s3_product()`** re-renders the styled copy and thumbnail from an existing float product in about 0.06 s, reading anything not given back from the product's own `PYSENT_*` tags and never rewriting the float. This is the owner's ask 5(b): a colour decision is now reversible after publication, and a study can put several candidate ranges in front of a reviewer without warping anything twice.
  - **Section 4 of the owner's message, item by item.** *Implemented:* `CPL_TMPDIR` is pointed at the scratch directory for the duration of every warp and restored afterwards — GDAL's geolocation hole fill writes work files there or to the current directory, and in a read-only container it fails silently with one ERROR 4. *Already satisfied:* pysent opens Sentinel-5P through GDAL's **multidimensional** API (`OF_MULTIDIM_RASTER` plus group paths), not the classic `NETCDF:` string, so the grouped-file problem does not arise; all eleven products read correctly. *Noted, nothing to change yet:* `snow_ice_flag` is not read by any current product, so its two codings do not bite — when a phase needs it, decode from `flag_meanings` (the `read_flag_table` helper already does exactly that); `AER_LH` having no retrieval over snow or ice is consistent with the 6.3 % valid fraction measured in phase 2; `AER_AI` processor 02.08 lacking a cloud-fraction variable is irrelevant while `aer_ai` reads only `aerosol_index_354_388`; the local archive holding NRTI only since 2026-09 is why phase 1 and 2 measurements used NRTI granules and the orbit measurements used the one OFFL file. *Worth carrying into any range decision:* the end colours of a ramp also hold values sitting exactly on the range ends, so "share clipped" understates what draws in them — relevant to the study's 5 % rule.
  - **Known wart, deliberately left:** the styled copy writes `PYSENT_STYLE_RANGE` with repr formatting (`"0.0,0.0001"`) while the float product writes it with `%g` (`"0,0.0001"`). Aligning them would change a tag on products already rendered for the study, so it waits for the freeze to lift.
  - **Verification:** 386 passed / 12 skipped with numba, 382 / 16 without, **397 passed / 1 skipped with the real products mounted**.
