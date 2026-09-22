# Sentinel-1 radiometry: dB stretch and speckle filtering

**Goal:** make the S1 quicklook read the way SAR is meant to be read. Two items have sat in
"known tuning work" without measurements behind them: the **linear amplitude stretch** and the
absence of any **speckle filtering**. Measure both on a real scene, let the user choose the
defaults, then ship them as options.

| | |
|---|---|
| Branch / worktree | `feature/s1-radiometry` → `../pysent.worktrees/s1-radiometry` |
| Base | `main` @ `7283ba8` (2026-09-22, end of the bulk-processing plan) |
| Status | Measurements done (phase A); all three questions answered. Phase 1 done. Phases 2–3 not started. |
| Evidence | `s1_radiometry_audit/` (to be committed with phase 1) |

## Kick-off prompt

Copy this into a new agent session. It can run from any directory.

```text
You are continuing the Sentinel-1 radiometry plan for the pysent library (github.com/metno/pysent).

Repository rules (mandatory):
1. Before anything else, read the repository memory:
   /home/ubuntu/.claude/projects/-home-ubuntu-dev-services-pysent/memory/MEMORY.md
   and every file it links. Use that absolute path: a session started inside the
   worktree has a different default memory directory.
2. Work only in the plan's git worktree:
   /home/ubuntu/dev/services/pysent.worktrees/s1-radiometry  (branch feature/s1-radiometry)
   If it is missing, run this from /home/ubuntu/dev/services/pysent:
   git worktree add ../pysent.worktrees/s1-radiometry feature/s1-radiometry
   (add `-b` and `main` if the branch does not exist either). Rebase on origin/main before starting.
3. Read PLANNING/TODO_PLANNING_s1_radiometry.md in full. Do the next unchecked phase, in order.
   Tick its checkboxes in the file as you complete them, and record every decision and
   deviation in the "Session log" section.
4. Verify the way CI does: run the full test suite in ubuntu:24.04 with apt GDAL 3.8
   (see .github/workflows/ci.yml). Every change needs a test; radiometry changes need a
   before/after measurement on the real benchmark scene, not just a synthetic one.
5. Commit once per phase and open one PR per phase against main. Do not merge without the
   user's go-ahead. Never add Claude attribution (no Co-Authored-By trailer, no
   "Generated with Claude Code" footer) to commits or PRs.
6. Do not change what the images look like without asking the user first. The S2 stretch
   decisions from the bulk-processing plan (percentile 0.5-99.5 + gamma 0.7 into [1,255],
   0 = NoData only) are settled - do not revisit them.
7. At the end of the session, update the memory directory from rule 1 with what this session
   learned: plan status, decisions, gotchas. When every phase is done, rename the file to
   PLANNING/PLANNING_s1_radiometry.md.
```

---

## A. Measurements

One real scene: `S1D_IW_GRDH_1SDV_20260810T052300…`, VV and VH, warped to EPSG:32661 at the
shipped 40 m grid (36.8 Mpx, 83 % valid). GDAL 3.8.4. The visual comparison the user reviewed
is at `https://claude.ai/artifact/9b4G61dm1PiH1usC8CYrsJ` (whole scene plus three
full-resolution crops: calm water, structured coast, bright targets).

### A.1 Linear versus dB (VV, whole scene)

| Option | Mean | σ | Entropy (bits) | Clipped low/high | Levels used |
|---|---:|---:|---:|---:|---:|
| linear 2–98 (today) | 116.2 | 68.1 | 7.82 | 2.5 % / 2.1 % | 256 |
| dB 2–98 | 154.7 | 68.8 | 7.73 | 2.2 % / 2.2 % | 256 |
| dB 1–99 | 152.7 | 63.9 | 7.72 | 1.1 % / 1.1 % | 256 |
| dB 5–95 | 155.7 | 77.1 | 7.57 | 5.3 % / 5.4 % | 256 |

**The headline claim in the docs is not supported by the global statistics.** "SAR spans orders
of magnitude, so a linear clip crushes the scene into a narrow band" is true of *raw* amplitude,
but the shipped stretch already clips at the 2nd/98th percentile, and after that clip the
distribution is not especially skewed: entropy is marginally *higher* for linear (7.82 vs 7.73),
and both use all 256 levels. What dB actually changes is **where the range is spent** - it
compresses the bright end and expands the dark end, which is what makes water, shadow and smooth
ground legible. That is a perceptual argument, so it was settled visually, not by entropy.

VH behaves the same way (linear 122.8/7.80, dB 2–98 163.3/7.62).

### A.2 Speckle

| | VV | VH |
|---|---:|---:|
| ENL, raw | 16.0 | 19.9 |
| ENL, Lee 5×5 | 174.5 | 313.5 |
| Cost | 0.235 s/Mpx | 0.240 s/Mpx |

ENL is measured over the most homogeneous decile of 32×32 tiles. A 5×5 Lee filter on intensity
(speckle variance 1/4.4 for IW GRDH) all but removes speckle, at **about 8 s per polarisation**
for a 37 Mpx scene - roughly 40 % on top of a 19 s two-polarisation scene.

Implemented with an integral image (`cumsum`), so **numpy only**: pysent depends on numpy and
rasterio, and a filter that needed scipy could not ship.

**Caveat on resolution.** At the 160 m quicklook grid the warp has already averaged 4×4 native
pixels, so raw ENL is 61/143 and a filter buys much less. Speckle filtering matters at 40 m and
finer; the quicklook preset barely needs it.

### A.3 What already exists

`pysent.qa.quality.stretch_s1_grayscale_db` is the reference dB implementation and was used for
these measurements. It is in the QA extra, not in the processing path, and its signature matches
`stretch_sentinel_s1_grayscale`.

---

## Plan

### Phase 1: dB stretch as an option (PR 1)
- [x] `stretch_method` on the S1 path: `"linear"` (today) or `"db"`, defaulting to whichever the
      user picks in open question 1. Fold the QA reference implementation into `pysent.s1` and
      have `pysent.qa` re-export it, so there is one implementation.
- [x] Record the unit in the job record (`stretch.unit = "amplitude" | "dB"`), so a consumer can
      tell what `p_low`/`p_high` mean.
- [x] The numba fast path must cover both, or refuse the dB path cleanly rather than silently
      producing the linear one.
- [x] Tests: both methods on synthetic data and on the committed `tests/data` fixtures; the dB
      path leaves no valid pixel at 0 (alpha stays the NoData channel); stats carry the unit.
- [x] Measure before/after on the benchmark scene; put the numbers in the PR.

### Phase 2: speckle filter as an option (PR 2)
- [ ] `speckle_filter="lee"` (default off), with `speckle_window` (default 5) and the number of
      looks derived from the product type where possible, else configurable.
- [ ] numpy-only implementation (integral image), applied to the warped amplitude before the
      stretch, respecting the valid mask so fill never bleeds into the image.
- [ ] Tests: ENL rises on a synthetic speckled field; edges are preserved better than a plain box
      mean; the valid mask is untouched; a window of 1 is a no-op.
- [ ] Measure the cost per polarisation and per scene, and say so in `examples/README.md`, since
      it changes the sizing arithmetic.

### Phase 3: docs and close-out (PR 3)
- [ ] README and `docs/tuning-and-roadmap.md`: replace the two "known tuning work" bullets with
      what shipped and what it costs, including the correction from A.1 (the linear stretch is
      not as bad as the docs claimed; dB wins on where the range is spent, not on entropy).
- [ ] A notebook cell comparing the options on the committed fixtures, so the choice stays visible.
- [ ] Update memory. Rename this file to `PLANNING_s1_radiometry.md`.

## Open questions for the user
Recommendations in *italics*. Both were put to the user with the visual comparison above.
1. **S1 stretch default:** **answered 2026-09-22 - dB at 1–99**, chosen by the user from the
   visual comparison. `stretch_method="linear"` with `(2.0, 98.0)` reproduces the old rendering.
2. **Speckle filter:** **answered 2026-09-22 - ship it, default off.**
3. **Downstream consumers:** **answered 2026-09-22 - display only**, so changing the curve is
   safe. The job record still names the unit.

## Session log
- **2026-09-22, measurement session.** Worktree and branch created from `main` @ `7283ba8`.
  Measured linear vs dB (four variants) and a 5×5 Lee filter on the real scene at both 40 m and
  160 m, VV and VH; built the visual comparison artifact and put the two decisions to the user.
  Found that the documented justification for dB does not hold as stated (see A.1) and corrected
  it here rather than repeating it. No library code changed yet.
- **2026-09-22, phase 1: the dB stretch (PR #11).** `stretch_sentinel_s1_grayscale` gained
  `method="db"|"linear"`, defaulting to dB, and `S1_STRETCH_PERCENTILES` moved to `(1.0, 99.0)`.
  The numba kernel takes a `to_db` flag rather than refusing the fast path, and matches numpy to
  within one grey level as before. The stats dict now names its `unit`
  (`"dB"`/`"amplitude"`), since `p_low`/`p_high` are no longer comparable across methods.
  `pysent.qa.quality.stretch_s1_grayscale_db` now delegates to the library instead of keeping a
  second implementation, with its own 2-98 default so notebook comparisons do not shift.
  Verified on the real scene: VV mean 116.2 → 152.7, VH 122.7 → 161.8, same runtime (20.1 s vs
  19.7 s), and `stretch_method="linear"` with `(2.0, 98.0)` reproduces the old output exactly.
  The overstated claim in `docs/tuning-and-roadmap.md` was corrected rather than repeated.
