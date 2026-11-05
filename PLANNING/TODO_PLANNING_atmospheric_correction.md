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

### A11. The per-pixel aerosol reference was in the water product all along ✅ verified

**This corrects A6 by addition, not by contradiction.** A6 went looking for aerosol in the aerosol
product and found the optical depth sparse and most of the rest empty. It did not look in
`OL_2_WFR`, which carries a **`w_aer` member holding `T865`** - aerosol optical thickness at
865 nm - and **`A865`**, the Ångström exponent, **per pixel on the same 4865-column instrument grid
as the radiance**, 79 % and 75 % populated over clear water on the water frame. Their `_unc`
companions are empty, like every uncertainty field in `SY_2_AOD`.

That is a far better reference for a water retrieval than the coarse product: same grid, no
resampling, eight times the coverage. Phase 4's water acceptance should be measured against it.

Reproduce with `run.sh retrieval`.

### A12. ESA's own two aerosol retrievals disagree by a factor of 2.2 ⚠️ verified, unresolved

On the water frame `20261031T120105`, over the same clear water:

| source | optical depth at 550 nm |
|---|---|
| `OL_2_WFR` `w_aer`, from `T865` 0.0630 and `A865` 0.541 | **0.0921** |
| `SY_2_AOD` `AOD_550`, median over the frame's cells | **0.0424** |

**Phase 3 used the lower one**, because A6 had only found that one. If the water product's figure
is the better one, every water correction in Phase 3 was applied at less than half the loading it
should have been - which is a candidate explanation for why the water residual improves by only a
quarter instead of closing.

This is the second ESA-internal disagreement this plan has turned up, after the 560 nm one in A10,
and it has the same shape: two processors, one radiance, two answers, and no way to arbitrate from
our side. It becomes a question for the expert review. It also means **"validate against ESA" is
not a single well-defined target over water**, which weakens every water number in this plan rather
than just this one.

### A13. The black-pixel assumption is sound about the water and wrong about the surface ✅ verified

The near-infrared retrieval assumes the water leaves nothing at 865 nm. Checked against ESA's own
water-leaving reflectance over clear water, that is very nearly true - median **0.0001**, p95
0.0009, and 665 nm is nearly as black at 0.0003. So the assumption is not the problem.

What *is* left over is not water. Taking ESA's own `T865` and `A865` as truth and subtracting the
path reflectance they imply, the leftover at 865 nm has median **+0.0048** and p95 +0.041 - fifty
times ESA's water-leaving reflectance there. That is surface reflection at the air-water interface,
sun glint, whitecaps and model mismatch, and a black-pixel retrieval hands all of it to the
aerosol. Measured: our single-band retrieval comes out **+0.010 to +0.031 high in `tau865`**
depending on the exponent assumed, against a correlation with ESA's `T865` of **0.82 to 0.84**.

So the retrieval gets the *pattern* right and the *level* wrong, by an amount that is now measured
rather than inferred. A level error is the easier of the two to carry honestly.

**Two bands fix the level and do not recover the slope.** A single black pixel is one measurement
against two unknowns, which is why the retrieved depth above slides with the exponent it is told to
assume. 665 nm is nearly as black as 865 over this water (0.0003), and `ln(865/665)` is 0.262
against 0.165 for `ln(1020/865)`, so **the pair already in the table has more spectral leverage
than adding the 1020 nm band would** - no regeneration, no new 6SV runs. Solving for both from the
two together:

| quantity | ours | ESA | correlation |
|---|---|---|---|
| `tau865` | **0.0691** | 0.0630 | **+0.838** |
| Ångström exponent | 1.200 | 0.553 | **+0.369** |

The depth bias falls from +0.021 to **+0.006** - from 33 % to 10 % - at the same correlation. The
exponent does **not** come back: the median is wrong by more than a factor of two and the
correlation is 0.37. The reason is in the same place as the level bias. The residual at 665 nm
after the fit is 0.0026, the same size as A13's leftover, so the spectral slope is being inferred
from the difference between two small numbers that are both contaminated by surface reflection and
glint. The slope is what that contamination destroys first.

**The exponent has to be fitted but not believed, and that is not the same as supplying it.** The
obvious reading of the above is "take the exponent from ESA's `A865` and use both bands to pin the
depth". Measured on the same pixels, with `A865` supplied per pixel as the exponent:

| inversion | bias in `tau865` | correlation |
|---|---|---|
| 865 alone, exponent supplied | +0.0201 | 0.790 |
| 665 and 865, exponent **supplied and held** | **+0.0432** | 0.779 |
| 665 and 865, exponent **fitted** | **+0.0061** | 0.838 |

Adding the second band at a *fixed* exponent is **twice as bad as not adding it at all**. The
reason is A13's leftover: 665 nm carries a larger molecular path and a relatively larger surface
and glint term, so forcing agreement at both bands with the slope held drags the depth upwards to
split the difference. Letting the slope float lets the fit put the contamination there instead of
into the depth.

So the exponent is a **nuisance parameter**: fitted because fitting it protects the quantity we
care about, and **not reported**, because at a correlation of 0.37 against ESA's own it is not a
measurement of anything. Phase 4's water arm fits both and publishes only the depth, with the
fitted slope recorded in provenance as a fit parameter rather than as an aerosol property. Solved
on 67 % of clear water.

### A15. There is no sea ice in the archive this plan validates against ⚠️ scope limit

Raised because the owner's group works on **sea ice** and wants accuracy over sea before land. The
honest position has to be stated before any sea-ice claim is made:

- **The OLCI holding tops out at 68.7 degrees north** (checked across 2026/10/29 to 11/01; median
  frame latitude 64.8). In late October the Nordic and Barents ice edge is far north of that, so
  **none of these frames contains sea ice.**
- OLCI Level-1 defines **no ice flag**. Its thirteen flags are land, coastline,
  fresh_inland_water, tidal_region, bright, straylight_risk, invalid, cosmetic, duplicated,
  sun-glint_risk, dubious, saturated, partially_saturated. Ice, snow and cloud all land in
  **`bright`**, and nothing in the Level-1 product separates them.
- **Every number in this plan excludes `bright` pixels.** That was right for validating against
  Synergy and WFR, and it means the validation says nothing at all about bright surfaces. The
  bright-but-not-land pixels in these frames - 1 % to 73 % of a frame - are cloud, not ice.

Three consequences, two of which are good news:

1. **The black-pixel aerosol retrieval must refuse over `bright` pixels.** Ice reflects 0.3 to 0.9
   in the near infrared where the method assumes zero, so it would return a wildly inflated
   optical depth with no indication of trouble. This is a hard requirement on Phase 4, not a
   preference.
2. **The exact inversion matters more over ice than anywhere else.** The coupling term
   `1/(1 - S*rho_s)` is what a subtraction-based correction drops. Over dark water at
   `rho_s = 0.03` it is 1.004 and ignorable; over ice at `rho_s = 0.8` with `S = 0.12` it is
   **1.106**, a 10 % effect. A8's decision to invert exactly rather than subtract was taken for
   the blue band over land and happens to be most valuable for exactly the surface the owner
   cares about.
3. **Validating over sea ice needs frames this archive does not hold.** Nothing was found at
   higher latitude on the mounted volumes; the `ESAcolhub` global store is content-hashed rather
   than organised by date or place, so it cannot be searched by latitude cheaply. **This is a
   question for the owner**: is there an Arctic OLCI source, and is there a sea-ice surface
   reflectance product to validate against? Without one, the correction can be applied over ice
   and its physics defended, but its accuracy there cannot be claimed.

### A14. A land retrieval has no dark target to anchor on 🚧 constraint, not a finding

OLCI has **no shortwave-infrared band** - its 21 bands stop at 1020 nm - so the standard
dark-dense-vegetation method, which anchors red surface reflectance to the 2100 nm band, cannot be
used. What the darkest clear land actually is, per Synergy on the water frame's land:

| band | p01 | p05 | median |
|---|---|---|---|
| 665 nm | **0.0180** | 0.0363 | 0.1082 |
| 560 nm | 0.0436 | 0.0715 | 0.1388 |
| 490 nm | 0.0248 | 0.0563 | 0.1207 |

The darkest percentile at 665 nm is 0.018, not zero, and it is *brighter* at 560 than at either
neighbour. Any land dark-target has to assume a surface reflectance of about 0.02 at 665 rather
than zero, and the assumption is then the dominant error. This is the reason Phase 4's land arm is
the weakest of the three and why the climatological fallback matters more than first thought.

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
  | water | 665 | 0.0371 | **0.0239** | **19 / 19** |
  | water | 560 | 0.0512 | **0.0369** | **19 / 19** |
  | water | 490 | 0.0697 | **0.0517** | **19 / 19** |
  | land | 665 | 0.0035 | **0.0034** | 10 / 19 |
  | land | 560 | 0.0117 | **0.0175** | **0 / 20** |
  | land | 490 | 0.0107 | **0.0031** | **19 / 20** |

  Over water the tier improves **every band on every frame**, by 28 to 36 %, which is the
  acceptance met. Over land 490 nm comes down by a factor of 3.5 and 665 nm is a wash - marginally
  better by median, 10 frames of 19 - but **560 nm gets worse on all twenty frames**, so "smaller
  at every band" is not met and is recorded as not met. Two bands of three meet it.
  The unanimity is the point: a correction with the wrong magnitude or the wrong spectral shape
  would scatter across frames of differing loading (0.012 to 0.288 here) and geometry. Twenty out
  of twenty in one direction, at the one band A10 had already localised a 0.012 reference-side bias
  to - band-specific, geometry-independent, negative in 12 of 13 frames before any aerosol term
  existed - says the aerosol tier is pushing 560 nm down correctly and the reference is not where
  we are heading. **The spectral signature is cleaner than it was**: with both neighbours now
  improving (490 by 3.5x, 665 marginally) and only 560 worsening, a band-localised offset in the
  reference fits and an error in our aerosol magnitude or slope does not. That is a claim for
  **question E1** to settle, not one to settle here, and it is why Phase 5 must not turn the tier
  on by default while E1 is open.
- [x] **Phase 4 - aerosol retrieval, for when no AOD product is at hand.** Dark-target over water
  (the near-infrared black-pixel assumption at 865 nm), dark-dense-vegetation over land, and a
  climatological fallback. *Acceptance: retrieved AOD against the `SY_2_AOD` product's own
  `AOD_550` on the same acquisition - a direct, honest metric - and the end-to-end surface
  reflectance no worse than Phase 3's by a stated margin.*
  **Shipped as `_aerosol_retrieval.py`, water only, and the acceptance is exceeded - with one
  caveat that has to be read before the numbers.** Validated against `OL_2_WFR`'s per-pixel `T865`
  rather than the planned `SY_2_AOD`, because A11 found a better reference and A12 showed the two
  ESA products disagree by 2.2x.

  Retrieval against ESA's own, on the water frame: `tau865` **0.0708** against **0.0630**, bias
  **+0.0078**, correlation **+0.841**, on 64,233 pixels - 77 % of clear water. Of the rest, 22.4 %
  refused as brighter than the table explains and 0.3 % as darker than clean air.

  End to end, median absolute residual against the water-leaving reflectance over clear water:

  | aerosol source | 665 | 560 | 490 |
  |---|---|---|---|
  | none | 0.0304 | 0.0439 | 0.0592 |
  | `SY_2_AOD`, per frame | 0.0215 | 0.0343 | 0.0501 |
  | `OL_2_WFR` `w_aer`, per pixel | 0.0124 | 0.0239 | 0.0391 |
  | **retrieved from the frame** | *0.0037* | **0.0142** | **0.0300** |

  **The 665 nm figure is substantially circular and must not be quoted as accuracy.** The
  retrieval fits the loading that makes 665 and 865 consistent with a black surface, and the
  reference at 665 is itself near zero, so driving that residual down is close to a tautology. The
  honest test is **560 and 490, which the retrieval never looks at**: against ESA's own per-pixel
  aerosol they improve by **41 %** and **23 %**. That is the claim, and it is a real one.

  **Land has no retrieval and will not get one here.** A14: OLCI has no shortwave-infrared band,
  so there is no dark target to anchor on, and the darkest clear land is 0.018 at 665 nm rather
  than zero. Over land the loading comes from a product or from the stated climatological value.
  Scope reduction, taken deliberately after the owner put sea and ocean ahead of land (B2).

  **The fitted exponent is a nuisance parameter and is not published as an aerosol property** -
  see A13. The fill order is retrieval, then product, then `CLIMATOLOGICAL_DEPTH_550` = 0.044,
  this archive's own median, with a `source` array recording which tier answered each pixel.
- [x] **Phase 5 - the product surface.** A `surface_reflectance` product and an
  `atmospheric_correction` option on `truecolor` (Q2), defaulting off until the owner says
  otherwise. Provenance tags recording the tier, the aerosol source and every optical depth used; a
  per-pixel quality band carrying the high-air-mass ceiling (Q3); and an uncertainty derived from
  `AOD_*_uncertainty`. *Acceptance: the provenance is enough to reproduce the pixel, and the frozen
  defaults are untouched.*

  Fixed by the owner's answers of 2026-11-05, and all four are acceptance conditions rather than
  preferences:
  - **A value on every clear pixel, dark water included, with the per-pixel absolute uncertainty
    beside it** (Q5). The uncertainty band is not optional decoration here - it is the only thing
    that makes shipping a 490 nm number whose error is nine times the open-ocean signal an honest
    act. A test must fail if the product can be written without it.

    **The uncertainty must be empirical, and the obvious construction is a tenfold lie.** The
    textbook route is to propagate the aerosol loading's uncertainty through the forward model.
    Measured, at the water frame's own geometry, the sensitivity is **about -0.10 of surface
    reflectance per unit optical depth at all three bands**, so even a generous 0.03 error in the
    loading - four times the retrieval's measured bias - moves the answer by only **0.0032 at
    490 nm**. The residual actually measured there against `OL_2_WFR` is **0.0300**. A propagated
    uncertainty would therefore stamp every open-ocean pixel with +-0.003 where the real error is
    ten times that, and it would do so with the full authority of a derivation.

    So the band carries a **measured** uncertainty: the validation residual for that band, surface
    class and aerosol tier, with the propagated loading term added in quadrature rather than
    standing in for it. That is less elegant and it is the only version that is not a lie. Two
    consequences: the uncertainty is explicitly *an empirical agreement with ESA's Level-2*, named
    as such in the tags and the documentation rather than implied to be a theoretical error budget;
    and **Phase 6 is what populates the table**, so Phase 5 ships it with the Phase 3/4 numbers and
    Phase 6 refines them. Where no validation figure covers a pixel's combination the band reports
    NaN rather than a propagated number, because "unmeasured" and "small" are different claims -
    though the climatological tier turned out to be measurable after all, and the Phase 5 note below
    records why leaving it NaN was wasteful rather than honest.

    This is also the sharpest instance of risk 6 in this plan: the quantity whose job is to stop a
    user over-trusting the product was itself about to be computed in the way that most understates
    it, and the derivation would have looked impeccable.
  - **The phrase "water-leaving reflectance" appears nowhere** in the label, the tags, the
    docstrings or the documentation (Q5).
  - **A second true-colour stretch for corrected mode, measured here and reported for sign-off**
    (Q6), selected when `atmospheric_correction` is not `"none"`.
  - **The frozen top-of-atmosphere style stays byte-identical** - `(0.0, 0.6)`, `gamma 0.6` (Q6).
    A regression test should pin that, because this phase is the one that could break it.

  **Shipped, and all four conditions are met.** `_olci_uncertainty.py`, `_olci_correction.py`, a
  `surface_reflectance` product and an `atmospheric_correction` option on `truecolor`. Suite **545
  passed / 18 skipped**, 549/14 with numba, against `main`'s 440/13.

  **Q6's stretch is measured, not chosen** - `atmospheric_correction_audit/stretch.py`, six S3A and
  six S3B frames. The criterion was stated before the numbers: clear land keeps the tone the frozen
  stretch gave it, and the top of the ramp does not clip. The corrected clear-land composite median
  is **0.088 against 0.157** at top of atmosphere, a 44 % drop, which at the frozen stretch would
  render **29 % darker**.

  **The first criterion was wrong and the measurement is what said so.** Anchoring the ramp's top on
  the corrected cloud p99 at 0.95 gave `(0.0, 1.18)` and gamma 0.31 - but corrected cloud measures
  **0.92 to 1.10, above one**, because the correction places the surface at ground level under the
  whole atmosphere and a cloud top is neither. That anchors the whole picture's tone on the one part
  of it that is not a retrieval. With the top instead fixed at **1.0, a reflectance's physical
  ceiling**, gamma is determined rather than fitted: **0.33**, which puts corrected land back at the
  0.448 of full scale the frozen stretch gave it. S3A gives 0.331 and S3B **0.333 independently**,
  and the per-frame spread is -14 % to +7 %.

  **Signed off by the owner on 2026-11-05**, on the pictures rather than the numbers, and what
  settled it was not the land tone it was designed around. Over land and open water the two
  candidates are a matter of taste and the frozen stretch is defensible. **Over sea ice they are not
  comparable.** Snow-covered ice sits at reflectance 0.4 to 0.9 and the frozen ramp ends at 0.6, so
  on the April polar frame it clips **99.0 % of the scene to flat white** - displayed 5th to 95th
  percentile 1.000 to 1.000, and two ice patches at 0.60 and 0.70 reflectance separated by
  **exactly nothing**. The accepted stretch clips 9.2 % and separates those same patches by 0.044 of
  the display range. The rendered pair is unmistakable: a blank page against visible floes and
  leads, from identical pixels. **The criterion the stretch was derived from - keep clear land's
  tone - turned out to be the second-best argument for it.**

  **The quality band is a 10-bit field in one `uint16`**, warped nearest whatever the values asked
  for, because a bitfield averaged with its neighbour is a different and plausible-looking bitfield.
  It records: corrected, the aerosol tier that answered (retrieved / product / climatological), haze
  beyond the table's edge via `aerosol_grid_limits`, bright surface, negative reflectance, the
  uncertainty being inferred or unmeasured, and **geometry outside the validated 63-80 degrees** -
  flagged, not refused, because A9 killed the ceiling but "no evidence of degradation inside 63-80"
  is not "accurate at 85".

  **The first real frame changed the design.** On `20261105T095140`, 67 % of the output came back
  with **no measured uncertainty**, because the retrieval is water-only and everything else fell to
  the climatological loading - which was in `UNMEASURED_TIERS`. Over a land frame that is most of
  what the product is for, so leaving it NaN was wasteful rather than honest. It turns out to be
  measurable: across the **47 frames of the holding with a reported `AOD_550`**, the stated 0.044 is
  off by **0.0346 at one sigma** (median 0.018, p95 0.131, range 0.012-0.288), and at the measured
  0.10-per-unit-depth sensitivity that costs **0.0035** of reflectance. So a climatological pixel
  borrows its surface class's measured `product` row and adds 0.0035 in quadrature. This is the one
  place a propagated term is the whole of a contribution rather than a tenth of it, because here the
  loading error *is* what is being described. **Unmeasured fell from 67 % to the `gas` tier alone.**

  **Two defects of my own, both caught by the suite rather than by reading.** The size cap counted
  spectral bands where the file now holds seven layers, so a `surface_reflectance` request could
  have passed the check and then written over the limit. And `surface_reflectance` was unroutable:
  an invariant test asserts every product in `S3_PRODUCTS` is reachable from a type, and adding the
  product to `S3_TYPE_PRODUCTS` would have made it a *default* for every `OL_1_EFR` request - an
  aerosol retrieval nobody asked for. Fixed with an `S3_OPT_IN_PRODUCTS` table, so the completeness
  check still covers it while `default_sentinel_s3_products` still returns `("truecolor",)` alone.
- [x] **Phase 6 - validation across the holding, not one frame.** Run the accepted correction over a
  sample of A1's 58 triples, both platforms, and report the residual distribution by band, by
  land/water and by solar zenith. *Acceptance: a table the next reviewer can argue with, and a
  stated regime where we refuse.*
  **Done, through `validate.py` (`run.sh validate`), and it found three defects in the uncertainty
  band rather than in the correction.** 55 comparisons over both platforms - 25 land, 16 bright, 14
  water - run through the **shipped** `correct_frame` rather than the audit's re-implementation, on
  the instrument grid so no warp enters the comparison.

  **The measurement that mattered is coverage**, which no earlier phase computed: the share of
  pixels whose real disagreement with ESA falls inside the figure the product published for them.
  It ran at **8 to 15 %** where a one-sigma band should reach 68 %.

  | surface | band | residual | p68 | ESA's signal | coverage before | after |
  |---|---|---:|---:|---:|---:|---:|
  | water | 665 | 0.0049 | 0.0094 | 0.0007 | 73.3 % | 79.3 % |
  | water | 560 | 0.0167 | 0.0252 | 0.0028 | 39.4 % | 67.7 % |
  | water | 490 | 0.0327 | 0.0486 | 0.0042 | 44.0 % | 67.6 % |
  | **bright** | 665 | 0.0475 | 0.0759 | 0.2639 | **5.0 %** | 68.0 % |
  | **bright** | 560 | 0.0681 | 0.0984 | 0.3114 | **12.2 %** | 68.0 % |
  | **bright** | 490 | 0.0454 | 0.0696 | 0.2900 | **5.8 %** | 68.1 % |
  | land | 665 | 0.0169 | 0.0294 | 0.0690 | 14.8 % | 67.9 % |
  | land | 560 | 0.0221 | 0.0419 | 0.0785 | 41.9 % | 68.1 % |
  | land | 490 | 0.0221 | 0.0329 | 0.0458 | 8.2 % | 68.0 % |

  **Three defects, none of which any test could have caught, because they were all defects in what
  was being measured rather than in how.**
  1. **A bias published where a spread was needed.** `sweep.py` computes `median(ours - reference)`,
     and the table was filled from those numbers while being used as a typical *absolute* error. At
     490 nm over land the bias is 0.0031 and the typical absolute error is 0.0221 - **seven times
     larger**. This happened immediately after writing an elaborate argument for not understating
     this band, which is worth remembering: the argument was right and the data fed into it was the
     wrong statistic.
  2. **A median where a p68 was needed.** A median absolute residual is exceeded by half of all
     pixels *by construction*, so publishing one as an uncertainty guarantees it is wrong half the
     time. The band now carries p68 and coverage lands at 67.6-68.1 % across every surface and band.
  3. **Bright surfaces had no row.** Ice over ocean took the water row and ice over land took the
     land row, understating a 665 nm ice pixel by a factor of ten. `bright` is now its own class.

  **And it refuted the premise Q5's answer was argued from.** The claim was that the error is
  absolute and so barely depends on target brightness. Measured, the correction is **1.4 times worse
  at 490 nm over bright surfaces than over clear water and nearly ten times worse at 665**. The
  *conclusion* survives comfortably - against ESA's 0.26 to 0.31 over those pixels, 0.045 to 0.068
  is 16 to 22 % relative where open ocean is 780 % - but it survives because the signal is seventy
  times larger, not because the error is the same. One of those was measured and the other was
  reasoning. A caveat that cannot be resolved with these products: over bright pixels ESA's own
  reference is least reliable, so some share of that 0.0475 is theirs.

  **The stated regime (the acceptance's second half).** No solar-zenith ceiling is imposed - A9
  killed that and this run agrees, with coverage holding near 68 % across 63 to 80 degrees. But
  **beyond 80 degrees over bright surfaces the band under-covers**: 41.7 % at 560 nm and 45.8 % at
  490, against a residual of 0.1224 where 0.0984 is published. That is spring and autumn sea ice, so
  it is the regime this group most needs and the one the single figure fits worst. The quality
  band's `outside_validated_geometry` bit already marks it; a zenith-dependent uncertainty is the
  obvious next refinement and is **not** in this phase.

---

## B2. What the correction is for

Stated by the owner on 2026-11-02, and it reorders the rest of the plan: **accuracy over sea and
ocean matters more than over land, because the group works on sea ice.**

Consequences already acted on:

- **Over water the loading comes from `OL_2_WFR`'s own `w_aer`, per pixel**, not from the coarse
  `SY_2_AOD` frame median - the owner's decision between the two products of A12. Measured over the
  whole holding, 55 triples, median absolute residual against the water-leaving reflectance:

  | band | gas+Rayleigh | +aerosol, coarse per frame | +aerosol, **`w_aer` per pixel** | frames better |
  |---|---|---|---|---|
  | 665 | 0.0353 | 0.0239 | **0.0121** | **21 / 21** |
  | 560 | 0.0512 | 0.0369 | **0.0235** | **21 / 21** |
  | 490 | 0.0697 | 0.0517 | **0.0386** | **21 / 21** |

  The per-pixel source **roughly doubles the improvement at every band** - 66 % off the residual at
  665 nm against 32 % from the coarse product - and it brings two more frames into the comparison,
  because `w_aer` covers 74 to 79 % of clear water where the coarse product covered 9 %. On the
  water frame the correlation against ESA also rises from 0.489 to 0.608 at 665 nm. **This is the
  single largest gain in the water arm, and it came from choosing the right reference rather than
  from better physics** - which is worth remembering before reaching for more radiative transfer.
  Land is unchanged, because there is no equivalent per-pixel field there.
- **Phase 4's retrieval must refuse over `bright` pixels** (A15), because that is where ice is and
  the near-infrared black-pixel assumption inverts to nonsense there.
- **Phase 6's validation must report water and land separately and lead with water**, rather than
  averaging them into one residual distribution.
- The land arm's weakness (A14, no shortwave infrared, no dark target) is **no longer the critical
  path**. A climatological fallback over land is acceptable where it would not have been if land
  were the priority.

Still open and needing the owner: **an Arctic OLCI source**. A15 establishes that the mounted
archive stops at 68.7 degrees north and holds no sea ice at all, so nothing in this plan
demonstrates accuracy over the owner's own surface. The owner has said they will point at one.

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
  **Answered 2026-11-05: ship everywhere, publish the error**, as recommended - a value on every
  clear pixel, the measured absolute uncertainty in a per-pixel band, and the phrase "water-leaving
  reflectance" nowhere in the product, its tags or its documentation.

  **The question was put to the owner in the wrong frame, and the re-framing is the part worth
  keeping.** Q5 as written above asks "water or land". That axis is wrong. The residual is an
  *absolute* reflectance error contributed by the atmosphere, so it is very nearly independent of
  how bright the target is, and what changes is only what it is a fraction *of*:

  | surface | reflectance at 490 nm | what 0.030 absolute becomes |
  |---|---:|---:|
  | clear open ocean | 0.004 | **800 %** |
  | turbid coastal water | 0.03 | 100 % |
  | melting / wet sea ice | 0.4 | 7.5 % |
  | dry snow-covered ice | 0.8 | **3.8 %** |

  So the real axis is **bright against dark, not water against land**, and both ends of it are
  water. The product is close to useless for open-ocean colour and genuinely good on sea ice. Two
  things follow, and they are the owner's answer rather than an inference from it:
  - The product **does not refuse a dark pixel**. It returns the number and publishes the error
    beside it, and the only thing standing between a user and a misreading is the uncertainty band.
    That puts a real obligation on Phase 5: the uncertainty has to be present, per pixel, and
    documented, or this answer has been implemented dishonestly.
  - **Nothing anywhere may call this water-leaving reflectance** - not the label, not a tag, not a
    docstring. The nearest wrong word would make the product look like a WFR substitute, which at
    nine times the signal it is not.
- **Q6 (Phase 2). Truecolor's frozen style.** *Ask the review study again, with corrected pictures.*
  The `0 - 0.6` range and `gamma 0.6` were chosen on pictures 2.1x too blue and are already open as
  Q5 of the Sentinel-3 review plan. After Phase 2 they will be wrong for a second reason: a
  Rayleigh-corrected blue band is roughly a third of the TOA value. No default moves without
  sign-off.
  **Answered 2026-11-05: a separate stretch per mode.** The frozen top-of-atmosphere style stays
  exactly as it is - `(0.0, 0.6)` and `gamma 0.6`, byte-identical output - and a second stretch,
  measured in Phase 5, is selected by the `atmospheric_correction` option when it is not `"none"`.

  Why a second stretch is needed at all, backing the TOA values out of Phase 2's own published
  terms on the A3 land frame:

  | band | TOA | corrected surface | change |
  |---|---:|---:|---:|
  | 665 nm | 0.082 | 0.060 | -27 % |
  | 560 nm | 0.099 | 0.057 | -42 % |
  | 490 nm | 0.135 | 0.045 | **-67 %** |

  The level drops, but the **shape inverting is the real change**: TOA rises towards blue, corrected
  is nearly flat. That is the haze leaving the picture, which is the entire point of the option. Held
  at `(0.0, 0.6)`, corrected land would use about the bottom tenth of the ramp where TOA used a
  fifth, rendering near 25 % grey instead of 40 % - a picture that is more correct and visibly
  darker at once, which users read as a bug in the correction rather than as honesty.

  **This answer deliberately does not re-open the frozen default**, so no pinned baseline moves for
  a third time this month and nobody who has not asked for the correction sees any change. The
  corrected-mode numbers are new defaults on a new code path, measured in Phase 5 and reported for
  sign-off before that path is reachable; the 2.1x-too-blue question against the frozen style stays
  with the review study, where it already is.

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
4. **Water may stay out of reach** (A4), and over clear open ocean it has stayed out of reach: the
   best residual is nine times ESA's own signal there. *Mitigation, now settled by Q5: ship the
   value anyway with its measured per-pixel uncertainty, and let no product, tag or docstring call
   it water-leaving reflectance. The risk is therefore accepted rather than closed, and the
   uncertainty band is what carries it.*
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
- **2026-11-02 - two interpolation bugs in Phase 3, found while setting up Phase 4.** Both were
  mine, both were shipped in `00692c7`, and the first invalidated every number Phase 3 reported.
  They are the same mistake twice: **a rule that was right for one axis, applied to another whose
  physics differs.**
  **Clamping at the bottom of the optical-depth axis.** The table's lightest row is 0.05, and
  `aerosol_terms` held that value below it, so **an optical depth of zero received the correction
  for the table's lightest haze**. Clamping is correct for the zenith axes and the plan says why -
  past the last node the curve is steep and unknown, so holding it is wrong in a bounded, visible
  way. I reused that helper for optical depth without noticing that **the value at zero is not
  unknown**: an atmosphere with no aerosol in it adds no path reflectance, dims nothing and adds
  nothing to the spherical albedo, by definition. It now scales linearly from that exact anchor to
  the first stored node, which the table itself shows is nearly straight - 0.157 of increment per
  unit depth at 0.05 against 0.148 at 0.15.
  This was not a corner case. **Of the 47 frames with a reported loading, 26 - 55 % - sit below
  0.05**, median 0.044, and the clamp over-corrected them by a median factor of **1.54**. Both
  frames Phase 3 validated on were among them (0.0332 land, 0.0424 water), which is why every
  number in the first Phase 3 write-up was overstated and has been re-measured here.
  **Pressure's extrapolation allowance inherited by an axis measured in optical depth.**
  `_trilinear` was written for the Rayleigh table and hardwired `(PRESSURE_EXTRAPOLATION_HPA, 0,
  0)`. The aerosol grid reused it, so its *first* axis - optical depth, topping out at 0.4 - was
  handed 60 units of extrapolation room, which is unbounded in practice. Measured at 490 nm:
  the transmittance ratio reads +0.703 at the table edge, **-0.010 at depth 1.5 and -2.28 at 5.0**.
  A negative two-way transmittance flips the sign of the inverted surface reflectance and reports
  it as confidently as any other pixel; a smoke plume is an ordinary way to reach 1.5. No frame in
  the current holding exceeds 0.4, so nothing measured was affected - this one was latent. Both
  tables now name their own rule (`RAYLEIGH_AXES`, `AEROSOL_AXES`) and `_trilinear` requires it
  rather than defaulting, so the next table cannot inherit the wrong one silently.
  **`aerosol_grid_limits`** is added alongside, because clamping at the top is only half an answer:
  a caller handed 1.5 now gets the 0.4 correction with no sign of it, and Phase 5's per-pixel
  quality band has to be able to say "hazier than the table goes".
  **What changed in the result, and what did not.** Every direction held; the magnitudes moved in
  the correction's favour. Land 490 nm went from 0.0045 to **0.0031** against a base of 0.0107,
  land 665 from 0.0041 to **0.0034** - now marginally *better* than the base rather than worse -
  and land 560's worsening shrank from +0.0089 to **+0.0058**. Water barely moved, because the
  water frames carry the heavier loadings. So the acceptance verdict improved from "fails at two
  bands of three" to "fails at one", and the 560 evidence got **stronger**, not weaker: both its
  spectral neighbours now improve while it alone degrades on every frame.
  **The lesson, stated so it is not relearned:** an interpolation rule is part of the physics of
  the axis it runs on, not a property of the interpolator. Clamping, extrapolating and anchoring at
  a known value are three different answers, and which is right depends on whether the value past
  the edge is unknown, nearly linear, or exactly known. Three of this plan's axes wanted three
  different answers and got one.
- **2026-11-03 - Phase 4, retrieving the loading from the frame.** Committed as `a2f62bb`. Three
  things had to be measured rather than reasoned, and one of them reversed a design I would have
  defended.
  **Two bands beat one only if the spectral slope floats.** The obvious design is to take ESA's own
  Angstrom exponent and invert both retrieval bands against it. Measured, that is **twice as bad as
  inverting 865 nm alone** - bias +0.043 against +0.020 - because 665 nm carries more surface and
  whitecap contamination, and a held slope has nowhere to put it except into the depth. Letting the
  slope float gives **+0.006**. So the exponent is fitted, and then *not published*: its correlation
  against ESA's own is 0.37, which means it measures nothing. It is a nuisance parameter that
  protects the depth, and it goes into provenance as a fit parameter rather than as an aerosol
  property (A13).
  **The inversion is exact, not a search.** The first version searched a 61-node grid and took
  **151 s for 15 tests** against 23 s for the entire rest of the suite. The forward model is
  piecewise linear in optical depth between the table's own nodes, with an exactly known anchor at
  zero, so evaluating four breakpoints and inverting analytically is **44x faster at 3.3 s** and
  continuous rather than snapped to a candidate. The tests retrieve 0.04, 0.12, 0.2 and 0.3 exactly,
  none of which is a node - which is the check that it is an inverse and not a lookup.
  **A refusal has to carry its reason, and twice it nearly did not.** `0.3999999999999999 >= 0.4` is
  False, so the ceiling refusal never fired until the comparison was moved to `nodes[-1] - 1e-9`.
  Worse, above the top path the exact inversion returns a bare **NaN**, so a bright pixel came back
  empty with nothing saying why - precisely the outcome the four reason masks exist to prevent. Now
  `brightest` is tracked across exponents so the pixel is refused *as* brighter than the table
  explains. Over sea ice this is the whole safety story: ice sits in `bright`, OLCI Level-1 has no
  ice flag, and the black-pixel assumption inverts to confident nonsense there.
  **Deviation: validated against a different product than the plan specified.** The acceptance named
  `SY_2_AOD`'s `AOD_550`. It was measured against `OL_2_WFR`'s per-pixel `T865` instead, because A11
  found the better reference and A12 found the two ESA products disagree with each other by 2.2x.
  Measuring against the worse of two references to honour the letter of an acceptance would have
  been theatre, but the substitution is a deviation and is logged as one.
  **Deviation: a disk fault that read as a data fault, and cost the most time of anything here.**
  Every audit script leaked its `tempfile.mkdtemp` scratch, which filled the root volume to zero
  bytes mid-sweep. The symptom was two granules failing with `RuntimeError` and `OSError`, which
  looks exactly like corrupt input - I went looking at the granules first. The fix is `scratch_dir()`
  onto `/mnt/data`, `atexit`, and a per-frame `rmtree` in a `finally`. **The lesson: when two
  unrelated granules fail at the same moment, suspect the machine before the data.**
- **2026-11-05 - Q5 and Q6 answered, both as recommended.** Q5: ship a value everywhere and publish
  the per-pixel error. Q6: a separate stretch per mode, leaving the frozen one alone.
  **The more useful outcome is that Q5 turned out to be asked in the wrong frame**, and the owner's
  answer is to the re-framed version. The question as written in section C asks whether to ship over
  water or restrict to land. But the residual is an absolute error contributed by the atmosphere, so
  it hardly depends on the target's brightness - only on what it is a fraction of. The same 0.030 at
  490 nm is **800 % of clear open ocean and 3.8 % of dry snow-covered ice**, and both of those are
  water. The axis was never water against land; it is bright against dark. That makes the product
  close to useless for ocean colour and genuinely good on sea ice, which is the surface this group
  actually works on - so "ship everywhere" is not a compromise here, it is the case where the answer
  and the purpose line up.
  The cost of that answer is a real obligation rather than a caveat: nothing refuses a dark pixel, so
  **the per-pixel uncertainty band is the only thing standing between a user and a misreading**. If
  Phase 5 ships the value without the error beside it, this answer has been implemented dishonestly,
  which is why it is written into Phase 5 as an acceptance condition with a test rather than as a
  preference.
  Q6 costs least of the four options offered precisely because it **does not touch the frozen
  default**: the pinned Sentinel-3 baselines have already been invalidated twice this month by the
  solar-flux fix, and a third time for a style compromise - decided before the review study has seen
  a single corrected picture - would have been the worst trade available. The corrected-mode numbers
  are new defaults on a new code path, reported for sign-off before that path is reachable.
- **2026-11-05 - Phase 5, the product surface.** The owner also lifted the real-data constraint:
  scenes may be copied from the read-only NFS archive onto `/mnt/data` so the CI container can see
  them. Two frames copied, 1.6 GB, 243 GB still free; the root volume was not touched.
  **The uncertainty band nearly shipped a tenfold understatement, and the derivation looked
  impeccable.** Propagating the loading's uncertainty through the forward model is the textbook
  route and it is wrong here by an order of magnitude: the measured sensitivity is **-0.10 of
  reflectance per unit optical depth**, so a 0.03 depth error - four times the retrieval's measured
  bias - moves 490 nm by **0.0032** against a residual measured at **0.0300**. Under Q5 the product
  ships a value on every clear pixel including dark open ocean, which makes this band the only thing
  between a user and a misreading, so it carries the empirical agreement with ESA's Level-2 indexed
  by band, surface class and loading tier, and the tags say "not a propagated error budget" in those
  words. Three refusals follow, each a test: 665 nm never reports its own residual when the loading
  was retrieved (the retrieval fits that band to a near-zero reference, so its 0.0037 is a
  tautology - it takes 560 nm's figure and is flagged as inferred); the table's own interpolation
  error is **not** added, because the residuals were measured *through those same tables* and adding
  it would double-count; and the `gas` tier claims nothing at all. That last one was my own error
  first - I had it inheriting the gas+Rayleigh row, which would have understated it by most of the
  Rayleigh path reflectance, 0.10 at 490 nm, a larger lie than the one the module exists to avoid.
  **The first real frame overturned a second decision of mine.** 67 % of the output came back with
  no measured uncertainty, because the retrieval is water-only and the rest fell to the
  climatological loading, which I had put in `UNMEASURED_TIERS` on the reasoning that borrowing the
  product row would claim a measurement's accuracy for a guess. Over a land frame that is most of
  what the product is for, and the reasoning was lazy rather than careful: **the cost of guessing is
  measurable, and both halves of it were already in hand.** Across the 47 frames with a reported
  `AOD_550` the stated 0.044 is off by **0.0346 at one sigma**, and at 0.10 per unit depth that is
  **0.0035** of reflectance. So the tier borrows the `product` row and pays 0.0035 in quadrature,
  and "unmeasured" shrank to the `gas` tier alone. The shape of the mistake is worth keeping:
  **"we never measured that" is a claim to check, not a conclusion** - twice in this phase the
  ingredients for the measurement were already committed to the repository.
  **Q6's first criterion was wrong, and the measurement said so.** Anchoring the corrected ramp's
  top on cloud at 0.95 gave `(0.0, 1.18)`, gamma 0.31 - but corrected cloud measures **0.92 to 1.10,
  above one**, because the correction places the surface at ground level under the whole atmosphere
  and a cloud top is neither, so that anchors the picture's tone on the one part of it that is not a
  retrieval. Fixing the top at **1.0**, a reflectance's physical ceiling, leaves gamma determined
  rather than fitted: **0.33**, and S3A and S3B agree to the third decimal independently (0.331,
  0.333), neither having been compared with the other until after both were measured.
  **Two defects caught by the suite rather than by reading.** The output size cap counted spectral
  bands where a corrected `surface_reflectance` writes seven layers, so a request could pass the
  check and then write over the limit. And the product was unroutable: the invariant that every
  entry in `S3_PRODUCTS` is reachable from a type failed, and the obvious fix - adding it to
  `S3_TYPE_PRODUCTS` - would have made a full aerosol retrieval the **default** for every
  `OL_1_EFR` request. `S3_OPT_IN_PRODUCTS` keeps the completeness check honest while
  `default_sentinel_s3_products` still returns `("truecolor",)` alone. That near-miss is a better
  argument for the invariant than anything written when it was added.
  **Two more found by reviewing my own diff after committing it, both honesty bugs rather than
  crashes.** `surface_reflectance` with `atmospheric_correction: "none"` returned
  **top-of-atmosphere reflectance, with no uncertainty band, under the label "Surface reflectance
  (atmospherically corrected)"** - the precise silent mislabelling Q5's answer exists to prevent,
  reached by passing one option. It is now refused, pointing the caller at `truecolor`, which is
  what the uncorrected quantity is already called. And the table-edge ceiling took the **highest**
  top across the bands in play where it should take the lowest: a loading is beyond the table if
  *any* band being corrected cannot reach it. Every band shares the 0.05-0.4 grid today, so the two
  agree - which is what would have made it a latent under-flagging bug rather than a visible one,
  the same shape as the `_trilinear` extrapolation bug in Phase 3.
- **2026-11-05 - the cost, measured, and a 25x fix** (risk 5). Phase 5's acceptance says to measure
  the correction's cost. Measured on a quicklook frame, the correction is cheap and the retrieval
  was not:

  | request | before | after |
  |---|---:|---:|
  | `truecolor`, no correction | 4.6 s | 4.6 s |
  | `truecolor`, `rayleigh` | 7.5 s | 7.5 s |
  | `truecolor`, `full` + climatological | 16.8 s | 16.8 s |
  | `truecolor`, `full` + **retrieve** | **692.3 s** | **27.2 s** |
  | `surface_reflectance` | **644.8 s** | **28.4 s** |

  So gas and Rayleigh cost 2.9 s and the aerosol tier 9.3 s, while the retrieval cost **675 s -
  forty times the whole rest of the pipeline**. Two causes, and the profile found both rather than
  intuition: 2204 calls to `_trilinear` taking 180 s on 100 000 pixels, from 38 calls to the blended
  forward model.

  **Half of it was computing answers in order to throw them away.** The forward model ran over every
  pixel of the frame when about two thirds are land, cloud or ice that the refusal masks discard
  immediately afterwards. Narrowing the arithmetic to the candidates - while still assembling the
  masks at the frame's own shape, so a refused pixel still says why - gave **692 s to 215 s**.

  **The rest was recomputing nineteen times what does not depend on the exponent.** The search tries
  19 Angstrom exponents, and the exponent changes **only how the two aerosol models are weighted**;
  every table interpolation underneath it, and the Rayleigh term entirely, is exponent-independent.
  `aerosol_path_components` now returns those pieces unblended, and the retrieval interpolates once
  and blends with arithmetic: **215 s to 27.2 s**. The azimuth interpolation moved into one
  `_model_path` helper so the blended and unblended paths cannot drift apart.

  **The answer is unchanged at every step** - retrieved fraction 0.2944 and median optical depth
  0.044 on the same frame, identical across all three versions, and the exact round-trip inversions
  at 0.04, 0.12, 0.2 and 0.3 still hold along with the four pinned 6SV off-grid points. The gated
  real-frame test fell from **655 s to 29.7 s**.

  **This is what makes Phase 6 possible**: 55 triples at 215 s a frame is three and a half hours; at
  28 s it is 26 minutes.
- **2026-11-05 - the Arctic gap, opened.** The owner answered A15's "we have no sea ice" by pointing
  at the catalogue rather than a directory: **`https://nbs.csw-staging.met.no/csw`**, a new DNS for
  the NBS CSW (pycsw 3.0-dev), and authorised copying scenes onto `/mnt/data`.
  `atmospheric_correction_audit/arctic_find.py` queries it; `run.sh arctic` reproduces.
  **Three things the catalogue settled.** It holds **1805 `OL_1_EFR` records for April 2026**, with
  real Arctic coverage - frames reaching **85.8 and 90.0 degrees north** - so the sea-ice regime is
  reachable after all. The records carry **THREDDS `fileServer` URLs**, so a frame is fetchable even
  though the mounted archive's seven-day window dropped it months ago; the local mount was never the
  limit, only the thing I had looked at. And **a title pattern and a bounding box can be combined**
  in one constraint here (1805 narrowing to 1002 for a 75-85 N box) - the S5P/S3 plan recorded that
  they could not, so either the new service fixed it or that note was written too broadly.
  **What does not match is `SY_2_AOD`**, and not for a catalogue reason: those products are
  orbit-long, so their sensing window *contains* an OLCI frame instead of equalling it, and an
  exact-key join finds zero. The existing audit already matches them by coverage (`sensing_window` /
  `covers`) for exactly this reason. Ten April acquisitions carry `OL_1_EFR` + `OL_2_WFR` above
  78 N, which is the pair that matters anyway, since `w_aer` is the better reference (A11/A12).
  **A pair 38 minutes apart exists**: S3B `20260419T171323` and S3A `20260419T175203`, both above
  76.7 N. That is the reference-free geometry-consistency test the plan asked for, with two
  platforms and two viewing geometries over very nearly the same surface. Downloaded, not yet run.
- **2026-11-05 - the first sea-ice frame, and what it showed.** S3A `20260412T072814`, 76.8 to
  **90.0 N**, 12 April 2026, run through `surface_reflectance` end to end.
  **Every design decision about bright surfaces fired correctly, which is the first real evidence
  any of them work.** The frame is **99.99 % `bright`** - that is sea ice, and OLCI has no ice flag,
  exactly as A15 said. The retrieval **refused 99.994 % of it as not clear water** and the
  climatological loading filled in; `aerosol_retrieved` is 0.000. Median corrected reflectance at
  665 nm is **0.80**, which is right for snow-covered sea ice, and nothing is negative.
  **And it vindicated this morning's climatological fix in the sharpest possible way.** Had
  `climatological` stayed in `UNMEASURED_TIERS`, this entire frame - every pixel of it - would have
  shipped with **no uncertainty at all**, which under Q5 is the one thing the product must not do.
  As it is, `uncertainty_unmeasured` is **0.000**. The decision that looked like tidiness over a
  land frame turns out to be the difference between a usable ice product and an unusable one.
  **The honest limitation, measured rather than asserted.** Every validation row was measured on
  pixels the product does *not* call bright, so over ice the published figure is an extrapolation
  along exactly the axis Q5 says matters. One term in that extrapolation can be checked: the surface
  coupling `1/(1 - S*rho_s)` is 1.004 over dark water and 1.106 at reflectance 0.8, and through the
  real inversion a 5 % error in the spherical albedo moves the answer by **0.00001 at 490 nm over
  dark water against 0.00418 over ice** - four hundred times more, but still an order of magnitude
  under the 0.039 published for that class. So the figure is not an understatement on that account;
  what the *other* terms do over ice is unmeasured, and that is Phase 6's job now that the frames
  exist. `19.3 %` of the frame is also flagged `outside_validated_geometry`, April at 76-90 N being
  outside the autumn solar-zenith range every number was measured in.
