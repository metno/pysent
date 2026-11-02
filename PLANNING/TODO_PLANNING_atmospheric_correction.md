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

440, 550 and 670 bracket the three bands true colour renders.

**Corrected 2026-11-02, and the correction matters: most of those fields are empty.** Checked across
six of the 31 products, `Surface_reflectance_*`, `SSA_*`, `ANG550_865` and every `_uncertainty` are
**0 % populated** - they exist as variables and hold no data at all. What is actually there is the
optical depth itself, at **6 to 25 % spatial coverage** depending on the granule, plus `FM_AOD550`.
*Widened 2026-11-02 during Phase 3:* across the frames the sweep covered, coverage inside a frame's
own bounding box runs **6 to 58 %** - the land frame is the generous end at 58 %, the water frame
the sparse end at 9 % - so the range is wider than the first six products suggested, but the
conclusion is unchanged: on most frames most pixels have no retrieval.

So the aerosol tier gets measured loading but not a measured phase function, and three consequences
follow: the Angstrom exponent has to be **derived** from `AOD_440`/`AOD_550`/`AOD_670` (over the
land frame that gives about 1.18, a fine-mode continental aerosol, and a fine-mode fraction of 0.69
from `FM_AOD550`); most pixels of a frame have **no measured aerosol at all**, so Phase 3 needs a
stated fill policy rather than an interpolation; and the product's surface reflectance, which would
have been an independent second reference for A10 below, cannot be used.

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

### A9. Accuracy does not degrade with solar zenith, which the two-frame reading got wrong ✅ verified

`run.sh sweep` - the correction of Phase 2 run over **28 matched triples**, median absolute land
residual against `SY_2_SYN`, grouped by the frame's median solar zenith:

| zenith | frames | 665 nm | 560 nm | 490 nm | worst band |
|---|---:|---:|---:|---:|---:|
| 68-72 | 12 | 0.0039 | 0.0122 | **0.0176** | 0.0176 |
| 76-80 | 16 | 0.0089 | 0.0109 | 0.0071 | **0.0109** |

**The high-zenith group is as accurate as the low one, and at 490 nm it is better.** That directly
contradicts what two frames suggested - Phase 2 reported residuals of 0.038 to 0.094 at solar
zenith 79.6 and inferred a three- to sevenfold degradation. That frame
(`20261101T063201`) was **76 % bright**, leaving only 53 000 clear-land pixels, so its "clear land"
was almost certainly cloud-contaminated. It is an outlier about cloud, not a data point about
geometry, and a ceiling placed from it would have refused the better half of the archive.

Two other patterns hold across the whole sweep and are worth more than the zenith question was:

- **560 nm is negative in 12 of the 13 frames that have usable land**, between -0.004 and -0.016,
  at both zenith groups. So the Phase 2 miss is band-specific and geometry-independent, which rules
  out the air-mass explanations and points at the gas term or at the band itself.
- **Water sits at 0.03 to 0.08 in every frame regardless of zenith** - the aerosol gap of A4,
  stable, and the single largest error anywhere in the correction.

**What this means for a quality band:** inside 63 to 80 degrees there is nothing to refuse on
geometric grounds. Cloud contamination of the land mask matters far more than the sun angle, and the
honest per-pixel flag is about the mask and the aerosol source rather than the zenith.

### A10. The 560 nm bias is spectrally localised and the ozone term is not at fault ✅ verified

`run.sh spectral` turns the comparison round. Instead of asking what surface reflectance we produce,
it asks what path reflectance **Synergy's own answer implies**, and subtracts the Rayleigh part we
model:

    rho_implied = rho_toa / T_gas  -  rho_R  -  T_down T_up rho_syn / (1 - S rho_syn)

What is left must be the aerosol path reflectance, which **cannot be negative** and must fall
smoothly with wavelength. Over the A3 land frame, across the sixteen bands Synergy publishes:

| band | 400 | 412 | 442 | 490 | 510 | **560** | **620** | 665 | 674 | 681 | 709 | 865 | 1020 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| implied aerosol path | .0078 | .0066 | .0076 | .0046 | .0006 | **-.0102** | **-.0095** | -.0004 | .0008 | .0017 | .0011 | -.0033 | .0053 |

It is positive and smoothly falling from 400 to 510 nm - and consistent in size with the measured
`AOD_440` of 0.044 - then near zero from 665 to 1020. **It is strongly negative at exactly two
bands, 560 and 620**, which are the two with the strongest ozone absorption (gaseous transmittance
0.889 and 0.885, against 0.956 at 510 and 0.944 at 665). Both need the same factor, about 1.7x more
ozone absorption, to come out physical.

**But the ozone term is right.** Derived independently from 6SV at 300 DU and air mass 4, the
coefficient for band 7 is 1.0511e-4 against the table's 1.0513e-4; and `transmittance_ozone.total`
really is the two-way transmittance, since it equals `downward * upward` to five decimals and is
identical (0.88147 against 0.88148) for two geometries with the same air mass but a different split
between the legs. A column error of 1.7x would mean 485 Dobson units where the product says 285,
which is not a value the atmosphere takes at 50-62 degrees north in October.

So the disagreement is **0.012 of surface reflectance in two bands**, and the evidence points at the
reference rather than at this correction: Synergy's own spectrum there is 0.0451 at 510, 0.0697 at
560, **0.0701 at 620** and 0.0609 at 665 - a 620 nm value equal to the green peak, where chlorophyll
absorption should put it well below. **This is not settled**, and the thing that would settle it -
the aerosol product's own surface reflectance - is empty (A6). Recorded as a disagreement with
Synergy at 560 and 620, to be validated against the 400-510 nm bands where the implied path is
physical, rather than chased as a defect.

---

## B. Phases (one PR each, against `main`)

The sequence is chosen so that **each phase is validated against an independent reference before the
next one adds error on top**, and so that the least determined piece comes last, by which time its
error budget is measurable. It is not ordered by difficulty.

- [x] **Phase 0 - the table generator** (added after Q1 was answered). `tools/rt6s/` builds 6SV 1.1
  in a container, pinned by checksum, with `Py6S` driving it; `tools/rt6s/check.py` pins the
  reference case so a rebuild that changes the physics fails loudly, and `tools/rt6s/offgrid.py`
  (added in Phase 3) measures what the tables cost *between* their rows rather than at them - the
  off-grid reference points the tests pin, the azimuth step, and the sea-level aerosol assumption. **Verified**: at 490 nm, solar
  zenith 70, view zenith 20, relative azimuth 90, no aerosols, black ground, 6SV gives path
  reflectance **0.09609**, `tau_R` **0.15676**, transmittance **0.81451** down and **0.92285** up,
  spherical albedo **0.12390**. Two numbers to carry into Phase 2: `tau_R` agrees with the audit's
  analytic formula to four decimals (0.15676 against 0.1561), while the **multiple-scattering path
  reflectance is about 9 % *below* the single-scattering value** the audit used - the right sign to
  explain part of A3's 560 nm over-correction - and `T_down * T_up` is **0.75**, so the division A8
  describes is a 33 % lift, much larger than the 10-15 % estimated there.
- [x] **Phase 1 - the radiative-transfer frame, and the gas term.** Generalise tie-point reading to
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
  **Done, and it beat its acceptance.** `_atmosphere.py` holds the inversion, the two gas tables and
  the unit conversions; `_olci_l1.tie_point_field` is the generalised reader and `solar_zenith` is
  now one caller of it, as is the audit script - a measurement taken with a private copy of the
  reader proves nothing about the reader that ships. Measured on the A3 land frame
  (`run.sh budget land`), the gas correction alone:

  | band | T_gas | residual before | residual after |
  |---|---:|---:|---:|
  | Oa08 665 nm | 0.9439 | -0.0066 | **-0.0013** |
  | Oa06 560 nm | 0.8893 | -0.0280 | **-0.0150** |
  | Oa04 490 nm | 0.9780 | -0.0031 | **+0.0001** |

  All three move towards ESA's answer; 490 nm is now exact to a tenth of a per cent of reflectance
  and 665 nm is five times closer. **The 560 nm gap halves but does not close** - the remaining
  -0.015 is the single-scattering Rayleigh over-estimate, which is Phase 2's to fix and which 6SV
  already says is about 9 % too large before the 33 % transmittance division is applied.

  Two decisions taken here, both recorded because they shape every later phase:
  - **A negative surface reflectance is kept, not masked.** Where the atmosphere is brighter than
    the measurement the result goes below zero, and that is a retrieval outcome rather than a
    failure: ESA's own water-leaving reflectance is negative on 28 % of its valid pixels in the
    matched scene. Masking them would hide the error distribution and bias every statistic towards
    agreement. NaN is reserved for a genuinely undefined inversion.
  - **The air mass is decided by the angle, not its cosine.** `cos(90)` is 6e-17 rather than 0 in
    floating point, so a cosine test lets a horizon pixel through as an air mass of 1.6e16.

  21 new tests, all failing on `main` (the module does not exist there); the five that carry
  measured claims check the tables against 6SV at geometries and columns deliberately **off** the
  generated grid, so they test the interpolation and the unit conversions rather than the table
  they came from. Suite **461 passed, 13 skipped**. S5P freeze re-checked: zero differing hashes.
- [x] **Phase 2 - Rayleigh, properly.** Multiple scattering, the transmittance division, the
  spherical-albedo coupling, pressure from `sea_level_pressure` scaled by per-pixel `altitude`.
  *Acceptance: on the A3 land frame, the residual against `SY_2_SYN` is under 0.01 at all three
  bands - including 560 nm, where single scattering misses by 0.028 - and the correlation is no worse
  than A3's. On the A5 low-sun frame the result is not negative anywhere the quality band calls
  usable.* This is the phase that makes true colour defensible, and the phase that re-opens the
  frozen style question (Q6).
  **Done; the acceptance was met at two of the three bands, not all three.** The table is 36 288
  rows covering all 21 bands on both platforms: three Fourier terms in the relative azimuth, and
  pressure, solar zenith and view zenith on a grid. Measured on the A3 land frame:

  | band | T_gas | rho_R single | rho_R 6SV | T_dn*T_up | surface | SYN SDR | residual | r before | r after |
  |---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
  | Oa08 665 | 0.9439 | 0.0305 | 0.0324 | 0.9136 | 0.0600 | 0.0609 | **-0.0009** | 0.854 | **0.920** |
  | Oa06 560 | 0.8893 | 0.0613 | 0.0635 | 0.8377 | 0.0571 | 0.0697 | **-0.0126** | 0.814 | **0.929** |
  | Oa04 490 | 0.9780 | 0.1058 | 0.1049 | 0.7467 | 0.0445 | 0.0380 | **+0.0065** | 0.353 | **0.818** |

  665 and 490 nm are inside the 0.01 target; **560 nm misses it at -0.0126**. The correlation part of
  the acceptance is met with room to spare - at blue it goes 0.353 (uncorrected) to 0.555
  (single-scattering subtraction) to **0.818** (this phase), which is the clearest single number for
  what the phase bought.

  **The 560 nm miss is systematic, not noise, and it is not aerosol-shaped.** A second land sample
  (the clear-land part of the water frame) gives +0.0019, **-0.0099**, +0.0109: the same pattern,
  low at 560 and high at 490. An unmodelled aerosol inflates every band and inflates blue most, so
  it would push 560 *up*, not down. Something band-specific is left at 560 - most likely in how our
  ozone coefficient and Synergy's own correction treat that band, since 560 carries by far the
  largest gas term (T_gas 0.889). **Carried into Phase 3 as its first question**, since aerosol
  changes all three bands and diagnosing one through the other would be guesswork.

  **The low-sun half of the acceptance is met**: on the A5 frame at solar zenith 79.6 nothing is
  negative any more (0.1157 / 0.1284 / 0.1401 where single-scattering subtraction gave 0.088 /
  0.053 / 0.050). But the residuals there are **-0.038 / -0.094 / -0.079**, three to seven times
  the 70-degree frames, so the regime needs the ceiling Q3 asks about - now with numbers.

  **Over water the full inversion is further from WFR than a crude subtraction was** - residual
  0.0411 / 0.0558 / **0.0746** against the audit's 0.0391 / 0.0420 / 0.0634 - and that is correct
  behaviour, not a regression: the inversion attributes the leftover aerosol path radiance to the
  surface and then divides by a transmittance of 0.75, which amplifies it. A4 said the atmosphere
  over water is up to 195 times the signal; this is what that looks like once the Rayleigh part is
  handled properly.
- [x] **Phase 3 - aerosol from the AOD product.** Read `SY_2_AOD` (A6), put it on the frame, and
  build the aerosol path reflectance and transmittance from measured `AOD_550`, `ANG550_865` and
  `SSA_*` rather than a chosen model. *Acceptance: over land the residual against `SY_2_SYN` is
  smaller than Phase 2's at every band; over water the residual against `OL_2_WFR` falls from A4's
  0.039 to 0.063 by a stated factor. Both measured on the same frames, so the comparison is like for
  like.* Doing this **before** retrieval is the point of the sequence: with ESA's own aerosol inputs
  the correction's error is isolated, and if it cannot reach WFR here, no retrieval will rescue it.
  **Acceptance partly met, and the part that failed is the informative one.** Measured over every
  triple the archive held at the time - **55 frames, none unreadable** - with the loading each
  frame's own aerosol product reports (`run.sh sweep 60`). Median absolute residual over the frames
  that carry enough clear pixels of that type *and* have an aerosol product covering them, with the
  count that improved:

  | scene | band | gas+Rayleigh | +aerosol | frames better |
  |---|---|---|---|---|
  | water | 665 | 0.0371 | **0.0237** | **19 / 19** |
  | water | 560 | 0.0512 | **0.0360** | **19 / 19** |
  | water | 490 | 0.0697 | **0.0515** | **19 / 19** |
  | land | 665 | 0.0035 | 0.0041 | 10 / 19 |
  | land | 560 | 0.0116 | **0.0206** | **0 / 20** |
  | land | 490 | 0.0107 | **0.0045** | **19 / 20** |

  Over water the tier improves **every band on every frame**, by 26 to 36 %, which is the
  acceptance met. Over land 490 nm more than halves and 665 nm is a coin flip, but **560 nm gets
  worse on all twenty frames**, so "smaller at every band" is not met and is recorded as not met.
  The unanimity is the point: a correction with the wrong magnitude or the wrong spectral shape
  would scatter across frames of differing loading (0.02 to 0.25 here) and geometry. Twenty out of
  twenty in one direction, at the one band A10 had already localised a 0.012 reference-side bias
  to - band-specific, geometry-independent, negative in 12 of 13 frames before any aerosol term
  existed - says the aerosol tier is pushing 560 nm down correctly and the reference is not where
  we are heading. That is a claim for **question E1** to settle, not one to settle here, and it is
  why Phase 5 must not turn the tier on by default while E1 is open.
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
  **Answered 2026-11-02: generate the tables offline with an accepted radiative-transfer code,
  commit the tables and the script that generated them, and keep no runtime dependency.** This is
  *not* what the recommendation above said, and the reason for the change is risk 2: a hand-rolled
  scalar treatment carries a several-per-cent error at blue, which is the band the correction exists
  for. Generating offline buys accepted physics *and* keeps the apt-only runtime, at the cost of one
  reproducible offline step. The generation script and the container recipe are committed with the
  tables, so the numbers remain auditable - the point the original recommendation was defending.
- **Q2 (Phase 1). Where does the correction live?** *Both, with the option defaulting off:* a new
  `surface_reflectance` product for the physical quantity, and an `atmospheric_correction:
  "none" | "gas" | "rayleigh" | "full"` option on `truecolor` for the picture. The product is what a
  researcher wants; the option is what makes the picture right. Defaulting off keeps the frozen
  Sentinel-3 defaults frozen.
  **Answered 2026-11-02: both, option defaulting off**, as recommended.
- **Q3 (Phase 2). The high-air-mass ceiling.** *Produce, with a per-pixel quality band, and refuse
  above a documented solar zenith.* The whole archive is at 63 to 79 degrees (A5), so refusing the
  regime would refuse everything; but a correction that can return negative reflectance has to say
  where it stopped being meaningful. The threshold should be measured in Phase 2, not picked now.
- **Q4 (Phase 3). The aerosol source, and reading a second product.** *An explicit `aerosol` option
  naming the source - the `SY_2_AOD` path, a scalar AOD, or `"retrieve"` - and never a silent
  fetch.* A single-product entry point quietly opening a second product would be a surprise, and the
  catalogue cannot promise the matching AOD granule exists.
  **Answered 2026-11-02: the explicit option, never a silent fetch.** So `aerosol` names a path to
  an AOD product, a scalar, or `"retrieve"`; two runs of the same command cannot differ because of
  what happens to be on disk.
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
- **2026-11-02 - Q1 and Q2 answered, Phase 0 built.** Q1 came back **against** the plan's own
  recommendation - generate tables offline with a real radiative-transfer code rather than
  hand-rolling numpy - because a scalar treatment is several per cent wrong at blue. Built
  `tools/rt6s`. Three traps cost most of the time, each returning a plausible-looking wrong answer
  instead of failing: 6SV's own floating-point-exception summary breaks Py6S's output parser
  (`-ffpe-summary=none`); a `Geometry.User()` without `day`/`month` zeroes the solar term so every
  reflectance is 0.000; and the sensor defaults to **ground** level, which gives a path reflectance
  of 0 and an upward transmittance of exactly 1 while the optical depth still reports correctly.
  `check.py` now fails on each. Also: Py6S 1.9.2 imports `dateutil` without declaring it, and pip
  cannot upgrade an apt-installed numpy, so the image takes numpy from pip and not from apt.
  **Deviation from the plan:** Phase 0 did not exist when the phases were written; it was added
  because Q1's answer made the generator a prerequisite rather than an implementation detail.
- **2026-11-02 - Phase 1.** The gas tables came out shaped by measurement rather than by guess:
  ozone needs **one coefficient per band** because it is Beer-Lambert to one part in ten thousand
  over 100-400 DU, and water vapour needs **one curve per band** because its transmittance depends
  only on the column times the air mass. The second of those is 6S's own parameterisation, so the
  table is lossless with respect to 6S but the collapse is inherited, not tested - said so in the
  table header rather than presenting a zero disagreement as validation. Band 1 and 2 come out with
  exactly zero ozone absorption and band 7 with the maximum, which is the Chappuis band in the right
  place. Two traps in the generator: a Py6S predefined wavelength is `(id, start, end, response)`,
  so taking the first two elements labelled band 20 as 42961 nm; and the gas tables' units are
  neither the product's - the OLCI product reports `kg m-2`, 6S takes Dobson units for ozone and
  `g cm-2` for water, so the conversion lives in exactly one place.
  **Deviation:** the plan said Phase 1 would show the gas term moving 560 and 665 nm "in the
  documented direction by the documented amount". It moved all three bands and closed 490 nm
  entirely (+0.0001), which the plan had not predicted - so the acceptance was met on a band it
  did not name.
- **2026-11-02 - Phase 2.** Two findings worth more than the phase itself.
  **`set_target_pressure` is a silent no-op in this 6SV build**: accepted, and the Rayleigh optical
  depth stays at its sea-level value, identical at 1013, 850 and 700 hPa, while
  `set_target_custom_altitude` varies it correctly. The first table was generated through it and had
  a pressure axis holding one value five times over - it looked like a table and passed every test
  that did not ask how scattering varies with elevation. The generator now drives altitude and
  labels the axis in pressure, `check.py` asserts the no-op so a fixed 6SV announces itself, and
  the table had to be regenerated. Sea level is the axis top, because Py6S encodes a target altitude
  as a negative number in the field it shares with pressure, so a negative altitude is
  indistinguishable from a positive pressure and ignored; pressures above standard are
  **extrapolated** instead, which is safe on that axis only because a straight line through the top
  two nodes predicts the third to 5e-5.
  **Interpolate in the secant of the zenith angle, not the angle.** The path reflectance goes as
  `1/(mu_s mu_v)`, so it is nearly bilinear in the two secants and visibly curved in the angles. In
  degrees, the worst grid corner (solar zenith 78.5, view zenith 52) missed 6SV by **0.0041** - 40 %
  of the whole phase budget; in secant space the same corner misses by **0.0011** and the other
  reference points by about 1e-4. That is a four-fold accuracy gain for a one-line transform, and
  it is why the table did not need a denser grid.
  **Deviation:** the plan's acceptance was "under 0.01 at all three bands". Two of three were met;
  560 nm sits at -0.0126 and reproduces on a second frame, so it is carried into Phase 3 rather
  than papered over by loosening the target.
- **2026-11-02 - Q3 and Q4 answered; the sweep (A9).** Q3 came back "measure first", and the
  measurement **overturned the question's premise**: across 28 triples the 76-80 degree group is as
  accurate as the 68-72 group, and better at 490 nm. The 79.6-degree frame Phase 2 reasoned from was
  76 % bright, so its clear-land statistics were cloud-contaminated - an outlier about cloud, not a
  data point about geometry. **A threshold set from those two frames would have refused the better
  half of the archive.** The real lesson is the one the planning rules already encode: two points
  are an anecdote, and the question to ask of a surprising trend is how many frames it rests on.
  Q4 answered as recommended: aerosol comes from an explicit option, never a silent fetch.
  **Deviation:** no solar-zenith ceiling will be implemented, which is not one of the three options
  Q3 offered. The sweep also sharpened the 560 nm problem - negative in 12 of 13 frames with usable
  land, at both zenith groups - so it is band-specific and geometry-independent, which is a much
  better starting point for Phase 3 than Phase 2 had.
- **2026-11-02 - Phase 3, the aerosol tier.** Four things had to be measured rather than assumed,
  and two of them overturned a decision already taken.
  **Three Fourier terms cannot hold an aerosol phase function.** Phase 2 stores the azimuth
  dependence as three terms, which is lossless for Rayleigh (7.9e-6 over 21 bands). I reused that
  form for aerosol after checking **one** geometry, solar zenith 70 and view zenith 40, where it
  agreed to 4.5e-6. Over the whole table the worst fit is **0.18**, with 37 % of grid points past
  1e-3, concentrated where both zenith angles are large and the phase function is sharply
  forward-peaked: at solar zenith 85 and view zenith 60 the path reflectance runs 0.205 to 1.545
  across azimuth, and seven terms still miss it by 1.6e-2. Restricting to the archive's own
  envelope does not rescue it - p99 is still 6.6e-3 there, the size of Phase 2's entire budget -
  and refining the azimuth grid towards the forward peak came out *worse* than uniform spacing,
  because the curvature is spread across the range rather than concentrated. **The table now
  stores the samples**, every 10 degrees in columns `az000`..`az180`, and the library interpolates
  the three grid axes first and then along the azimuth curve.
  **The cost of that step, measured at the midpoints** where linear interpolation is furthest from
  the curve (`offgrid.py --what azimuth`, 384 geometries): **2.4e-3** at the archive's loading
  within its geometry, 9.1e-4 at view zenith 40 or less, 5.5e-3 at optical depth 0.15, and 2.3e-2
  at the table's worst corner. The `interp_error` column shipped with each row is a leave-one-out
  estimate over nodes 20 degrees apart, so it **over-states the real step by about 2.4 times** -
  5.8e-3 where the truth is 2.4e-3. It is a bound for screening a geometry, not the error, and
  both the generator and the table header now say so.
  **The whole chain against 6SV off the grid in every coordinate at once** - optical depth,
  both zeniths and azimuth - agrees to **1.8e-3** in path reflectance, worst at band 17, solar
  zenith 77.5, view zenith 52. Those four points are pinned in the tests.
  **Holding the aerosol at sea level is cheap.** The library adds a sea-level aerosol increment to
  the molecular term at the pixel's own pressure. Against 6SV run at altitude that costs
  **4.4e-4** or better over ground to 3 km at this archive's loading, reaching 5.5e-3 only at
  optical depth 0.4 and 3 km together. The elevated-terrain worry in D is therefore quantified and
  small, and the pressure axis the aerosol table does not carry is not worth its six-fold cost.
  **Deviation:** the aerosol table covers four bands (490, 560, 665, 865) and not all 21, because
  an aerosol run costs 6SV 0.65 s against 0.04 s and the azimuth axis is 19 deep - 4,752 rows from
  about 90,000 runs. Three of those bands are what true colour renders and the fourth is what a
  dark-target retrieval needs; extending is one command and about two hours.
  **Deviation:** the audit's water frame had to be repinned. `20261025T111740`'s Level-1 product
  **aged out of the rolling archive** while its Level-2 products stayed, so the frame reported
  itself as a half-present acquisition rather than an absent one. The water case is now
  `20261031T120105` - 63 % clear water against the old 48 %, but at solar zenith 79.5 rather than
  70.6, so its numbers are not comparable with A4's and are reported separately. `run.sh` now
  refuses a frame that has expired instead of passing empty paths into the reader, and says why.
  **Deviation:** the committed table's header prose was corrected in place rather than by
  regenerating the table, which would have cost two hours of 6SV for a comment. The numbers are
  untouched and the generator now emits exactly that text.
  **One defect found in committed audit code:** `aerosol.py` took its "ours" and "SYN" medians over
  each quantity's own valid pixels, so `ours - SYN` was a difference between two differently
  sampled populations - it read +0.0101 at 490 nm where the like-for-like figure is +0.0071. Fixed
  to share one mask, and it now agrees with `budget.py` to the digit on all three bands, which is
  two independent paths to the same number.
  **A second defect, and this one stopped the session: the audit scripts never released their
  scratch.** Every script took a `tempfile.mkdtemp` under `/tmp` and left it there, and `sweep.py`
  does that once per frame inside a fifty-frame loop. A frame's extracted members are a few
  hundred megabytes, so the run filled the root volume to **zero bytes free**. The failure wore a
  convincing disguise: the last two frames reported `RuntimeError` and `OSError` from the product
  reader, which reads as two corrupt granules rather than as a full disk - and the root volume was
  already at 97 % for reasons of its own, so there was little margin to begin with.
  Fixed twice over. Scratch now goes through one `scratch_dir()` helper that puts it on the data
  volume `/mnt/data` (overridable with `PYSENT_AUDIT_SCRATCH`), where there is 244 GB rather than
  17 GB; and `sweep.py` deletes each frame's scratch in a `finally` before reading the next, so
  peak usage is one frame instead of fifty. The re-run held the volume flat at 4.6 GB from first
  frame to last and read all 55 frames, so the numbers above are from a run with no disk failure
  in it.
  **How much the archive moves under a measurement, measured by accident.** Between the two sweeps,
  about an hour apart: three acquisitions **gained** their Synergy product (52 complete triples
  became 55), and the last Level-1 product on 2026/10/25 was **deleted**, so a frame that
  contributed water statistics to the first run had stopped being a complete triple by the second.
  That is why the water counts read 19 rather than 20. Of the 51 frames common to both runs, 50
  reproduced to the digit and one moved by 3e-4 at 560 nm. Nothing here changes a conclusion, but
  it sets the shelf life of any frame-pinned number in this plan at **days**, and it is the reason
  the acceptance table above states its frame counts rather than just its medians.
