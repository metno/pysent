"""How wrong the corrected reflectance is, per pixel, measured rather than derived.

This module exists because of one decision and one near-miss.

The decision is the owner's answer to the plan's Q5: the ``surface_reflectance``
product returns a value on **every** clear pixel, including clear open ocean
where the error is several times the quantity being measured, and publishes the
error beside it. Nothing refuses a pixel for being too dark. That makes this
band the only thing standing between a user and a misreading, which is why it is
a required output rather than an optional extra.

The near-miss is how it is computed. The textbook route is to propagate the
aerosol loading's uncertainty through the forward model. Measured at the
validation frame's own geometry, the sensitivity of surface reflectance to the
loading is about **-0.10 per unit optical depth at 490, 560 and 665 nm alike**,
so even a 0.03 error in the depth - four times the retrieval's measured bias -
moves the answer by **0.0032** at 490 nm. The residual actually measured there
against ``OL_2_WFR`` is **0.0300**. A propagated uncertainty would therefore
stamp every open-ocean pixel with a confident +-0.003 that is ten times too
small, carrying all the authority of a derivation.

So the figure published here is **empirical: the agreement with ESA's own
Level-2 on matched pixels**, looked up by band, surface class and which tier
supplied the aerosol loading, with the propagated loading term added in
quadrature rather than standing in for it. It is an agreement with a reference,
not a theoretical error budget, and the product's tags say so in those words.

Two rules follow from not wanting to overstate what has been measured:

- Where no validation figure covers a pixel's combination, the uncertainty is
  **NaN** and the pixel is flagged. "Unmeasured" and "small" are different
  claims, and a filled-in number would erase the difference.
- At a band the retrieval itself fits, the residual against a reference that is
  also near zero does not measure accuracy - it is close to a tautology. Those
  bands take the figure from the nearest band the retrieval does *not* fit, and
  are flagged as inferred.

**Over a bright surface the figure is an extrapolation, and the quality band's
``bright_surface`` bit is how a reader can tell.** Every residual below was
measured on pixels the product does *not* call bright, because that is where
both reference products have a value - so sea ice, snow and cloud are outside
the sample entirely. The argument for using these numbers there anyway is that
the error is an absolute one contributed by the atmosphere and so barely depends
on the target's brightness, which is also why the plan's Q5 came out the way it
did. That argument is reasoning rather than measurement, and one term in it can
be checked: the surface coupling ``1 / (1 - S * rho_s)``, which is 1.004 over
dark water and 1.106 at a reflectance of 0.8. Measured through the real
inversion, a 5 % error in the spherical albedo moves the answer by **0.00001 at
490 nm over dark water and 0.00418 over ice** - four hundred times more, but
still an order of magnitude below the 0.039 published for that class, so it does
not by itself make the figure an understatement. What other terms do over ice is
not known here, and measuring it is what the Arctic frames are for.
"""
from __future__ import annotations

import numpy as np

from ._aerosol_retrieval import RETRIEVAL_BANDS

# Which bands the near-infrared retrieval drives to agreement by construction.
# A residual at one of these against a reference that is itself near zero is not
# a measurement of accuracy, so these bands never take their own figure when the
# loading was retrieved - see ``INFERRED_FROM``.
FITTED_BANDS = frozenset(RETRIEVAL_BANDS)

# The band whose measured residual stands in for a fitted band's. 560 nm is the
# nearest band the retrieval does not look at, so it is the closest honest
# estimate available for 665 nm. It is an inference and is flagged as one.
INFERRED_FROM = 6

# **The 68th percentile of the absolute disagreement with ESA's own Level-2**,
# by surface class and band, measured end to end through the shipped
# ``correct_frame`` across the holding (Phase 6): 25 land, 16 bright and 14
# water comparisons over both platforms, including Arctic frames, on the
# instrument grid so no warp enters the comparison.
#
# Water and bright are against ``OL_2_WFR``, land against ``SY_2_SYN``
# ``SDR_Oa**``. Both references are independently produced by ESA from the same
# Level-1 radiance; neither is derived from anything this library computes.
#
# **p68, not a median, and the distinction is the whole point.** A median
# absolute residual is exceeded by half of all pixels by construction, so
# publishing one as an uncertainty guarantees it is wrong half the time. The
# first version of this table did worse still: it was filled with
# ``median(ours - reference)``, a *bias*, which at 490 nm over land is 0.0031
# where the typical absolute error is 0.0221 - seven times larger. Measured
# coverage then ran at 8 to 15 % where a one-sigma band should reach 68 %.
#
# These figures already include every term the chain commits, the climatological
# aerosol fill included, because they were measured through the shipped path
# with that fill in place. Nothing may be added on top of them: see the note at
# the end of ``uncertainty`` about double-counting.
VALIDATION_RESIDUAL: dict[str, dict[int, float]] = {
    # Open water, where the correction is worth least relative to the signal:
    # ESA reads 0.004 at 490 nm and we disagree by ten times that.
    "water": {4: 0.0486, 6: 0.0252, 8: 0.0094},
    # Bright: sea ice, snow and cloud. **These are the rows that did not exist
    # before Phase 6**, and their absence is why an ice pixel was previously
    # handed the land figure and under-stated by a factor of ten.
    "bright": {4: 0.0696, 6: 0.0984, 8: 0.0759},
    "land": {4: 0.0329, 6: 0.0419, 8: 0.0294},
}

#: Where each row was measured, for the provenance tags.
VALIDATION_SOURCE: dict[str, str] = {
    # Named by its product type rather than by the quantity it carries: the
    # phrase that quantity goes by must not appear in this product's tags, and
    # OL_2_WFR identifies it without ambiguity anyway.
    "water": "OL_2_WFR over clear water, 14 comparisons, p68",
    "bright": "OL_2_WFR over bright pixels, 16 comparisons, p68",
    "land": "SY_2_SYN SDR, 25 comparisons, p68",
}

# ``gas`` has no validation row and reports NaN. Every end-to-end residual in
# this work includes the Rayleigh term, which over water is the largest single
# term there is, so lending that tier these figures would understate it by most
# of the Rayleigh path reflectance - 0.10 at 490 nm. The tier stays available
# because it isolates one term for diagnosis; it does not get to claim an
# accuracy.
UNMEASURED_TIERS = frozenset({"gas"})


def surface_classes(land: np.ndarray, bright: np.ndarray | None = None) -> dict:
    """The three surface classes the validation was measured in.

    **Bright is a class of its own, and Phase 6 is why.** Before it, pixels were
    split by the product's land flag alone, so sea ice over ocean took the water
    row and ice over land took the land row. Measured, the correction is 1.4
    times worse at 490 nm over bright surfaces than over clear water and nearly
    ten times worse at 665, so either borrowing understated an ice pixel badly -
    at 665 nm by a factor of ten.

    Land against water remains the split for the two clear classes because that
    is how the reference products divide: ``SY_2_SYN`` over land, ``OL_2_WFR``
    over water.
    """
    land = np.asarray(land, dtype=bool)
    if bright is None:
        bright = np.zeros(land.shape, dtype=bool)
    bright = np.asarray(bright, dtype=bool)
    return {
        "bright": bright,
        "water": ~land & ~bright,
        "land": land & ~bright,
    }


def loading_sensitivity(
    platform: str,
    band: int,
    *,
    toa,
    optical_depth_550,
    angstrom,
    geometry: dict,
    step: float = 0.02,
):
    """How much the surface reflectance moves per unit optical depth, per pixel.

    A centred difference through the real forward model rather than an analytic
    derivative, because the path reflectance is piecewise linear in depth with a
    kink at every table node: a derivative would be exact on one side of a node
    and wrong on the other, while a difference straddling it gives the average
    slope that actually matters over the uncertainty's own width.

    This is the term that gets added in quadrature to the measured residual. It
    is the *small* part - about 0.10 per unit depth, so a tenth of the measured
    disagreement at a plausible loading error - and the module docstring explains
    why it must not be mistaken for the whole.
    """
    from ._atmosphere import atmosphere_terms, surface_from_toa

    depth = np.asarray(optical_depth_550, dtype=np.float64)
    toa = np.asarray(toa, dtype=np.float64)
    low = np.clip(depth - step, 0.0, None)
    high = depth + step
    surfaces = []
    for value in (low, high):
        terms = atmosphere_terms(
            platform, band, optical_depth_550=value, angstrom=angstrom, **geometry
        )
        surfaces.append(surface_from_toa(toa, **terms))
    with np.errstate(divide="ignore", invalid="ignore"):
        return (surfaces[1] - surfaces[0]) / (high - low)


def uncertainty(
    band: int,
    *,
    land: np.ndarray,
    tier: str,
    bright: np.ndarray | None = None,
    aerosol_source: np.ndarray | None = None,
    depth_uncertainty=None,
    sensitivity=None,
):
    """The per-pixel absolute uncertainty, and why each pixel got the figure it did.

    Indexed by **surface class** rather than by aerosol tier. The tier split was
    a guess at what would matter and Phase 6 measured what actually does: the
    surface. The published figures were taken end to end through the shipped
    correction with its own climatological fill in place, so they already
    contain whatever that fill costs.

    Returns the uncertainty array and boolean reason masks, so a caller can
    report *why* a pixel has no figure rather than only that it has none.
    """
    land = np.asarray(land, dtype=bool)
    shape = land.shape
    values = np.full(shape, np.nan, dtype=np.float64)
    inferred = np.zeros(shape, dtype=bool)
    unmeasured = np.zeros(shape, dtype=bool)

    if tier in UNMEASURED_TIERS:
        return values, {
            "inferred_from_neighbour": inferred,
            "unmeasured": np.ones(shape, dtype=bool),
        }

    retrieved = None
    if aerosol_source is not None:
        retrieved = np.asarray(aerosol_source) == 0

    for class_name, in_class in surface_classes(land, bright).items():
        if not in_class.any():
            continue
        row = VALIDATION_RESIDUAL.get(class_name)
        if row is None:
            unmeasured |= in_class
            continue
        # A band the retrieval fits cannot measure its own accuracy: it was
        # driven to agree with a reference that is itself near zero, so its
        # residual understates the error. Only the pixels whose loading really
        # was retrieved are affected.
        # Only a fitted band, and only where its loading really was retrieved.
        # Starting this at anything other than "nothing" sends every ordinary
        # band down the inference path.
        fitted = np.zeros(shape, dtype=bool)
        if band in FITTED_BANDS and retrieved is not None:
            fitted = in_class & retrieved
            inferred |= fitted
        plain = in_class & ~fitted
        for where, which in ((plain, band), (in_class & fitted, INFERRED_FROM)):
            if not where.any():
                continue
            if which not in row:
                unmeasured |= where
                continue
            values = np.where(where, row[which], values)

    # The loading's own uncertainty, propagated. This is the one term that may
    # be added, because it describes a *different* loading from the one the
    # figures above were measured with - a caller supplying SY_2_AOD's
    # AOD_*_uncertainty is describing their own input, not ours.
    if depth_uncertainty is not None and sensitivity is not None:
        propagated = np.abs(np.asarray(sensitivity, dtype=np.float64)) * np.abs(
            np.asarray(depth_uncertainty, dtype=np.float64)
        )
        values = np.where(
            np.isfinite(values) & np.isfinite(propagated),
            np.sqrt(values**2 + propagated**2),
            values,
        )

    # Nothing else is added. The tables' interpolation error, the scalar
    # Rayleigh error and the gas coefficients' error are all already inside
    # these numbers, because the numbers are end-to-end disagreements measured
    # through the chain that commits them. Adding any of them again
    # double-counts it.
    unmeasured |= ~np.isfinite(values)
    return values, {"inferred_from_neighbour": inferred, "unmeasured": unmeasured}


def uncertainty_note(tier: str) -> str:
    """One line for the provenance tags, naming what the figure actually is.

    The wording matters as much as the number. "Empirical agreement with ESA
    Level-2" is a claim a reader can check and bound; "uncertainty" alone
    invites them to read it as a propagated error budget, which it is not.
    """
    if tier in UNMEASURED_TIERS:
        return f"not measured for the {tier!r} tier; the band is NaN throughout"
    return (
        "p68 of the absolute disagreement with ESA Level-2 on matched pixels, "
        "measured through this correction end to end; not a propagated error "
        "budget. From " + "; ".join(VALIDATION_SOURCE[name]
                                    for name in ("water", "bright", "land"))
    )
