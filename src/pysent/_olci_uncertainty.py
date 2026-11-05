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

# Measured median absolute residual against ESA's own Level-2, by surface class,
# aerosol tier and band. **These are agreements with a reference on matched
# pixels, not propagated error budgets.** Each row names the reference, the
# sample and where the number comes from, because a figure this load-bearing has
# to be traceable to the run that produced it.
#
# Water is against ``OL_2_WFR`` water-leaving reflectance over clear water;
# land is against ``SY_2_SYN`` ``SDR_Oa**`` over clear land. The two references
# are independent of each other and of anything this library produces.
#
# Phase 6 of the plan re-measures all of these across the whole holding and by
# solar zenith; until it does, the land rows are the 55-triple sweep and the
# water rows are the single repinned water frame, which is why the sample size
# travels with the number.
VALIDATION_RESIDUAL: dict[tuple[str, str], dict[int, float]] = {
    # Gas and Rayleigh, no aerosol tier. This is the ``"rayleigh"`` correction
    # tier, and it is deliberately *not* reused for the ``"gas"`` tier below.
    ("water", "rayleigh"): {4: 0.0592, 6: 0.0439, 8: 0.0304},
    # The loading from an ESA product - the coarse SY_2_AOD frame value, or
    # OL_2_WFR's per-pixel w_aer.
    ("water", "product"): {4: 0.0391, 6: 0.0239, 8: 0.0124},
    # The loading retrieved from this frame's own near-infrared.
    ("water", "retrieved"): {4: 0.0300, 6: 0.0142, 8: 0.0037},
    ("land", "rayleigh"): {4: 0.0107, 6: 0.0117, 8: 0.0035},
    ("land", "product"): {4: 0.0031, 6: 0.0175, 8: 0.0034},
}

# Where each row above was measured, for the provenance tags.
VALIDATION_SOURCE: dict[tuple[str, str], str] = {
    ("water", "rayleigh"): "OL_2_WFR, 1 frame, gas+Rayleigh",
    ("water", "product"): "OL_2_WFR w_aer per pixel, 1 frame",
    ("water", "retrieved"): "OL_2_WFR, 1 frame, loading retrieved from the frame",
    ("land", "rayleigh"): "SY_2_SYN SDR, 55 triples, gas+Rayleigh",
    ("land", "product"): "SY_2_SYN SDR, 55 triples, SY_2_AOD loading",
}

# ``gas`` has no validation row and reports NaN. It is a correction tier nothing
# was ever validated as a *product*: every end-to-end residual in the plan
# includes the Rayleigh term, which over water is the largest single term there
# is. Lending it the gas+Rayleigh row would understate it by most of the
# Rayleigh path reflectance - 0.10 at 490 nm - and that is a far bigger lie than
# the propagation trap this module exists to avoid. The tier stays available
# because it isolates one term for diagnosis; it simply does not get to claim an
# accuracy.
UNMEASURED_TIERS = frozenset({"gas"})

# The climatological fallback has no validation row of its own either - no frame
# in the holding was corrected with a stated assumption standing in for its own
# measured loading. But unlike the gas tier it is **not** unmeasurable, and
# leaving it NaN was wasteful rather than honest: on the first real frame it put
# 67 % of the output beyond the uncertainty band's reach, which over land is
# most of what the product is for.
#
# What it costs is the difference between a measured loading and the stated one,
# and both halves of that are measured:
#
# - Across the 47 frames of the holding with a reported ``AOD_550``, the
#   absolute distance from the stated 0.044 has a 68th percentile of **0.0346**
#   in optical depth. (Median 0.018, p95 0.131, full range 0.012 to 0.288.)
# - The sensitivity of surface reflectance to optical depth is **0.10 per unit
#   depth**, flat to within 10 % across the three visible bands.
#
# So the stand-in costs about **0.0035** of reflectance, one sigma, and a
# climatological pixel reports its surface class's measured ``product`` row with
# that added in quadrature. This is the one place a propagated term is the whole
# of a contribution rather than a tenth of it, because here the loading error
# *is* the thing being described rather than an afterthought beside a reference
# disagreement.
CLIMATOLOGICAL_DEPTH_SPREAD = 0.0346
DEPTH_SENSITIVITY = 0.10
CLIMATOLOGICAL_EXTRA = CLIMATOLOGICAL_DEPTH_SPREAD * DEPTH_SENSITIVITY

# Which measured row a tier without one of its own borrows, and what it must add
# in quadrature for the borrowing to be honest.
BORROWED_ROWS: dict[str, tuple[str, float]] = {
    "climatological": ("product", CLIMATOLOGICAL_EXTRA),
}


def surface_classes(land: np.ndarray) -> dict[str, np.ndarray]:
    """The two surface classes the validation is measured in.

    Deliberately only two, and deliberately the product's own land flag rather
    than a brightness test. The plan's Q5 established that the useful axis for
    *interpreting* the error is bright against dark, but the axis the error was
    *measured* along is land against water, because that is how the two
    reference products divide. Classifying by brightness here would index the
    table with something it was not measured against.
    """
    land = np.asarray(land, dtype=bool)
    return {"land": land, "water": ~land}


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
    aerosol_source: np.ndarray | None = None,
    depth_uncertainty=None,
    sensitivity=None,
):
    """The per-pixel absolute uncertainty, and why each pixel got the figure it did.

    ``tier`` is the correction tier actually applied (``"none"``, ``"gas"``,
    ``"rayleigh"`` or ``"full"``); ``aerosol_source`` is the per-pixel integer
    from :func:`pysent._aerosol_retrieval.filled_loading`, present only for
    ``"full"``.

    Returns the uncertainty array and a dict of boolean reason masks, so a
    caller can report *why* a pixel has no figure instead of only that it has
    none.
    """
    land = np.asarray(land, dtype=bool)
    shape = land.shape
    values = np.full(shape, np.nan, dtype=np.float64)
    inferred = np.zeros(shape, dtype=bool)
    unmeasured = np.zeros(shape, dtype=bool)

    # Which validation row each pixel indexes. Without an aerosol tier every
    # pixel is on the "none" row; with one, the row follows that pixel's own
    # loading source, because a retrieved and a guessed loading are not equally
    # trustworthy and must not share a figure.
    if tier == "full" and aerosol_source is not None:
        source = np.asarray(aerosol_source)
        tiers = {name: source == code for code, name in
                 ((0, "retrieved"), (1, "product"), (2, "climatological"))}
    else:
        tiers = {tier: np.ones(shape, dtype=bool)}

    for tier_name, selected in tiers.items():
        if not selected.any():
            continue
        if tier_name in UNMEASURED_TIERS:
            unmeasured |= selected
            continue
        # A tier with no row of its own borrows one, and pays for the borrowing
        # with a measured term added in quadrature.
        row_tier, extra = BORROWED_ROWS.get(tier_name, (tier_name, 0.0))
        for class_name, in_class in surface_classes(land).items():
            where = selected & in_class
            if not where.any():
                continue
            row = VALIDATION_RESIDUAL.get((class_name, row_tier))
            if row is None:
                unmeasured |= where
                continue
            # A band the retrieval fits cannot measure its own accuracy.
            take = band
            if tier_name == "retrieved" and band in FITTED_BANDS:
                take = INFERRED_FROM
                inferred |= where
            if take not in row:
                unmeasured |= where
                continue
            values = np.where(where, np.hypot(row[take], extra), values)

    # The loading's own uncertainty, propagated - the quadrature partner, not
    # the headline. Absent where no loading uncertainty is available, which is
    # most of the time, since SY_2_AOD carries one and the retrieval does not.
    if depth_uncertainty is not None and sensitivity is not None:
        propagated = np.abs(np.asarray(sensitivity, dtype=np.float64)) * np.abs(
            np.asarray(depth_uncertainty, dtype=np.float64)
        )
        values = np.where(
            np.isfinite(values) & np.isfinite(propagated),
            np.sqrt(values**2 + propagated**2),
            values,
        )

    # The aerosol table's own interpolation error is deliberately **not** added
    # here, and the reason is easy to get backwards. It looks like a missing
    # term - ``aerosol_terms`` reports it per pixel, and an uncertainty should
    # be conservative. But the figures above are residuals measured *through
    # these same tables*: every interpolation error the tables commit was
    # already present in the validation run that produced the number. Adding it
    # again would double-count it. The same argument rules out adding the
    # scalar-Rayleigh error and the gas coefficients' error: a measured
    # end-to-end disagreement already contains every term the chain commits.
    unmeasured |= ~np.isfinite(values)
    return values, {"inferred_from_neighbour": inferred, "unmeasured": unmeasured}


def uncertainty_note(tier: str) -> str:
    """One line for the provenance tags, naming what the figure actually is.

    The wording matters as much as the number. "Empirical agreement with ESA
    Level-2" is a claim a reader can check and bound; "uncertainty" on its own
    invites them to read it as a propagated error budget, which it is not.
    """
    if tier in UNMEASURED_TIERS:
        return f"not measured for the {tier!r} tier; the band is NaN throughout"
    wanted = ("product", "retrieved") if tier == "full" else ("rayleigh",)
    rows = sorted(
        source
        for (_, tier_name), source in VALIDATION_SOURCE.items()
        if tier_name in wanted
    )
    return (
        "empirical agreement with ESA Level-2 on matched pixels, not a propagated "
        "error budget; measured from " + "; ".join(rows)
    )
