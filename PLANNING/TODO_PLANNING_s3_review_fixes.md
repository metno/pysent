# Sentinel-3 review-study findings: audit and fixes

**Goal:** fix what the FastAPI-mapserver Sentinel-3 review study (`seq-I9-s3-review-study`,
2026-10-31) found in pysent's Sentinel-3 code. Seven items: three plain bugs, three limitations and
one observation for the owner. **Item R1 blocks the study's full run**, by the owner's word, so it is
Phase 1 on its own.

| | |
|---|---|
| Branch / worktree | `feature/s3-review-fixes` → `../pysent.worktrees/s3-review-fixes` |
| Base | `main` @ `3a147c3` (2026-10-30) |
| Status | **Phases 1-4 done.** Phases 5 (R2) and 6 (R7) need the owner (Q1, Q4) and data this machine does not hold. |
| Audit evidence | [`s3_review_audit/`](s3_review_audit/) |
| Comes from | the FastAPI-mapserver study's report, relayed 2026-10-31; the closed `PLANNING_s5p_s3_pipelines.md` is the code's own plan |

**Constraints carried in from the report and from the earlier plans:**

- **The Sentinel-5P freeze holds.** Render the S5P cases on `main` and on the branch and show **zero
  differing pixel hashes** (`s3_review_audit/s5p_freeze_check.py`).
- **No Sentinel-3 default changes** — masks, ranges, ramps, gamma. They wait for the review study.
  A fix that necessarily changes pixels (R1) is not a default change, but the *style* chosen on top
  of it stays as it is and the new numbers go to the owner.
- **Truecolor's Phase 6 gate stays the owner's call** (accepted 2026-10-31 on the substituted
  physical check — which R1 now puts back in question, see R1 below).
- Every fix carries a **regression test that fails on `main`** for the intended reason.

## Kick-off prompt

```text
You are continuing the Sentinel-3 review-fix plan for the pysent library (github.com/metno/pysent).

Repository rules (mandatory):
1. Before anything else, read the repository memory:
   /home/ubuntu/.claude/projects/-home-ubuntu-dev-services-pysent/memory/MEMORY.md
   and every file it links, including the agent knowledge base it points to. Use that absolute
   path: a session started inside the worktree has a different default memory directory.
2. Work only in the plan's git worktree:
   /home/ubuntu/dev/services/pysent.worktrees/s3-review-fixes  (branch feature/s3-review-fixes)
   If it is missing, run this from /home/ubuntu/dev/services/pysent:
   git worktree add ../pysent.worktrees/s3-review-fixes feature/s3-review-fixes
   (add `-b` and `origin/main` if the branch does not exist either).
3. Read PLANNING/TODO_PLANNING_s3_review_fixes.md in full. Do the next unchecked phase, in order.
   Tick its checkboxes as you complete them and record every decision and deviation in the
   "Session log".
4. Verify the way CI does: the full suite in ubuntu:24.04 with apt GDAL 3.8 as a non-root user
   (docker run --rm -v $PWD:/src:ro pysent-ci:noble /run-tests.sh numba). Every fix needs a
   regression test that fails on main for the intended reason - say how many do.
5. Commit once per item and open one PR per phase against main. Do not merge without the user's
   go-ahead. Never add AI attribution to commits or PRs.
6. Do not change any Sentinel-3 default (mask, range, ramp, gamma) or any Sentinel-5P output. The
   S5P freeze is verified with s3_review_audit/s5p_freeze_check.py: zero differing pixel hashes.
7. Real products are on the mounted NBS archive and the data volume; see the memory file
   research-data-volume. Downloading is usually unnecessary.
8. At the end, update the memory directory, and tell the owner the merged commit so the study can
   re-pin scripts/review/run.sh (STUDY=s3-l2). Rename this file to PLANNING_s3_review_fixes.md
   when every phase is done.
```

---

## A. Audit

Each item is numbered as the report numbered it. **✅ verified** means I reproduced it here;
**📖 code reading** means I read the code and the claim follows from it but no test has run yet;
**❓ not reproduced** means I could not get at the data.

### R1. Truecolor divides each band by the wrong band's solar flux ✅ — blocks the study

Reproduced on `S3A_OL_1_EFR____20260609T091119…_NT_004` (the committed benchmark product), detector
column 1000, with `s3_review_audit/flux_orientation.py`:

| `GDAL_NETCDF_BOTTOMUP` | `lambda0` row 0 … row 20 |
|---|---|
| unset (the default) | **1016.0, 865.4, 761.6, 673.9, 510.3, 400.2** |
| `NO` | 400.2, 510.3, 673.9, 761.6, 865.4, 1016.0 |
| `YES` | 1016.0 … 400.2 (same as unset) |

So GDAL's netCDF driver hands back the 21 × 3700 table **bottom-up**: row 0 is band 21, not band 1.
`_olci_l1.solar_flux_per_pixel` takes `table[band - 1]`, so each band is divided by the flux of band
`22 - band`:

| Band | Nominal | Row taken | `lambda0` there | Flux used | Flux it should use | Error |
|---|---:|---:|---:|---:|---:|---:|
| Oa04 (blue) | 490 nm | 3 | 884.3 nm | 899.3 | 1888.6 | **2.10× too bright** |
| Oa06 (green) | 560 nm | 5 | 779.1 nm | 1152.2 | 1800.8 | **1.56× too bright** |
| Oa08 (red) | 665 nm | 7 | 764.7 nm | 1188.3 | 1505.4 | **1.27× too bright** |

The ratios match the study's to two decimals on a different granule and column, so this is the
arithmetic, not one odd file. **Every truecolor picture pysent has produced has a blue cast.**

Why Phase 6's gate did not catch it: the check was "reproduces the audit prototype, Oa08 p50 0.22",
and the prototype computed Oa08 the same wrong way. It validated one band against itself.

**What this does to the accepted Phase 6 decision:** the owner accepted the substituted physical
check on 2026-10-31 — open water 0.040/0.056/0.254 at 665 nm, and 100 % of over-unity pixels
`bright`. Those numbers were measured on biased reflectance, so **they have to be redone** before the
acceptance means anything, and the `0 – 0.6, gamma 0.6` style was chosen on biased pictures. Phase 1
redoes the numbers; the style stays untouched (it is a frozen default) and goes to the owner as Q5.

**Scope check — is anything else flipped?** The hazard is specific to an array whose row index means
something other than position. `lambda0` and `solar_flux` are the only 21 × 3700 band-indexed arrays
the code reads. The image bands, `detector_index`, the tie grids and the geolocation are all
position-indexed and all flip together, so they stay mutually consistent; a flip there would show as
an upside-down picture, which no reviewer has seen. Phase 1 asserts the consistency rather than
assuming it.

### R2. `sst` cannot read the archive's single-file WST layout ❓ not reproduced here

The claim: pre-June-2026 WST products are orbit-long dumps (duration 6059 s, baseline 003) holding
**one** GHRSST file and no `_DUAL_`/`_NADIR_` pair, so `find_member('*_DUAL_*.nc')` fails; 25 of 40
sampled frames over a year failed this way.

I cannot reproduce it from here: the mounted NBS archive holds **1 231 WST granules but only from
2026**, all baseline `004` and all 300-second frames (`_0179_`/`_0180_`), and nothing with a 6059
duration or an `_003` baseline. The four WST products on the data volume are all 2026 baseline 004
as well. So this needs either a fetch from the catalogue or the study's own copy. **It is real as
reported — the member pattern cannot match a product that has no `_DUAL_` member — but which view
that single file holds is a question about a file I have not seen, and the answer decides whether
pysent reads it or refuses it (Q1).**

### R3. SST NADIR is not selectable per request 📖 code reading

`normalize_sentinel_s3_product_map` takes `variable` from the request (`s3.py:587`) but
`candidates`, `geolocation` and `flag_member` come only from the product table
(`s3.py:588-589`, and `**defaults` above them). The swath path then resolves the variable from
`options["candidates"]` (`s3.py:1000`), so **a requested `variable` is accepted and silently
ignored** for every swath product. The study renders NADIR by patching the table in memory, which is
what a request option should let it do.

### R4. `browse_reject_flags` is accepted and never applied 📖 code reading

It is in `_PRODUCT_OPTIONS` (`s3.py:472`), so a request carrying it passes validation, but the
normalized product gets its value **only from `**defaults`** (`s3.py:582`) — nothing copies it from
the request, and unlike `mask_flags`/`reject_flags` it has no `"default"`/`"none"`/list parsing. So
`browse_reject_flags: []` silently keeps the default cloud margin, and a caller cannot turn the
browse mask off or change it. Confirmed by reading; Phase 2 adds the test that fails on `main`.

### R5. `restyle_sentinel_s3_product` mishandles the truecolor float 📖 code reading

`hide = BROWSE_HIDDEN_REASONS if band_count >= 2 else ()` and `reason_band = 2 if band_count >= 2`
(`s3.py:1666-1667`). A truecolor product has **three** bands of reflectance, so band 2 — green — is
read as the reason band and pixels are hidden wherever the rounded green reflectance equals a reason
code. It then writes a **single-band paletted** copy of a three-band picture, and there is no
`gamma` argument at all, so even a correct route could not reproduce the product's own rendering.

### R6. `on_empty="write"` still raises for an all-masked swath product 📖 code reading

`s3.py:1076` and `1195` skip the `EmptySceneError` when `on_empty == "write"`, but the warp two lines
later is given `minimum_valid=max(1, kept_count // 100)` (`s3.py:1125`, `1239`), which is **1** when
`kept_count` is 0, and `warp_swath` then refuses the empty warp. So `on_empty="write"` cannot write
an empty swath product; it swaps one exception for another. The guard exists to catch a *truncated*
warp, which is a different condition from "there was nothing to warp".

### R7. SLSTR outputs show dashed gaps across the swath ✅ — and SST is far worse than LST

`warp_swath` forces `resampling="near"` whenever a categorical companion band rides along
(`_swath.py:449`), and on the 1 km SLSTR grid some output cells then get no source pixel.

Measured with `s3_review_audit/slstr_gaps.py`, which warps the same frame twice — once with the
companion, so `near`, and once with the values alone and `average` — and counts the cells the
averaged warp fills that the nearest one does not:

| Frame | Product | Cells (average) | Gaps | Share | Longest run |
|---|---|---:|---:|---:|---:|
| `S3A_…LST…20261023T134501…_NT_005` | LST | 1,103,956 | 10,000 | **0.91 %** | 12 |
| `S3B_…LST…20260628T084307…_NT_005` | LST | 2,599,958 | 15,819 | **0.61 %** | 12 |
| `S3B_…LST…20190221T063656…_LR1_R_NT_004` | LST | 3,974 | 544 | 13.69 % | 4 |
| **`S3A_…WST…20261023T102002…_NT_004`** | **SST (dual)** | 106,399 | 34,982 | **32.88 %** | 9 |

**So this is two findings, not one.** For LST it is about one per cent of the cells, and the
complaint is cosmetic: the gaps are single cells and short runs, but they line up along the scan and
so read as dashes. **For dual-view SST a third of the map is missing** — the dual view is a narrower,
sparser swath, so at 1 km many output cells contain no source pixel centre at all. Pictures of a
400-cell crop, `near` against `average`, are in `/mnt/data/pysent/s3-review/`.

The remedy is a rendering choice and so the owner's (Q4). The 2019 reduced-resolution frame is in
the table for honesty, not for weight: at 3 974 cells it is too small to generalise from.

---

## B. Phases (one PR each, against `main`)

- [x] **Phase 1 — R1, the flux flip.** Pick the flux row by matching `lambda0` to the band's nominal
  wavelength, so the result is right whichever way the driver hands the table over. Assert the
  position-indexed arrays stay mutually consistent. Redo Phase 6's physical numbers on corrected
  reflectance and report them. Verify the S5P freeze. *Blocks the study: ship first.*
- [x] **Phase 2 — R4 and R6, the two unambiguous bugs.** Parse `browse_reject_flags` from the request
  like `reject_flags`; let `on_empty="write"` write an empty swath product.
- [x] **Phase 3 — R5, restyling an RGB product.** Route a three-band product to the RGB writers with
  a `gamma` read from the product's own tag, or refuse it with a message that says why. *Needs Q2.*
- [x] **Phase 4 — R3, the SST view.** A `view` request option for `sst`, setting variable candidates,
  geolocation and flag member together. *Needs Q3.*
- [ ] **Phase 5 — R2, the single-file WST layout.** A clear diagnosis always; reading it if the owner
  wants it. *Needs Q1 and a sample product.*
- [x] **Phase 6 — R7, the dashed gaps — measured.** The share is in R7 above and the pictures are
  rendered; the remedy itself is Q4 and waits for the owner. *No code change.*

---

## C. Open questions for the owner

*Italics are the recommendation.*

- **Q1 (Phase 5). The single-file WST layout: read it or refuse it clearly?** *Read it, if its
  attributes say which view it holds; refuse it with a precise message until then.* 25 of 40 sampled
  frames over a year are affected, so refusing permanently writes off a year of archive. I need one
  sample product to answer "which view is it" — may I fetch one from the catalogue (about half a
  gigabyte, onto the data volume)?
- **Q2 (Phase 3). Restyling truecolor: route it or refuse it?** *Route it* — a three-band product
  goes to `write_rgb_styled`/`write_rgb_thumbnail` with `gamma` taken from `PYSENT_STYLE_GAMMA`, so
  a restyle reproduces what the pipeline produced. Refusing would leave the review study unable to
  re-range a truecolor layer without reprocessing a 905 MB zip.
- **Q3 (Phase 4). A `view` option, or just fix the comments?** *The option.* `view: "dual" | "nadir"`
  for `sst`, setting the candidates, the geolocation and the flag member together. It is new
  behaviour rather than a changed default, and it removes the in-memory table patching the study
  does today.
- **Q4 (Phase 6). The dashed gaps.** *Measure first, then choose from pictures.* The candidates are a
  second warp for the values with `average` (one more warp per product, the reason band staying
  `near`), or a coarser output step. Rendering is yours.
- **Q5 (after Phase 1). The truecolor style, now that the reflectance changes.** *Leave `0 – 0.6`
  and `gamma 0.6` exactly as they are for now* — they are frozen Sentinel-3 defaults, and the review
  study is where a style gets chosen. But they were chosen on pictures that were 2.1× too blue, so
  they are very likely wrong; Phase 1 will give you the corrected medians to judge from.

---

## D. Risks

1. **R1 changes every truecolor pixel.** That is the point, but it means the Phase 6 acceptance and
   the chosen style both rest on measurements that no longer hold. *Mitigation: redo the numbers in
   Phase 1 and hand them to the owner with Q5.*
2. **A fix that reaches beyond Sentinel-3.** `_olci_l1` is OLCI-only, but `warp_swath` (R6, R7) is
   shared with Sentinel-5P. *Mitigation: the S5P pixel-hash check on every phase that touches
   `_swath.py`.*
3. **R2 cannot be tested against a real product here**, so a stub zip is the only test until one is
   fetched. *Mitigation: say so in the test's docstring rather than implying real coverage.*
4. **The study is pinned to a commit.** Shipping six phases as six merges would move the pin six
   times. *Mitigation: Phase 1 alone unblocks it; tell the owner that commit as soon as it merges.*

---

## Session log

- **2026-10-31 — audit.** Worktree `feature/s3-review-fixes` from `main` @ `3a147c3`. R1 reproduced
  on a real product and is exactly as reported (2.10×, 1.56×, 1.27×); R3, R4, R5 and R6 confirmed by
  reading, each with the line numbers above; R2 could not be reproduced because the mounted archive
  holds only 2026 baseline-004 WST frames, and R7 is not yet measured. No code changed.

- **2026-10-31 — Phase 1 (R1, the flux flip).** `flux_row_for_band` matches the table's own
  `lambda0` against the band's nominal wavelength, so the lookup is right whichever way round the
  driver hands the table over, and a table that cannot be matched within 12 nm raises rather than
  guessing. Evidence: `s3_review_audit/run.sh flux` and `… numbers`.
  - **Three new tests, three fail on `main`** — two on the missing helper, and one through
    `solar_flux_per_pixel` itself, which on `main` returns band 18's flux (1018 in the stub's
    scheme) where band 4's was asked for. That last one is the real regression test: it goes
    through the function the pipeline calls, on a stub SEN3 zip whose rows are in file order.
  - **The physical numbers, redone** on the benchmark frame at decimate 8, open water (not land, not
    bright):

    | | water p01/p50/p99 | share above 1.0 |
    |---|---|---:|
    | before, red (665 nm) | 0.040 / 0.057 / 0.269 | 2.75 % |
    | before, green (560 nm) | 0.087 / 0.123 / 0.353 | 7.80 % |
    | before, blue (490 nm) | 0.180 / 0.275 / 0.609 | **23.69 %** |
    | **after, red** | **0.032 / 0.045 / 0.211** | **0.33 %** |
    | **after, green** | **0.056 / 0.079 / 0.226** | **0.14 %** |
    | **after, blue** | **0.086 / 0.131 / 0.289** | **0.25 %** |

    Whole-frame medians move from 0.221 / 0.294 / 0.463 to 0.174 / 0.188 / 0.220 (R/G/B). Still
    bluer than red, which is what Rayleigh scattering does to a top-of-atmosphere spectrum, but no
    longer twice as blue. **Every over-unity pixel is still flagged `bright`**, so the substituted
    Phase 6 check the owner accepted on 2026-10-31 holds on the corrected numbers — and holds more
    convincingly, since the over-unity share fell from 23.7 % to 0.25 % in blue.
  - **The S5P freeze holds**: four cases (committed NO2 window, archive NO2, archive CO, archive
    O3), eight rasters, **zero differing pixel hashes** against `main`, hashing pixels separately
    from tags because the provenance tags carry a version string. `run.sh freeze` reproduces it.
  - Full suite in the CI container: **421 passed, 13 skipped**.
  - **No default changed.** The `0 – 0.6` range and `gamma 0.6` stay exactly as they are, and go to
    the owner as Q5 with the numbers above — they were chosen on pictures 2.1× too blue.

- **2026-10-31 — Phase 2 (R4 and R6).**
  - **R4**: `browse_reject_flags` is now parsed from the request exactly like `reject_flags`
    (`"default"` / `"none"` / a list), and asking for it makes the flag names strict, so a typo is
    an error rather than a silently empty mask. Three tests, all three fail on `main`, where
    `browse_reject_flags: []` comes back as the default cloud margin.
  - **R6**: the empty-warp guard is now skipped when nothing was kept. The guard asks "did the warp
    lose the data?" and a frame whose mask kept no pixel has none to lose; left at 1 it turned
    `on_empty="write"` into `ValueError: the warp produced 0 valid pixels` — which is the exact
    message the regression test sees on `main`. Both swath call sites changed; the grid product's
    path never had the problem.
  - Full suite **425 passed, 13 skipped**. The S5P freeze is untouched by construction here: the
    changes are in `s3.py`, which no Sentinel-5P path imports.

- **2026-10-31 — Phases 3 and 4 (R5 and R3).** Both items offered a choice; both were taken the way
  the study's report preferred, because neither changes a default output and both remove something
  the study works around today. Said here rather than only in a PR, so the owner can object.
  - **R5**: a three-band picture product now goes to `write_rgb_styled` / `write_rgb_thumbnail`
    through `_restyle_rgb`, with a `gamma` argument; `colormap` and `log_scale` are **refused** for
    it rather than ignored, since a caller passing them has misunderstood what they are restyling,
    and `gamma` is refused for a ramped product for the same reason. The gamma comes from the
    product's own `PYSENT_STYLE_GAMMA`, falling back to the product table, so a restyle that changes
    only the range reproduces the pipeline's rendering. **A new tag:** the true-colour float now
    records `PYSENT_STYLE_GAMMA` — a provenance addition, no pixel changes.
  - **How the kind is decided, and a mistake on the way.** I first keyed the reason band off band
    2's description being `reject_reason`, which broke `test_restyling_still_hides_what_the_browse_image_hides`:
    only the **grid** path names that band, while the swath path leaves the description empty. The
    discrimination is now "is this a picture?" — the `PYSENT_PRODUCT` tag's `kind`, or three bands
    that all say `reflectance` — and the reason band stays the old count test, which is safe once
    pictures are routed away. Four tests, all four fail on `main`; the headline one fails there with
    `assert 1 == 4` on the styled band count, which is the defect itself.
  - **R3**: `view` is a request option for `sst` (`"dual"` default, `"nadir"`), and the table now
    carries a `views` entry so one request sets the variable's member, the geolocation and the flag
    member **together** — the reason overriding `variable` alone could never select a view. A view
    on a product that has none, or an unknown view, is refused. Separately, a requested `variable`
    now reaches the swath resolver: it is substituted into the table's candidate patterns, so the
    caller names a variable and the member pattern stays the table's. Three tests, all three fail on
    `main`.
  - Full suite **432 passed, 13 skipped** with numba, **428 passed, 17 skipped** without. The S5P
    freeze re-checked after all four phases: **zero differing pixel hashes**.

- **2026-10-31 — R7 measured (no code change).** `slstr_gaps.py` warps a frame both ways and counts
  what the nearest-neighbour warp loses. **LST loses 0.6-0.9 %, dual-view SST loses 32.9 %.** The
  LST case is cosmetic - single cells and runs of up to 12, lined up along the scan, which is why
  they read as dashes. The SST case is not cosmetic: a third of the map is absent, because the dual
  view is a narrower and sparser swath and at 1 km many cells hold no source pixel centre. Pictures
  of a 400-cell crop are in `/mnt/data/pysent/s3-review/` (`sst_crop_near.png` against
  `sst_crop_average.png`), and the dashes are plain in the first and gone in the second. The remedy
  is a rendering decision, so it stays with the owner as Q4 - but the SST number raises the question
  from cosmetic to substantive, and the plan says so.
