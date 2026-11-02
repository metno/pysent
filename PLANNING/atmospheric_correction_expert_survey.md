# OLCI atmospheric correction: the choices we made, for expert review

**Expert-facing page:** <https://claude.ai/code/artifact/ae8af71a-84ac-4405-a309-5ae61fbf28f9>
(private until shared). This file is the source of record; the page is the same content laid out for
someone who is going to answer the questions rather than maintain the code.

Every decision taken while building pysent's atmospheric correction for OLCI Level-1, written so a
domain expert can disagree with it. Each entry says **what we chose**, **the evidence**, and **what
it costs if the choice is wrong** - the last of those being the part that decides whether a
disagreement matters.

Nothing here is settled by authority. Where a choice rests on a measurement, the measurement is
reproducible with `PLANNING/atmospheric_correction_audit/run.sh`; where it rests on judgement, it is
marked as such and appears again in the questions at the end.

**Status.** Phases 0-2 are implemented and merged into no branch yet (PR #32); Phase 3 (aerosol) is
in progress. Nothing is wired into a product and **no existing default output has changed**.

---

## 0. What the correction is, and what it is not

| | |
|---|---|
| Input | Sentinel-3 OLCI Level-1 EFR radiance, both platforms |
| Output | surface reflectance, per band, as a physical quantity |
| Tiers | gaseous absorption, Rayleigh scattering, aerosol |
| Not included | adjacency effects, sun glint, whitecaps, BRDF normalisation, cloud screening beyond the product's own flags |
| Other Sentinel-3 products | untouched - `chl`, `lst`, `sst`, `ndvi` and the rest are ESA Level-2 and arrive with ESA's correction already applied |

**The region we can test in is mid-latitude to sub-polar autumn.** Every validation frame is from the
Norwegian ground segment's holding for October and November 2026, at solar zenith 63 to 79 degrees.
That is a hard regime, not an easy one, but it is also a narrow one: no tropical scene, no summer
high sun, no desert dust, no biomass burning.

---

## 1. Method choices

### C1. The reference is ESA's own Level-2, pixel for pixel

**Chosen.** Accuracy is measured against `SY_2_SYN` surface directional reflectance over land and
`OL_2_WFR` water-leaving reflectance over water, for the *same acquisition*, on the *same*
4865-column instrument grid - so no warping or resampling enters the comparison.

**Evidence.** The archive holds 58 such matched triples for S3A over two months. Measured residuals
are therefore per-pixel differences against an independently produced retrieval.

**If wrong.** Both references are processor outputs, not ground truth, and they share the same
Level-1 radiance with us. A bias common to ESA's processor and ours would be invisible. This is the
single largest methodological weakness in the work, and **question E8** asks what else to use.

*Why it was chosen anyway: the alternative in reach was comparing against our own earlier output,
which is how the previous true-colour bug survived an acceptance gate - it validated one band
against itself.*

### C2. The radiative transfer is 6SV 1.1, run offline, with the tables committed

**Chosen.** Coefficients are generated once by 6SV 1.1 in a pinned container
(`tools/rt6s/Dockerfile`) and committed as CSV. pysent itself depends on numpy and GDAL and calls no
radiative-transfer code at processing time.

**Evidence.** A hand-rolled scalar treatment was the alternative and was rejected on a measurement:
6SV's multiple-scattering path reflectance is about 9 % below the single-scattering value, and the
two-way scattering transmittance at this archive's air masses is 0.75 rather than the 0.85-0.9 a
first-order estimate suggests. Both errors would have applied at once, in the same direction.

**If wrong.** We inherit 6SV's own errors, including its scalar-versus-vector treatment of
polarisation, with no way to see them from inside. The tables are diffable, so an expert can check
any single value against another code.

**A note on how much care this took, because it bears on trust in the tables.** Five separate ways
of driving 6SV through Py6S return a plausible wrong answer rather than an error, and each was found
by a measurement that did not come out as expected: the sensor defaults to ground level, which gives
a path reflectance of 0 and an upward transmittance of exactly 1 while the optical depth still reads
correctly; a geometry without a date zeroes the solar term so every reflectance is 0.000; gfortran's
floating-point-exception summary lands in the output Py6S parses and breaks every run;
`set_target_pressure` is accepted and ignored, so a pressure axis silently holds one value at every
node; and with an aerosol present the path reflectance is **split across columns**, the first
holding only the molecular part, so an aerosol axis reads as having no effect. All five are asserted
in `tools/rt6s/check.py`, so a future rebuild that behaves differently says so.

### C3. The inversion is not a subtraction

**Chosen.** `rho_toa / T_gas = rho_path + T_down T_up rho_s / (1 - S rho_s)`, inverted exactly for
`rho_s` as `x / (1 + S x)` with `x = (rho_toa/T_gas - rho_path) / (T_down T_up)`.

**Evidence.** The transmittance division alone is a 33 % lift at 490 nm and solar zenith 70. The
spherical-albedo coupling is negligible over dark water and a few per cent over bright land.

**If wrong.** It is standard first-order radiative transfer; the risk is not in the form but in the
terms fed to it.

---

## 2. Gaseous absorption

### C4. Ozone gets one Beer-Lambert coefficient per band

**Chosen.** `T = exp(-k U m)`, with `U` in Dobson units and `m = 1/cos(sza) + 1/cos(vza)`; one `k`
per band per platform, from OLCI's own spectral response.

**Evidence.** Measured with 6SV over columns of 100 to 400 DU, `k` is constant **to about one part
in ten thousand** - ozone does not saturate in the Chappuis band, so a curve would add nothing. The
coefficients put zero absorption at 400 and 412 nm and the maximum at 620 nm, which is the Chappuis
band in the right place. An independent re-derivation at 300 DU and air mass 4 gives 1.0511e-4 for
band 7 against the table's 1.0513e-4.

**If wrong.** Directly scales the correction in the 490-665 nm range. See **question E1**: two bands
disagree with Synergy by 0.012 and would need 1.7x more ozone absorption to be reconciled, which we
do not believe.

### C5. Water vapour gets one curve per band, in the scaled path amount

**Chosen.** Transmittance is tabulated against `column x air mass` and interpolated linearly.

**Evidence.** Water vapour saturates, so a single coefficient cannot work. The collapse onto the
scaled path amount is exact within 6SV.

**Honest limitation.** That collapse is **6SV's own parameterisation**, so the table is lossless
with respect to 6SV but the physics is inherited, not tested. **Question E7.**

### C6. The columns come from the product, in the product's units

**Chosen.** `total_ozone` and `total_columnar_water_vapour` from `tie_meteo.nc`, both `kg m-2`,
converted in exactly one place (1 DU = 2.1415e-5 kg m-2; 1 kg m-2 = 0.1 g cm-2).

**If wrong.** A unit error here is a silent factor of tens of thousands, which is why the conversion
is centralised and tested against 6SV at columns off the generated grid.

---

## 3. Rayleigh scattering

### C7. Three Fourier terms in the relative azimuth - for Rayleigh only

**Chosen.** `rho = a0 + 2 a1 cos(dphi) + 2 a2 cos(2 dphi)`, fitted to thirteen azimuths. **The
aerosol table does not use this form**; it stores samples every 10 degrees and interpolates
linearly between them.

**Evidence.** For Rayleigh the worst fit error anywhere in the table, across all 21 bands and both
platforms, is **7.9e-6** in reflectance - effectively lossless, because molecular scattering really
is a two-term phase function.

For aerosol the same form fails, and this was measured only after a first table had been generated
with it. The worst fit is **0.18**, with 37 % of grid points past 1e-3, concentrated where both
zenith angles are large and the phase function is sharply forward-peaked; at solar zenith 85 and
view zenith 60 the path reflectance runs 0.205 to 1.545 across azimuth, and *seven* terms still
miss it by 1.6e-2. Inside this archive's own geometry the p99 was still 6.6e-3, the size of the
entire molecular error budget. Refining the azimuth grid towards the forward peak came out **worse**
than uniform spacing, because the curvature is spread across the range rather than concentrated.

Stored samples at 10 degrees cost, measured against 6SV at the midpoints where linear
interpolation is furthest from the curve: **2.4e-3** at this archive's loading and geometry,
9.1e-4 at view zenith 40 or less, and 2.3e-2 at the table's worst corner (optical depth 0.4, solar
zenith 85, view zenith 60).

**Caveat a reviewer should hold us to.** Each aerosol row also ships an `interp_error` column, a
leave-one-out estimate. It interpolates across 20 degrees where a caller only crosses 10, so it
**over-states the real error by about 2.4 times**. It is a screening bound, not the error.

### C8. Interpolate in the secant of each zenith angle, not the angle

**Chosen.** Multilinear interpolation on `(pressure, 1/cos(sza), 1/cos(vza))`.

**Evidence.** Path reflectance goes as `1/(mu_s mu_v)`, so it is nearly bilinear in the secants. At
the steepest grid corner (solar zenith 78.5, view zenith 52) interpolating in degrees misses 6SV by
**0.0041**; in secant space, by **0.0011**.

**If wrong.** Nothing - it is a change of variable, and the 6SV comparison bounds the residual error.

### C9. Pressure from sea-level pressure and per-pixel elevation, extrapolated above standard

**Chosen.** Barometric formula from the product's own `sea_level_pressure` and per-pixel `altitude`.
The table's pressure axis stops at sea-level standard, and higher pressures are **linearly
extrapolated** up to 60 hPa above it; the zenith axes are **clamped**, never extrapolated.

**Evidence.** The table could not be generated above sea level: Py6S encodes a target altitude as a
negative number in the field it shares with pressure, so a negative altitude is indistinguishable
from a positive pressure and silently ignored. Extrapolation is safe on this axis because a straight
line through the top two nodes predicts the third to **5e-5**; clamping instead would cost about
0.0025 at 1040 hPa, a quarter of the phase's whole error budget. The zenith axes are clamped because
the curve steepens towards the horizon, where extrapolating invents brightness.

**If wrong.** Elevated terrain is where it would show. Rayleigh optical depth follows pressure, so
ground at 2 km scatters about a fifth less than the same scene at sea level; treating a mountain as
sea level over-corrects it.

---

## 4. Aerosol

**What the tier does, measured over every matched triple the archive held (55 frames, none
unreadable), using the loading each frame's own `SY_2_AOD` product reports.** Median absolute
residual against ESA's Level-2, over the frames carrying enough clear pixels of that type and
covered by an aerosol product, with the number in which the tier improved agreement:

| scene | band | gas+Rayleigh | +aerosol | frames better |
|---|---|---|---|---|
| water | 665 | 0.0371 | **0.0239** | **19 / 19** |
| water | 560 | 0.0512 | **0.0369** | **19 / 19** |
| water | 490 | 0.0697 | **0.0517** | **19 / 19** |
| land | 665 | 0.0035 | **0.0034** | 10 / 19 |
| land | 560 | 0.0117 | **0.0175** | **0 / 20** |
| land | 490 | 0.0107 | **0.0031** | **19 / 20** |

Over water every band improves on every frame. Over land 490 nm comes down by a factor of 3.5 and
665 nm is a wash, but **560 nm gets worse on all twenty frames** - which is the single most
important number in this document and the subject of **question E1**. Across frames whose loading
spans 0.012 to 0.288 and whose solar zenith spans 63 to 79 degrees, a correction of the wrong
magnitude or the wrong spectral shape would scatter. Twenty out of twenty in one direction, at the
one band where a 0.012 disagreement was already localised before any aerosol term existed, points
at the reference rather than at the correction - but we cannot prove that from our side, which is
why nothing is on by default (C16).

What makes that reading harder to dismiss is the **spectral shape**: 560 nm is the only band that
degrades, and both its immediate neighbours improve. An error in our aerosol magnitude or spectral
slope would not single out one band between two that get better.

Over water the improvement is real and still not sufficient: 0.0697 to 0.0517 at 490 nm leaves a
residual **fourteen times** the water-leaving signal it is being compared against. See **E2**; the
two quantities are not the same thing.

### C10. The caller names the aerosol source; nothing is fetched silently

**Chosen.** An `aerosol` option taking a path to an `SY_2_AOD` product, a scalar optical depth, or
`"retrieve"`. A single-product entry point never opens a second product on its own, and the
product's sensing window must *contain* the frame's - orbit-long aerosol products cover several
frames, so containment is the check.

**Evidence/judgement.** Judgement. Two runs of the same command must not differ because of what
happens to be on disk, and the catalogue cannot promise the matching aerosol granule exists.

### C11. Two aerosol models, chosen by the *measured* Angstrom exponent

**Chosen.** 6SV's Continental and Maritime models, interpolated on the Angstrom exponent **derived
from the aerosol product's own `AOD_440`, `AOD_550` and `AOD_670`** - not selected by a land/water
mask.

**Evidence.** Over the land frame the measured optical depths give an Angstrom exponent of about
**1.18** and a fine-mode fraction of 0.69 - fine-mode continental. Marine air runs nearer 0.1 to
0.5. Choosing by land mask would mislabel continental outflow over water and marine air over
coastal land.

**Scope, stated plainly.** The aerosol table covers **four bands** - 490, 560, 665 and 865 nm - at
optical depths 0.05, 0.15 and 0.4, over 11 solar zeniths, 9 view zeniths and 19 stored azimuths:
4,752 rows from about 90,000 6SV runs. Three of those bands are what true colour renders and the
fourth is what a dark-target retrieval needs. Extending to all 21 is one command and about two
hours; it was not done first because an aerosol run costs 6SV 0.65 s against 0.04 s for a clear
one, sixteen times as much, and because C7's azimuth axis is 19 deep rather than 13.

The exponents the two models themselves carry, read off their own band optical depths rather than
quoted from a textbook: **Continental 1.11, Maritime 0.27**. The land frame's measured 1.18
therefore sits at - in fact just past - the Continental end, so on that frame the blend is not
really blending. A reviewer should read the land results as "Continental", not as "a mixture".

**If wrong.** This is the weakest physical choice in the correction, and **question E4** asks
directly about it. Two models span a line in a space that really has at least three dimensions
(size distribution, absorption, vertical placement), and the archive's own single-scattering albedo
field - which would have constrained absorption - is empty.

### C11b. How the aerosol table behaves at its own edges

**Chosen.** Below the lightest stored optical depth, 0.05, the terms scale linearly to an **exact
zero at no aerosol at all** - no path reflectance, no dimming, no addition to the spherical albedo.
Above the heaviest, 0.4, they are **clamped**, and `aerosol_grid_limits` publishes both ends so a
caller can tell when a pixel was hazier than the table goes.

**Evidence/judgement.** Three different answers for three axes, chosen by whether the value past
the edge is known. At zero it is known exactly, so anchoring there is not an approximation; between
zero and 0.05 the increment per unit depth is 0.157 against 0.148 at 0.15, so a straight line
through the origin is close. Above 0.4 it is unknown, so clamping is the bounded wrong answer
rather than an invented one. Pressure, by contrast, extrapolates a little (C9), because it is
nearly linear there.

**This was wrong in the first implementation, in both directions at once.** The lookup clamped at
the bottom as well as the top, so an optical depth of zero received the correction for the table's
lightest haze - and 55 % of this archive's frames sit below 0.05, over-corrected by a median factor
of 1.54. The top end meanwhile inherited the pressure axis's extrapolation allowance, which drove
the transmittance ratio **negative** past depth 1.5 and would have flipped the sign of a smoke
plume's surface reflectance. Every aerosol figure in this document was re-measured after the fix.

### C12. The aerosol table is at sea level only

**Chosen.** The aerosol increment is tabulated at sea level and added to the pressure-interpolated
Rayleigh value.

**Evidence, now measured.** Against 6SV run at altitude - the only honest test, since the question
is whether the air column above the aerosol matters - the approximation costs **4.4e-4 or better
over ground up to 3 km** at this archive's loading, and reaches 5.5e-3 only at optical depth 0.4
and 3 km together. The reasoning behind it holds: the aerosol sits in the boundary layer, so the
column above changes its scattering far less than it changes the molecular term, and a pressure
axis would multiply the table sixfold for an effect smaller than the azimuth step.

**Question E6** stands, but now as a question about the hazy-and-high corner rather than about the
approximation in general.

### C13. Where the aerosol product has no value, the granule's own median is used

**Chosen.** Interpolate where measured; fall back to the scene median of that granule; record the
filled fraction in the product's provenance. Never substitute a climatology silently.

**Evidence.** The aerosol product's optical depth is present on only **6 to 58 %** of its cells,
depending on the granule, so most pixels of most frames have no measured aerosol at all. **Question
E5** asks whether a median fill is acceptable or whether unmeasured ground should be refused.

**How the table in this section was actually measured, stated so it is not over-read.** It used
**one optical depth and one Angstrom exponent per frame** - the median over the aerosol cells
inside the frame's bounding box - not a per-pixel field. That was deliberate: with coverage this
sparse, on a coarse grid unaligned with the radiance grid, a per-pixel field would mostly be
measuring the gap-filling policy rather than the aerosol tier. So the numbers above answer "given
the loading ESA itself reports here, does correcting for it help?" and not "does a per-pixel
aerosol field help?". The second question needs the retrieval and the fill policy, and belongs
with E5.

---

## 5. Output and product choices

### C14. A negative surface reflectance is kept, not masked

**Chosen.** NaN is reserved for a genuinely undefined inversion (no transmitted light, missing
input). A negative result is returned as it is.

**Evidence.** ESA's own water-leaving reflectance is negative on **28 %** of its valid pixels in the
matched scene we validate against. Masking negatives would hide the retrieval's error distribution
and bias every statistic computed from the product towards agreement.

**If wrong.** Downstream users who clip without counting will quietly discard a real part of the
distribution. A caller who wants only physical values should clip, and know how much was clipped.

### C15. No solar-zenith ceiling

**Chosen.** The correction is produced everywhere the tables cover, with no refusal based on sun
angle.

**Evidence.** This reverses an earlier reading. Two frames suggested a three- to sevenfold
degradation towards the horizon; a sweep over **28 matched triples** showed the 76-80 degree group is
**as accurate as** the 68-72 group (worst-band median absolute residual 0.0109 against 0.0176) and
better at 490 nm. The frame that suggested degradation was 76 % bright, so its clear-land statistics
were cloud-contaminated. A ceiling set from those two frames would have refused the better half of
the archive.

The wider sweep run for the aerosol tier agrees, and adds a detail: by solar-zenith band the
worst-band median absolute land residual is 0.0131 at 68-72 degrees, 0.0124 at 72-76 and **0.0117
at 76-80**, over 13, 10 and 31 frames. The relationship with sun angle is flat to slightly
*improving*, not degrading.

**If wrong.** Beyond 80 degrees we have no matched data at all, so the claim is "no evidence of
degradation inside 63-80", not "accurate to 85".

### C16. Nothing is on by default

**Chosen.** A new `surface_reflectance` product for the physical quantity, and an
`atmospheric_correction: "none" | "gas" | "rayleigh" | "full"` option on `truecolor` that defaults to
`"none"`. The frozen Sentinel-3 defaults - masks, value ranges, colour ramps, gamma - are untouched.

---

## 6. Questions for the expert

Ordered by how much the answer would change.

**E1. Two bands disagree with Synergy by 0.012, and we cannot tell which of us is wrong.**
Turning the comparison round - asking what path reflectance Synergy's answer implies, and
subtracting the Rayleigh part we model - leaves what must be the aerosol path reflectance. It is
positive and smoothly falling from 400 to 510 nm, consistent with the measured optical depth, and
near zero from 665 to 1020. It is **negative at exactly 560 and 620 nm**, by -0.0102 and -0.0095,
which is physically impossible. Both bands would need 1.7x more ozone absorption, which would mean a
column of 485 DU where the product says 285. Synergy's own spectrum there reads 0.0451 at 510,
0.0697 at 560, **0.0701 at 620** and 0.0609 at 665 - a 620 nm value level with the green peak, where
chlorophyll absorption should put it well below. *Is this a known feature of the Synergy surface
reflectance in these bands, or is there a term we are missing that acts only where ozone absorption
is strong?*

**The aerosol tier has now sharpened this into the sharpest result we have.** Correcting for the
measured aerosol makes 560 nm worse on **20 of 20** frames while making 490 nm better on 19 of 20
and every water band better on all 19. Those frames span optical depth 0.012 to 0.288 and solar
zenith 63 to 79 degrees. A correction whose magnitude or spectral slope was wrong would have to
scatter across that range; a reference-side offset of fixed size would behave exactly like this.
We think the evidence now points at Synergy's 560 nm, and we would rather be told we are wrong
than ship on our own reading of it.

**E2. Can a Rayleigh-and-aerosol correction over water be called anything useful?**
Over water the atmosphere is up to **195 times** the water-leaving signal (residual 0.063 against a
signal of 0.0037 at 490 nm). After the full inversion the residual is *larger* than after a crude
subtraction, because the inversion correctly attributes the leftover aerosol path to the surface and
then divides by a transmittance of 0.75. The aerosol tier improves this on every band of every
frame - 0.0668 to 0.0511 at 490 nm - but "improved by a quarter" still leaves a residual **fourteen
times the signal**, and the two quantities are not the same thing in any case: our surface
reflectance includes the air-water interface, while water-leaving reflectance is defined below it.
*Should we ship a water product at all, with its uncertainty stated, or restrict surface
reflectance to land and point water users at the Level-2 products?*

**E3. The true-colour rendering, now that the reflectance changes.**
The frozen value range `0 - 0.6` and `gamma 0.6` were chosen by eye on pictures that were later
found to be 2.1x too blue, from a solar-flux indexing bug. They are still frozen. After correction,
a blue band is roughly a third of its top-of-atmosphere value. *What range and tone curve should a
corrected true-colour composite use?*

**E4. Are two aerosol models enough for this region?** See C11. *Is a Continental-to-Maritime
interpolation on the measured Angstrom exponent adequate for Nordic autumn, or do absorbing types -
long-range smoke, Arctic haze - need to be in the set? And is the fine-mode fraction a usable proxy
for the phase function when the single-scattering albedo field is empty?*

**E5. The aerosol product covers 6 to 58 % of its cells.** See C13. *Is filling unmeasured ground
with the granule median acceptable, or should the correction refuse there - and if it fills, what
should the per-pixel uncertainty be, given the product's own uncertainty field is also empty?*

**E6. Is a sea-level-only aerosol table acceptable?** See C12. *Over what elevation does the
boundary-layer assumption stop holding well enough for 0.01 in reflectance?*

**E7. The water-vapour collapse is inherited from 6SV.** See C5. *Is transmittance-as-a-function-of-
scaled-path-amount accurate enough for the OLCI water-vapour bands, or does the 709/885/900 nm
region need a two-dimensional treatment?*

**E8. Both our references are processor outputs sharing our own Level-1 radiance.** See C1. *What
independent reference should this be judged against - AERONET-collocated retrievals, in-situ
reflectance, something else - and does one exist for this region and season?*

---

## 7. Where the numbers come from

Everything above is reproducible from the mounted archive with no downloads:

| command | what it measures |
|---|---|
| `run.sh pairs` | the matched triples available |
| `run.sh probe n` | the geometry of the first n |
| `run.sh budget land` / `water` / `lowsun` | the reflectance budget per band against ESA's Level-2, both tiers |
| `run.sh sweep n` | residual against solar zenith over n triples, both tiers (C15) |
| `run.sh spectral` | the implied aerosol path across Synergy's sixteen bands (E1) |
| `run.sh aerosol` | what the aerosol product holds over the land frame (C13) |
| `run.sh tooling` | the radiative-transfer tooling present |
| `docker run ... tools/rt6s/check.py` | that 6SV still reproduces its reference case |
| `docker run ... tools/rt6s/offgrid.py` | what the tables cost *between* their rows: the off-grid reference points, the azimuth step (C7) and the sea-level aerosol assumption (C12) |

A caveat about reproducibility that cost us a frame: **the archive is a rolling window of about
seven days.** The water frame these numbers were first measured on had its Level-1 product deleted
while its Level-2 products remained, so the acquisition went half-present rather than absent. The
water case is now `20261031T120105` at solar zenith 79.5, and `run.sh` refuses an expired frame
instead of reading empty paths.

The plan, with the audit findings and the phase-by-phase record, is
`PLANNING/TODO_PLANNING_atmospheric_correction.md`.
