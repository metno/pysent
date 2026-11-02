# Atmospheric correction for OLCI: audit and phases

**Goal.** Turn pysent's top-of-atmosphere OLCI reflectance into **surface reflectance**, all three
tiers: gaseous absorption, Rayleigh scattering, and aerosol. Judged against ESA's own corrected
products for the same acquisition, not against our own expectations.

| | |
|---|---|
| Branch / worktree | `feature/atmospheric-correction` / `../pysent.worktrees/atmospheric-correction` |
| Base | `main` @ `da8f98d` |
| Status | **audit complete, phases not started** |
| Audit evidence | `PLANNING/atmospheric_correction_audit/run.sh` |
| Products | the mounted NBS archive only - nothing downloaded (see memory `research-data-volume`) |

**What exists today.** Nothing. `_olci_l1.toa_reflectance` computes `rho = pi L / (F0 cos theta)` and
the `truecolor` product renders it. Every other Sentinel-3 product pysent routes is an ESA Level-2
and arrives already corrected, so this plan touches OLCI Level-1 only.

---

## Kick-off prompt

> Work on pysent's atmospheric correction plan.
>
> 1. **Read memory first**, at the absolute path
>    `/home/ubuntu/.claude/projects/-home-ubuntu-dev-services-pysent/memory/`: `MEMORY.md`, then the
>    files it points to, then the knowledge base `AGENT_MEMORY/` on the local-only `agent-memory`
>    branch. At the **end** of the session, update memory with what the session found - bugs,
>    decisions, gotchas, plan status.
> 2. **Work only in the plan's worktree**, `../pysent.worktrees/atmospheric-correction` on
>    `feature/atmospheric-correction`. Rebase on `origin/main` at the start of each session.
> 3. Read `PLANNING/TODO_PLANNING_atmospheric_correction.md` in full. Do the next unchecked phase, in
>    order. Tick its box in the file as part of the phase's commit.
> 4. **Audit before you build**: reproduce a finding before you fix it, and mark every claim as
>    reproduced or merely read. Every finding gets a line in the evidence script
>    `PLANNING/atmospheric_correction_audit/run.sh` so someone else can re-run it.
> 5. **Verify the way CI does** - the `pysent-ci:noble` container, not the dev environment. That
>    container cannot see `/lustre`, so a script that reads the archive runs on the host.
> 6. **Every fix needs a regression test that fails on `main`** for the intended reason, and say how
>    many of the new tests do.
> 7. **Never change a user-visible default without asking.** Measure the alternatives, show the
>    comparison - pictures where the choice is visual - and let the owner choose.
> 8. **One commit and one PR per phase, against `main`. Never merge without the owner's go-ahead.**
> 9. Ask the open questions in section C just-in-time, with a recommendation, before the phase that
>    needs the answer.
> 10. No Claude attribution in commits or PRs.
>
> This correction is physics, so one more rule: **no coefficient without a source**, and no
> measurement quoted that `run.sh` cannot reproduce.

---

## A. Audit

Every finding below was measured on the mounted archive with
`PLANNING/atmospheric_correction_audit/run.sh`, on 2026-11-02.

### A1. The archive holds 58 matched validation sets ✅ verified

`run.sh pairs`. For S3A over October and November 2026 there are 92 OLCI acquisitions, and **58 of
them have all three of** `OL_1_EFR` (our input), `OL_2_WFR` (water-leaving reflectance) and
`SY_2_SYN` (surface directional reflectance), matched on sensing start, stop, cycle and orbit. A
further 32 `SY_2_AOD` products cover the same period.

**All three are on the same 4865-column instrument grid for the same frame** - L1 `Oa04_radiance`,
WFR `Oa04_reflectance` and SYN `SDR_Oa04` are each 4865 x 1908 on the 063201 frame - so the
comparison is **per pixel with no warping in between**. That is the single most valuable fact in
this audit: it means every claim about how well a correction works is a direct pixel-to-pixel
measurement against ESA's own answer, with no resampling error mixed in.

The variable names do not follow the member names: Synergy's `Syn_Oa04_reflectance.nc` holds
`SDR_Oa04`, not `Syn_Oa04_reflectance`.

### A2. Every input a correction needs is already in the Level-1 product ✅ verified

| Member | Variables | Grid |
|---|---|---|
| `tie_meteo.nc` | `sea_level_pressure`, `total_ozone`, `total_columnar_water_vapour`, `humidity`, `atmospheric_temperature_profile`, `horizontal_wind` | 4091 x 77 tie |
| `tie_geometries.nc` | `SZA`, `OZA`, `SAA`, `OAA` | 4091 x 77 tie |
| `geo_coordinates.nc` | `altitude` (per pixel), `latitude`, `longitude` | full |

The tie grids are the same shape the existing `_olci_l1.solar_zenith` already expands (one row per
image row, interpolated across track), so the plumbing is a generalisation of a function that
exists, not new machinery. Nothing has to be fetched or assumed: pressure, ozone and water vapour
are measured per frame.

### A3. Over land, Rayleigh alone is most of the answer ✅ verified

`run.sh budget land` - frame `20261026T072930`, SZA median 70.9 deg, **1 181 928 clear land pixels
and only 1.4 % bright**, so the statistics are not a cloud artefact. Single-scattering Rayleigh,
subtracted from TOA, against `SY_2_SYN` surface directional reflectance:

| band | TOA | rho_R | TOA - rho_R | SYN SDR | residual | r(TOA, SDR) | r(corrected, SDR) |
|---|---:|---:|---:|---:|---:|---:|---:|
| Oa08 665 nm | 0.0854 | 0.0305 | 0.0543 | 0.0609 | **-0.0066** | 0.854 | **0.906** |
| Oa06 560 nm | 0.1013 | 0.0613 | 0.0417 | 0.0697 | **-0.0280** | 0.814 | **0.876** |
| Oa04 490 nm | 0.1339 | 0.1058 | 0.0349 | 0.0380 | **-0.0031** | 0.353 | **0.555** |

Two things matter here. The **median lands within 0.003 to 0.007 of ESA's own surface reflectance at
red and blue** from the crudest possible Rayleigh treatment. And the **correlation with the true
surface rises at every band** - most of all at blue, 0.353 to 0.555, which is exactly where the
atmosphere dominates. A correction that only shifted a level would not move a correlation; this one
makes the product track the surface.

560 nm over-corrects by 0.028. That is not noise, it is a missing term - see A8.

### A4. Over water, Rayleigh is less than half the answer ✅ verified

`run.sh budget water` - frame `20261025T111740`, SZA median 70.6 deg, 595 284 clear water pixels.

| band | TOA | rho_R | TOA - rho_R | WFR water-leaving | residual |
|---|---:|---:|---:|---:|---:|
| Oa08 665 nm | 0.0748 | 0.0293 | 0.0392 | 0.0002 | **0.0391** |
| Oa06 560 nm | 0.1112 | 0.0589 | 0.0435 | 0.0016 | **0.0420** |
| Oa04 490 nm | 0.1764 | 0.1016 | 0.0672 | 0.0037 | **0.0634** |

Rayleigh removes 39 % to 58 % of what has to come off. **The residual is 17 to 195 times the
water-leaving signal itself** (0.0634 against 0.0037 at blue). Over water the quantity of interest
is a rounding error on the atmosphere, which is why ESA runs a full aerosol inversion for WFR and
why a Rayleigh-only product must never be labelled a water product.

Correlations over water are 0.08 to 0.27 and barely move. That is expected and not a failure: with a
signal of 0.0002 at 665 nm there is almost nothing to correlate with, so over water the metric has
to be the residual, not the correlation.

### A5. The archive's sun is low, and single scattering breaks there ✅ verified

`run.sh probe 10`. Ten matched acquisitions, **solar zenith median 70.6 to 78.7 degrees, minimum
63.5** across all of them. This is the Norwegian ground segment in late autumn: there is no high-sun
frame in the holding, and the air mass is 3 to 6.

`run.sh budget lowsun` - frame `20261101T063201`, SZA median **79.6** deg:

| band | TOA | rho_R (single scatter) | TOA - rho_R | SYN SDR | residual |
|---|---:|---:|---:|---:|---:|
| Oa08 665 nm | 0.1565 | 0.0594 | 0.0880 | 0.1534 | -0.0654 |
| Oa06 560 nm | 0.1896 | 0.1151 | 0.0529 | 0.2225 | -0.1696 |
| Oa04 490 nm | 0.2746 | 0.2017 | 0.0495 | 0.2194 | -0.1699 |

At 79.6 degrees single scattering claims **0.20 of a 0.27 TOA reflectance at blue** and the result
misses ESA's answer by 0.17 in the wrong direction. Push a little further and a naive correction
produces negative reflectance. So the single-scattering formula used in this audit is a measuring
stick, **not a candidate implementation**, and the correction needs a high-air-mass treatment plus a
documented ceiling (Q3). That the whole archive lives in this regime makes it the design case rather
than an edge case.

### A6. The aerosol inputs exist in the archive too ✅ verified

`SY_2_AOD` (32 products for the period) holds, on a 4026 x 324 orbit-long grid with its own
`latitude`/`longitude` and geometry:

- `AOD_440`, `AOD_550`, `AOD_670`, `AOD_865`, `AOD_1600`, `AOD_2250`, each with an `_uncertainty`
- `ANG550_865` (Angstrom exponent), `FM_AOD550` (fine-mode), `AAOD550` (absorption)
- `SSA_440` ... `SSA_2250` (single-scattering albedo), `Surface_reflectance_440` ... `_2250`
- `aod_quality_flags`, `cloud_fraction_nadir`

440, 550 and 670 bracket the three bands true colour renders. So the aerosol tier can be built
**with measured aerosol loading and a measured phase-function shape** rather than a guessed model -
which turns the hardest tier into interpolation plus arithmetic, and leaves retrieval (Phase 4) as
the only genuinely open piece.

### A7. numpy and GDAL, and nothing else ✅ verified

`run.sh tooling`: numpy 1.26.4 and GDAL 3.8.4 present; **Py6S, smac, scipy and netCDF4 all absent,
and the host has no pip**. Consistent with the repository's apt-only geo stack and with CI. So the
correction must be numpy arithmetic plus coefficient tables committed to the repository, and there is
no radiative-transfer code to call. Q1 asks whether that constraint stands.

No scipy also means no KD-tree for putting the AOD grid onto the OLCI frame. That is already
solved in-repo: `_swath.warp_swath` warps a geolocated array onto a regular grid, and a regular grid
can be sampled per pixel with plain index arithmetic.

### A8. The inversion is not a subtraction, and that is the 560 nm gap 📖 literature, not yet measured

The audit script subtracts a path reflectance. The actual first-order inversion is

    rho_TOA / T_gas  =  rho_path  +  T_down * T_up * rho_surf / (1 - S * rho_surf)

with `T_gas` the two-way gaseous transmittance, `T_down`/`T_up` the atmosphere's diffuse-plus-direct
transmittance on each leg, and `S` the atmosphere's spherical albedo seen from below. Three terms
the audit left out, all of which push the answer **up**:

- no gas correction, so TOA is biased low before anything else happens;
- no division by `T_down * T_up`, which at these air masses is roughly 0.85 to 0.9, i.e. 10 to 15 %;
- no spherical-albedo coupling, a per-cent-level term over bright surfaces.

A 10 to 15 % lift on the 0.042 corrected value at 560 nm is 0.004 to 0.006 - the right sign, the
right order, not the whole 0.028. **So the remainder at 560 nm is a real finding to chase in Phase
2, not something the algebra will absorb**, and the honest statement today is that the gap is
partly explained. Marked literature because no line of this has been implemented or measured here.

---

## B. Phases (one PR each, against `main`)

The sequence is chosen so that **each phase is validated against an independent reference before the
next one adds error on top**, and so that the least determined piece comes last, by which time its
error budget is measurable. It is not ordered by difficulty.

- [ ] **Phase 1 - the radiative-transfer frame, and the gas term.** Generalise tie-point reading to
  any `tie_geometries`/`tie_meteo` field (`solar_zenith` becomes one caller of it). Implement the
  inversion of A8 as the shape of the whole correction, with the Rayleigh and aerosol terms still
  zero, so the algebra is in place and testable before any physics is tuned. Then the gas term:
  ozone (Chappuis, which is why it matters at 560 and 665) and water vapour, from the product's own
  `total_ozone` and `total_columnar_water_vapour`, with the unit conversion audited - the product
  stores roughly 0.0062 in its own units and a correction needs Dobson units or kg m-2, and getting
  that wrong is a silent factor of hundreds. Coefficients committed as a table with a cited source
  per row (Q1). *Acceptance: the gas-only correction moves 560 and 665 nm in the documented
  direction by the documented amount, and a round trip with zero optical depths reproduces TOA
  exactly.*
- [ ] **Phase 2 - Rayleigh, properly.** Multiple scattering, the transmittance division, the
  spherical-albedo coupling, pressure from `sea_level_pressure` scaled by per-pixel `altitude`.
  *Acceptance: on the A3 land frame, the residual against `SY_2_SYN` is under 0.01 at all three
  bands - including 560 nm, where single scattering misses by 0.028 - and the correlation is no worse
  than A3's. On the A5 low-sun frame the result is not negative anywhere the quality band calls
  usable.* This is the phase that makes true colour defensible, and the phase that re-opens the
  frozen style question (Q6).
- [ ] **Phase 3 - aerosol from the AOD product.** Read `SY_2_AOD` (A6), put it on the frame, and
  build the aerosol path reflectance and transmittance from measured `AOD_550`, `ANG550_865` and
  `SSA_*` rather than a chosen model. *Acceptance: over land the residual against `SY_2_SYN` is
  smaller than Phase 2's at every band; over water the residual against `OL_2_WFR` falls from A4's
  0.039 to 0.063 by a stated factor. Both measured on the same frames, so the comparison is like for
  like.* Doing this **before** retrieval is the point of the sequence: with ESA's own aerosol inputs
  the correction's error is isolated, and if it cannot reach WFR here, no retrieval will rescue it.
- [ ] **Phase 4 - aerosol retrieval, for when no AOD product is at hand.** Dark-target over water
  (the near-infrared black-pixel assumption at 865 nm), dark-dense-vegetation over land, and a
  climatological fallback. *Acceptance: retrieved AOD against the `SY_2_AOD` product's own
  `AOD_550` on the same acquisition - a direct, honest metric - and the end-to-end surface
  reflectance no worse than Phase 3's by a stated margin.*
- [ ] **Phase 5 - the product surface.** A `surface_reflectance` product and an
  `atmospheric_correction` option on `truecolor` (Q2), defaulting off until the owner says
  otherwise. Provenance tags recording the tier, the aerosol source and every optical depth used; a
  per-pixel quality band carrying the high-air-mass ceiling (Q3); and an uncertainty derived from
  `AOD_*_uncertainty`. *Acceptance: the provenance is enough to reproduce the pixel, and the frozen
  defaults are untouched.*
- [ ] **Phase 6 - validation across the holding, not one frame.** Run the accepted correction over a
  sample of A1's 58 triples, both platforms, and report the residual distribution by band, by
  land/water and by solar zenith. *Acceptance: a table the next reviewer can argue with, and a
  stated regime where we refuse.*

---

## C. Open questions for the owner

*Italics are the recommendation. **Q1 and Q2 are needed before Phase 1**; the rest are flagged with
the phase that needs them.*

- **Q1 (Phase 1). Coefficients and dependencies.** *Stay numpy-only: every coefficient committed as
  a small table in the repository, each row carrying its source, and no new runtime dependency.* The
  host has no pip, no scipy and no radiative-transfer package (A7), and the repository's geo stack is
  deliberately apt-only. The alternative is to add `Py6S` or a generated lookup table as a
  dependency, which buys accepted physics at the cost of the deployment story. A committed table is
  auditable and diffable; a dependency is neither.
- **Q2 (Phase 1). Where does the correction live?** *Both, with the option defaulting off:* a new
  `surface_reflectance` product for the physical quantity, and an `atmospheric_correction:
  "none" | "gas" | "rayleigh" | "full"` option on `truecolor` for the picture. The product is what a
  researcher wants; the option is what makes the picture right. Defaulting off keeps the frozen
  Sentinel-3 defaults frozen.
- **Q3 (Phase 2). The high-air-mass ceiling.** *Produce, with a per-pixel quality band, and refuse
  above a documented solar zenith.* The whole archive is at 63 to 79 degrees (A5), so refusing the
  regime would refuse everything; but a correction that can return negative reflectance has to say
  where it stopped being meaningful. The threshold should be measured in Phase 2, not picked now.
- **Q4 (Phase 3). The aerosol source, and reading a second product.** *An explicit `aerosol` option
  naming the source - the `SY_2_AOD` path, a scalar AOD, or `"retrieve"` - and never a silent
  fetch.* A single-product entry point quietly opening a second product would be a surprise, and the
  catalogue cannot promise the matching AOD granule exists.
- **Q5 (Phase 5). The honest ceiling over water.** *Ship it, with the measured uncertainty, and
  refuse to call it water-leaving reflectance.* A4 says the atmosphere over water is up to 195 times
  the signal; we will not match WFR, and should not imply we do. The alternative - restrict surface
  reflectance to land and point water users at `chl`/`tsm` and WFR - is defensible and should be the
  owner's call once Phase 3 has a number.
- **Q6 (Phase 2). Truecolor's frozen style.** *Ask the review study again, with corrected pictures.*
  The `0 - 0.6` range and `gamma 0.6` were chosen on pictures 2.1x too blue and are already open as
  Q5 of the Sentinel-3 review plan. After Phase 2 they will be wrong for a second reason: a
  Rayleigh-corrected blue band is roughly a third of the TOA value. No default moves without
  sign-off.

---

## D. Risks

1. **We are not going to beat ESA's processor, and should not try.** The goal is agreement within a
   stated tolerance against `SY_2_SYN` and `OL_2_WFR`. *Mitigation: every acceptance in section B is
   a residual against one of those products, on a named frame, reproducible from `run.sh`.*
2. **Scalar radiative transfer has a floor.** Rayleigh scattering is polarising, and a scalar
   treatment carries a known several-per-cent error at blue - the same place the correction matters
   most. *Mitigation: measure the floor in Phase 2 against SYN rather than claiming it away, and
   report it as the method's limit.*
3. **The only regime we can validate is the hard one.** Every matched triple is at solar zenith 63
   to 79 degrees (A5); there is no mid-latitude summer pair in the holding, because the archive has
   only October and November 2026 for S3A. *Mitigation: Phase 6 reports by solar zenith; if a
   high-sun check is wanted, one matched pair has to be fetched from the catalogue - ask first.*
4. **Water may stay out of reach** (A4). *Mitigation: Q5 before Phase 5, and no labelling of a
   corrected product as water-leaving reflectance.*
5. **Cost.** Three more tie-grid expansions and several array operations per band, on top of a
   true-colour path that takes 6.5 s. *Mitigation: measure it in Phase 1 and report it next to the
   failure count, per the benchmarking rule.*
6. **A correction is easy to get subtly, silently wrong** - the solar-flux flip that made every
   true-colour picture 2.1x too blue sat in a shipped product through an acceptance gate that
   compared it with itself. *Mitigation: the reference is always an independently produced ESA
   product on the same pixels, never our own earlier output.*

---

## Session log

- **2026-11-02 - audit.** Worktree `feature/atmospheric-correction` from `main` @ `da8f98d`. Found
  the archive holds 58 matched L1/WFR/SYN triples on a common pixel grid (A1), that every
  meteorological and geometric input is in the L1 product (A2), and that aerosol loading, Angstrom
  exponent and single-scattering albedo are all in `SY_2_AOD` (A6) - so all three tiers can be built
  and validated from the mount with nothing downloaded. Measured the budget: over land Rayleigh
  alone reaches within 0.003 of ESA's surface reflectance at red and blue and lifts the correlation
  at blue from 0.35 to 0.56 (A3); over water it covers less than half, leaving a residual up to 195x
  the water-leaving signal (A4). The whole holding is at solar zenith 63-79 degrees, where single
  scattering over-corrects by 0.17 (A5), so the high-air-mass case is the design case. No code
  changed.
