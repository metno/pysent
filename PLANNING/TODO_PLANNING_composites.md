# Multi-granule composites: audit and plan

**Goal:** let the service ask for one raster covering a region over a time window, built from every
granule that contributes — a daily mean NO2 map over Norway, and the same shape for the other
Sentinel-5P and Sentinel-3 products. Today every pysent entry point takes one input and produces one
or more products from it; a composite is the opposite shape.

| | |
|---|---|
| Branch / worktree | `feature/composites` → `../pysent.worktrees/composites` |
| Base | `main` @ `3a147c3` (2026-10-30) |
| Status | **Audit done, Q1–Q7 answered (2026-10-31).** Phase 1, the job shape, is unblocked. |
| Audit evidence | [`composites_audit/`](composites_audit/): `catalogue_day.py`, `accumulate.py`, `weights.py` |
| Comes from | `PLANNING_s5p_s3_pipelines.md` B.9 and Q16, which put composites before the vector products |

## Kick-off prompt

Copy into a new agent session; it can run from any directory.

```text
You are continuing the composites plan for the pysent library (github.com/metno/pysent).

Repository rules (mandatory):
1. Before anything else, read the repository memory:
   /home/ubuntu/.claude/projects/-home-ubuntu-dev-services-pysent/memory/MEMORY.md
   and every file it links, including the agent knowledge base it points to. Use that absolute
   path: a session started inside the worktree has a different default memory directory.
2. Work only in the plan's git worktree:
   /home/ubuntu/dev/services/pysent.worktrees/composites  (branch feature/composites)
   If it is missing, run this from /home/ubuntu/dev/services/pysent:
   git worktree add ../pysent.worktrees/composites feature/composites
   (add `-b` and `main` if the branch does not exist either). Rebase on origin/main before starting.
3. Read PLANNING/TODO_PLANNING_composites.md in full. Do the next unchecked phase, in order.
   Tick its checkboxes in the file as you complete them, and record every decision and
   deviation in the "Session log" section.
4. Verify the way CI does: run the full test suite in ubuntu:24.04 with apt GDAL 3.8 as a
   non-root user (see .github/workflows/ci.yml). Every bug fix needs a regression test that
   fails on main. PLANNING/composites_audit/ reproduces the findings below.
5. Commit once per phase and open one PR per phase against main. Do not merge without the
   user's go-ahead. Never add AI attribution (no Co-Authored-By trailer, no "Generated with"
   footer) to commits or PRs.
6. Do not change the output of an existing product. The Sentinel-5P defaults are frozen for an
   expert review study; the Sentinel-3 mask defaults are the owner's decision. A composite is a
   new product and must not alter any single-granule one.
7. At the end of the session, update the memory directory from rule 1 with what this session
   learned. When every phase is done, rename the file to PLANNING/PLANNING_composites.md.
```

---

## A. Audit report

Every number below was measured on real data on 2026-10-30 and is reproduced by the scripts in
`composites_audit/`. **✅ verified** means measured; **📖 read** means taken from code or a document
without running it.

### A1. A day over Norway is 23 granules, not "several" ✅

`catalogue_day.py 20261009`. For `S5P_NRTI_L2__NO2___` on 2026-10-09 the catalogue holds **30 records
for the day**, of which **23 have a bounding box intersecting a Nordic box** (2–32°E, 57–72°N). All 23
have a download reference. B.9 guessed "several"; the real number is an order of magnitude larger and
it is what every cost below scales with.

### A2. The catalogue cannot answer the query the service needs ✅

Measured against the pycsw 3.0-dev instance at the NBS endpoint:

| Constraint | Result |
|---|---|
| `dc:title like 'S5P_NRTI_L2__NO2____20261009%'` | **30** — correct |
| `BBOX(ows:BoundingBox,57,2,72,32)` | 1,239,604 |
| **`title like … AND BBOX(…)`** | **0** — in every ordering and with parentheses |
| `BBOX` with a 1° box over Oslo | 1,482,965 — implausible |
| `BBOX(ows:BoundingBox,-90,-180,90,180)` | **rejected**, "Invalid query syntax" |

Three separate problems. **Combining a title filter with a spatial filter returns zero**, although each
works alone. **The spatial filter does not discriminate**: a one-degree box matches 1.5 M records, so it
cannot be used to select inputs. And **a box whose second argument falls outside ±90 is rejected
outright** — the parser is validating that argument as a latitude, so the accepted argument order is
latitude-first and a longitude in that position silently means something else.

**Consequence for the service, which pysent cannot fix:** select inputs by **time**, using the title
pattern, which is exact; then filter spatially **client-side** on each record's `ows:BoundingBox`.

### A3. Half the inputs a bbox filter selects contribute nothing ✅

Of 23 intersecting records, **12 report a longitude range of exactly −180 to 180**: a polar pass, whose
bounding box the catalogue does not wrap, so it degenerates to the whole world and "intersects"
everything. Measured end to end on 8 fetched granules (`accumulate.py`): **2 were skipped entirely**
(no pixel above `qa_min`) and **2 more contributed 0 cells** to the region grid. So **4 of 8 selected
inputs added nothing at all**.

A composite job must therefore treat a zero-contribution input as **normal**, not as a failure, and
report how many actually contributed.

### A4. Accumulating is cheap, and memory is O(grid) as B.9 predicted ✅

`accumulate.py`, 8 granules, `--memory=4g`:

| Region grid | Cells | Time | Peak RSS |
|---|---:|---:|---:|
| 2–32°E, 57–72°N at 0.05° | 0.18 M | **2.4 s** | **136 MB** |
| the same at 0.01° | 4.50 M | 12.2 s | 386 MB |

Peak memory moved from 128 MB to 136 MB **as the eighth granule was added** — flat in the number of
inputs, which is the property the design depends on. Extrapolating A1's 23 granules at 0.05°: about
**7 s and under 200 MB**. This is a cheap job, not a heavy one.

### A5. A day does not fill the region, and a third of it is a single observation ✅

At both resolutions, one day of NRTI NO2 fills **about 80 %** of the Nordic box (80.29 % at 0.05 %,
79.34 % at 0.01°). Of the cells that are filled, **mean 1.68 granules contribute and the maximum is 3**,
and **32 % are the value of exactly one granule**.

So "daily mean" overstates what the product is: a fifth of the map is empty and a third of the rest is
not a mean of anything. That is a product-description problem more than a code one, but it decides
whether a daily composite is the right offering (see Q1).

### A6. Weighting the mean by the quality value is arithmetic that changes nothing ✅

B.9 proposed "mean of valid pixels weighted by `qa`". Measured both ways on the same 8 granules:

| | median \|difference\| | p99 \|difference\| | as a share of the p1–p99 spread |
|---|---:|---:|---:|
| all filled cells | 1.5e−13 | 1.37e−6 | **1.39 %** |
| cells with more than one granule | 8.8e−14 | 1.61e−6 | **1.64 %** |

`weights.py` explains why. Once the threshold has run, the surviving quality values barely vary:

| Threshold | Surviving weights | Composition |
|---|---|---|
| **`qa > 0.75`** (the NO2 default) | mean **0.996**, sd **0.020** | **95–96 % are exactly 1.00** |
| `qa > 0.5` | mean 0.82–0.88, sd 0.12–0.13 | two clusters: 0.74 (44–65 %) and 1.00 (32–53 %) |

**At the default threshold the weights are all but identical, so the weighted mean and the plain mean
are the same number.** The weighting costs a second warp per granule — it doubles the warp work — for
under 2 % of the dynamic range at the 99th percentile and nothing at all typically. The plan therefore
makes a **plain mean of valid pixels the default** and the weighted mean an option, which is a
deviation from B.9 and the reason is this measurement.

### A7. The existing job model cannot express this shape 📖

Every entry point — `process_sentinel_s1_safe`, `…_s2_safe`, `…_s5p_netcdf`, `…_s3_safe` — takes a
single `input_dataset: str`, and `_runtime.run_product_jobs` fans **one input out to several products**,
collecting one result per product. A composite is **several inputs collapsing to one product**. There
is no parameter that turns one into the other, so this needs a new entry point rather than an option on
an existing one. What it can reuse, unchanged: `_swath.warp_swath` and the grid policy, `_sen3`'s
reader, `_output`'s three-file writer, `scratch_dir`, `atomic_output`, `gdal_runtime`, and the
provenance helper.

### A8. Not verified

The per-granule cost on Sentinel-3 (only Sentinel-5P NO2 was measured); an OFFL-orbit composite
(B.9's "about two OFFL orbits" — the fetched day was NRTI only); whether a composite of more than
about 30 inputs behaves differently; the behaviour across the antimeridian, where the single-granule
grid policy already needs a polar projection; and anything about the vector products.

---

## B. The plan

### B.1 Scope

Composites of the **products that already exist**, on a caller-supplied region and grid. No new
science: a composite of `no2` is the mean of the same masked, gridded `no2` that a single-granule call
produces. First Sentinel-5P, because the audit measured it and the quality threshold gives a clean
definition of "valid"; then Sentinel-3, whose flag masks give the same thing by a different route.

### B.2 The payload

```json
{ "input_datasets": ["...nc", "...nc"],
  "region": {"bbox": [2.0, 57.0, 32.0, 72.0], "epsg": 4326, "res": 0.05},
  "products": {"no2": {"qa_min": 0.75}},
  "method": "mean" }
```

`region` is required and fixed by the caller — unlike a single-granule call, a composite cannot derive
its grid from its inputs, because the inputs are the thing being combined. `method` is `mean`
(default), `weighted_mean` (A6 says it is not worth defaulting to) or `max`/`min` for a later phase.

The result carries what a single-granule result does, plus: `inputs_used` and `inputs_skipped` with
reasons, `coverage` (the filled share of the region), `observations_per_cell` (mean and maximum), and a
second band of per-cell observation counts, because A5 shows a third of the map is one observation and
a user cannot otherwise tell.

### B.3 Phases (one PR each, against `main`)

- [ ] **Phase 1 — The job shape.** `process_sentinel_composite(...)` taking `input_datasets`, a fixed
  region, products and a method. Accumulate sum and count on the region grid, one input at a time,
  writing nothing per input. Idempotency key over the sorted inputs plus the options. Zero-contribution
  and stub inputs counted, not fatal. The three output files, plus the count band.
  *Q1–Q4 answered 2026-10-31, so: a caller-supplied window defaulting to one day, a plain mean by
  default, a required caller-supplied region, and flat one-fact-per-tag provenance.*
- [ ] **Phase 2 — Sentinel-5P products.** All eleven, reusing their single-granule tables so a
  composite cannot drift from the product it composites.
- [ ] **Phase 3 — Sentinel-3 products.** The same, with the flag masks in place of a quality
  threshold; the two-tier browse mask applies unchanged.
- [ ] **Phase 4 — Cost, limits and the queue.** Measure 23 and about 60 inputs, set a cap on
  `inputs × cells`, and decide what belongs on the interactive queue (A4 suggests a day over a region
  is interactive-cheap, which would be a change from the single-granule assumption).
- [ ] **Phase 5 — Service integration notes and close-out.** Write down what the service must do that
  pysent cannot (A2's query problem above all), and rename the plan file.

### B.4 What is deliberately out

Vector products (FRP, SRAL) — the other half of Q16 and its own plan. Anything that resamples in time
rather than space. Gap-filling the 20 % a day does not cover.

---

## C. Open questions for the owner

Asked just-in-time, before the phase that needs the answer. *Italics are the recommendation.*
**All seven were answered on 2026-10-31, each as recommended**, through the decision form at
`https://claude.ai/code/artifact/dcd2b639-c82b-4599-86cc-960e97fc3bc6` (the answers are stored with
the page, so they survive the session).

- **Q1 (before Phase 1). Is a daily composite the right product?** A5 measured that one day fills 80 %
  of a Nordic box and that 32 % of the filled cells are a single observation. *A composite over a
  caller-supplied window, defaulting to one day, with the coverage and the per-cell count reported and
  a count band in the file, so a thin composite is visibly thin.* The alternative is a multi-day
  default, which fills more and averages more but blurs a day's weather.
  **Answered 2026-10-31: a caller-supplied window, defaulting to one day.** So the window is an input,
  not a product definition, and the honesty about coverage has to be in the file.
- **Q2 (before Phase 1). Plain mean or quality-weighted?** *Plain mean as the default, weighted as an
  option.* A6 is the evidence: at the default threshold 96 % of weights are exactly 1.0 and the two
  methods differ by under 2 % of the dynamic range, for double the warp work. This is a deviation from
  B.9 and wants a yes.
  **Answered 2026-10-31: plain mean as the default, `weighted_mean` as an option.** The deviation from
  B.9 is accepted; A6 is the reason and stays in the file so a reviewer who expects weighting finds it.
- **Q3 (before Phase 1). Who chooses the grid?** *The caller must pass a region and resolution.* A
  composite has no single input to derive them from, and two composites of the same region on different
  grids cannot be compared. Should there be named standard regions, as the Synergy products have?
  **Answered 2026-10-31: the caller must pass a bounding box, an EPSG code and a resolution.** No named
  regions, and nothing derived from the inputs: an absent or partial `region` is an error, not a
  default. Comparability is the service's to maintain by sending the same region each time.
- **Q4 (before Phase 1). What goes in the provenance?** 23 input names is a long GDAL tag. *A count,
  the time window, the sorted-input digest used as the idempotency key, and the list in the result dict
  rather than in the file tags* — with the digest making the file traceable to an exact input set.
  **Answered 2026-10-31: count, time window and digest in the tags; the full list in the result.** With
  the owner's note: *"for the provenance we will design a specific JSON-LD string which will be
  connected to an ad-hoc PROV-O ontology and our own vocabulary — for now we keep the info we want,
  later on we will study the right format."* Phase 1 therefore records the **facts**, each as its own
  flat `PYSENT_COMPOSITE_*` tag with one meaning per tag, and does not invent a serialisation: a tag
  that holds one fact maps onto a PROV-O term later, whereas a composed string would have to be
  re-parsed. Nothing in Phase 1 may make that mapping harder — no packing two facts into one tag, no
  abbreviations, times in ISO 8601 UTC.
- **Q5 (Phase 4). Interactive or bulk?** A4 measured 2.4 s and 136 MB for 8 granules at 0.05°.
  *Interactive for a region at 0.05° or coarser, bulk otherwise.*
  **Answered 2026-10-31: interactive at 0.05° or coarser, bulk otherwise.** Phase 4 sets the
  `inputs × cells` cap that makes this safe.
- **Q6 (Phase 2). Which products get composites?** *All eleven Sentinel-5P, then Sentinel-3.* Some are
  more meaningful composited than others — a cloud fraction composite is a climatology, an aerosol
  index composite is defensible.
  **Answered 2026-10-31: all eleven Sentinel-5P, then Sentinel-3.** Breadth comes from reusing the
  single-granule product tables, so Phase 2 adds no per-product science.

**Q7, carried over from the Sentinel-3 plan** and not a composites question: the OLCI true-colour
acceptance gate. **Answered 2026-10-31: the substituted physical check is accepted and true colour is
catalogued like the rest.** The original gate — agreement with the Level-2 reflectance of the same
scene — was unmeetable because Level-2 is water-leaving reflectance after atmospheric correction
(r = −0.06, medians 127× apart). The substitute measured: 0.040–0.254 over open water at 665 nm, and
100 % of over-unity pixels carrying the `bright` flag. Recorded in `PLANNING_s5p_s3_pipelines.md`.

---

## D. Risks, ranked

1. **The service cannot select inputs with one catalogue query** (A2). Time filtering works, spatial
   filtering does not, and the two cannot be combined. If the service does not filter client-side it
   will pass either far too many inputs or none. *Mitigation: Phase 5's integration notes, and a job
   that tolerates useless inputs (A3).*
2. **Half the inputs may contribute nothing** (A3), so "it worked" and "it produced a map" are
   different questions. *Mitigation: coverage and contributing-input counts in the result; an empty
   composite raises `EmptySceneError` like any other empty product.*
3. **A composite looks more authoritative than it is** (A5): a fifth empty, a third single-observation.
   *Mitigation: the count band and the coverage figure, and Q1.*
4. **Doing the arithmetic twice** — a composite that re-implements masking or gridding will drift from
   the single-granule product. *Mitigation: Phase 2 and 3 reuse the existing product tables and the
   same warp, and a test asserts a one-input composite equals the single-granule product.*
5. **Memory if the region is large and fine** (A4): 4.5 Mcell cost 386 MB, so a 100 Mcell region would
   not fit a 4 GB worker. *Mitigation: Phase 4's cap on `inputs × cells`.*
6. **The antimeridian and the poles.** The single-granule policy switches to a polar projection there;
   a composite's region is caller-supplied and may straddle the seam. *Not measured — Phase 1 should
   refuse what it cannot do correctly rather than produce a smear.*

---

## Session log

- **2026-10-30 — audit session (no code).** Read the repository memory and the knowledge base; created
  the worktree from `main` @ `3a147c3`; measured the catalogue, the accumulation cost and the weighting
  question; wrote this plan. Evidence scripts are in `PLANNING/composites_audit/`. Eight real granules
  from 2026-10-09 were fetched to
  `/home/ubuntu/dev/services/pysent-testdata/composite-day/` (311 MB) and are re-fetchable from the
  URLs the catalogue returns. No library code changed.
  - **The two findings that changed the plan before it was written.** B.9's quality-weighted mean is
    not worth defaulting to (A6: 96 % of weights are exactly 1.0 at the default threshold), and the
    catalogue cannot answer the input-selection query at all (A2: title and bbox combine to zero).
  - **One correction to my own first reading.** I initially recorded that `BBOX` returned 0 records and
    that negative coordinates were rejected. Both were wrong: `BBOX` alone returns over a million, a
    negative *longitude* is fine, and what actually fails is the **combination** with a title filter and
    any value outside ±90 in the second argument. The lesson is in the method, not the finding — the
    first measurement was taken from one query shape and generalised.

- **2026-10-31 — Q1-Q7 answered, all as recommended.** Asked through a decision form published as an
  artifact (<https://claude.ai/code/artifact/dcd2b639-c82b-4599-86cc-960e97fc3bc6>) rather than in
  prose, one card per question carrying the measurement, the options and what each option commits the
  library to; the answers are stored with the page. Recorded in section C in place, with the date.
  Three consequences for the code:
  - **Q2 settles the deviation from B.9**: plain mean is the default, `weighted_mean` an option. The
    A6 measurement is the justification and stays in the plan for a reviewer who expects weighting.
  - **Q3 makes `region` a required argument** with no fallback: not derived from the inputs, no named
    presets. A missing or partial region is an error.
  - **Q4 shapes the provenance**, through the owner's note: a JSON-LD serialisation bound to an ad-hoc
    PROV-O ontology and MET's own vocabulary is planned, but not now. So Phase 1 records the facts as
    flat `PYSENT_COMPOSITE_*` tags, **one fact per tag**, ISO 8601 UTC times, no abbreviations and no
    composed strings — anything packed would have to be re-parsed when the mapping is designed.
- **2026-10-31 — Q7 closed in the Sentinel-3 plan.** The OLCI true-colour acceptance gate, left open at
  that plan's close-out, was accepted in the same form: the substituted physical check stands and true
  colour is catalogued. `PLANNING_s5p_s3_pipelines.md` is updated, so nothing is held back there.
