"""Retrieve the aerosol loading from the frame itself, when no product has it.

:mod:`._atmosphere` evaluates the forward model: given an optical depth, what
does the atmosphere do? This inverts it over water: given what the sensor
measured, what optical depth explains it? That is what lets the correction run
on a Level-1 frame alone, with no aerosol product alongside.

The method is the near-infrared **black-pixel** assumption. Clear water leaves
almost nothing at 865 nm, so every photon arriving there is atmosphere, and the
optical depth is whatever reproduces the measurement. Checked rather than
assumed: over the archive's clear water ESA's own water-leaving reflectance at
865 nm has a median of **0.0001** and a 95th percentile of 0.0009, and at
665 nm 0.0003.

Three things about this inversion are counter-intuitive and all three were
measured, not reasoned:

* **Two bands beat one only if the spectral slope is allowed to float.** With
  the Angstrom exponent supplied per pixel from ESA's own ``A865``, inverting
  665 and 865 together is *worse* than inverting 865 alone - bias +0.043
  against +0.020 in optical depth at 865 nm. Fitting the slope instead brings
  it to **+0.006** at a correlation of 0.84. 665 nm carries a larger molecular
  path and a relatively larger surface term, so holding the slope forces that
  contamination into the depth; letting it float puts it in the slope.
* **So the fitted slope is a nuisance parameter, not a measurement.** It
  correlates with ESA's own exponent at only 0.37 and its median is wrong by
  more than a factor of two. :func:`retrieve_optical_depth` returns it for
  provenance and labels it as a fit parameter. Do not publish it as an aerosol
  property.
* **The assumption is sound about the water and wrong about the surface.**
  Taking ESA's own loading as truth and subtracting the path it implies leaves
  **+0.0048** of reflectance at 865 nm - fifty times the water-leaving
  reflectance there. That is the air-water interface, sun glint, whitecaps and
  model mismatch, and a black-pixel retrieval hands all of it to the aerosol.
  It is why the retrieved depth is biased high at all, and the bias is stated
  rather than hidden.

**This refuses over anything bright.** Sea ice, snow and cloud reflect 0.3 to
0.9 in the near infrared, where this method assumes zero, so a retrieval there
would return a large confident nonsense. OLCI Level-1 has no ice flag - ice,
snow and cloud all arrive as ``bright`` - so ``bright`` is the line, and the
caller passes the mask.
"""
from __future__ import annotations

import numpy as np

from ._atmosphere import (
    _aerosol_grids,
    _model_weights,
    aerosol_grid_limits,
    aerosol_models,
    aerosol_path_components,
    gas_transmittance,
    rayleigh_terms,
)

#: The two bands the inversion uses: 665 and 865 nm. Both are nearly black over
#: clear water, and their separation - ``ln(865/665)`` is 0.262 - is wider than
#: 865 against the 1020 nm band's 0.165, so this pair has more spectral
#: leverage than adding a third band at 1020 would. It is also the pair the
#: committed aerosol table already covers.
RETRIEVAL_BANDS = (8, 17)

#: The optical depths the forward model is evaluated at, and **the inversion
#: needs no more than these**. The path reflectance is piecewise linear in
#: optical depth between the table's stored nodes - that is what the forward
#: model interpolates - with an exact anchor at zero, where an atmosphere with
#: no aerosol contributes no aerosol path. So evaluating at the breakpoints and
#: inverting the piecewise-linear function gives the answer *continuously* and
#: exactly, where searching a dense grid gives it snapped to the grid and
#: costs fifteen times as much. Filled from the table on first use.
_BREAKPOINTS: np.ndarray | None = None

#: Angstrom exponents to fit over. Wide enough to span coarse marine aerosol at
#: about 0.3 and fine continental smoke above 1.5, and fitted rather than
#: believed - see the module docstring.
ANGSTROM_STEPS = np.arange(0.0, 1.801, 0.1)

#: What to assume where nothing can be retrieved and no product is at hand: the
#: median of this archive's own measured loading, and ESA's own median exponent
#: over its water. **A stated assumption, not a measurement**, carried into
#: provenance as such, and deliberately not zero - assuming clean air where the
#: archive is typically at 0.044 would under-correct every such pixel.
CLIMATOLOGICAL_DEPTH_550 = 0.044
CLIMATOLOGICAL_ANGSTROM = 0.55


def breakpoints(platform="S3A", band=RETRIEVAL_BANDS[-1]):
    """The optical depths the inversion evaluates at: zero plus the table's."""
    global _BREAKPOINTS
    if _BREAKPOINTS is None:
        low, high = aerosol_grid_limits(platform, band)["optical_depth_550"]
        axes = _aerosol_grids()[(platform, int(band), aerosol_models(platform)[0])][0]
        stored = np.asarray(axes[0], dtype=np.float64)
        _BREAKPOINTS = np.concatenate([[0.0], stored])
        if not (low == stored[0] and high == stored[-1]):  # pragma: no cover
            raise ValueError("the aerosol table's depth axis disagrees with its limits")
    return _BREAKPOINTS


def _path_pieces(platform, band, *, shape, geometry):
    """Everything needed to build the breakpoint paths at **any** exponent.

    The exponent search is the whole cost of this module, and the exponent
    changes only how the two aerosol models are weighted - every table
    interpolation underneath is exponent-independent. So the expensive part is
    done once here, and :func:`_blend_paths` assembles a given exponent's paths
    from it with arithmetic alone.

    Measured before this split: 38 calls to the blended forward model, 2204
    calls to the trilinear interpolator, 180 s for 100 000 pixels. The pieces
    below are 2 interpolations per band instead of 19, plus the molecular term
    twice instead of 38 times.
    """
    nodes = breakpoints(platform, band)
    depth = nodes.reshape((-1,) + (1,) * len(shape)) * np.ones((1, *shape))
    broadcast = {
        name: np.broadcast_to(np.asarray(value, dtype=np.float64), (nodes.size, *shape))
        for name, value in geometry.items()
    }
    components = aerosol_path_components(
        platform, band,
        optical_depth_550=depth,
        solar_zenith_deg=broadcast["solar_zenith_deg"],
        view_zenith_deg=broadcast["view_zenith_deg"],
        relative_azimuth_deg=broadcast["relative_azimuth_deg"],
    )
    # The molecular path at the pixel's own pressure, which is what the
    # aerosol increment is added to. Distinct from the sea-level molecular path
    # the table carries, which is what the increment has subtracted from it.
    molecular_at_pressure = rayleigh_terms(
        platform, band,
        pressure_hpa=broadcast["pressure_hpa"],
        solar_zenith_deg=broadcast["solar_zenith_deg"],
        view_zenith_deg=broadcast["view_zenith_deg"],
        relative_azimuth_deg=broadcast["relative_azimuth_deg"],
    )["path_reflectance"]
    return {
        "platform": platform,
        "model_path": components["model_path"],
        "molecular_sea_level": components["molecular_path"],
        "molecular_at_pressure": molecular_at_pressure,
        "scale": components["scale"],
    }


def _blend_paths(pieces, angstrom):
    """One exponent's breakpoint paths, from the pre-computed pieces.

    Identical arithmetic to :func:`pysent._atmosphere.atmosphere_terms` over the
    same inputs - the models blended on the exponent, the sea-level molecular
    part removed, the below-floor ramp applied, and the result added to the
    molecular path at the pixel's own pressure.
    """
    weights = _model_weights(pieces["platform"], angstrom)
    blended = 0.0
    for model, weight in weights.items():
        blended = blended + weight * pieces["model_path"][model]
    increment = pieces["scale"] * (blended - pieces["molecular_sea_level"])
    return pieces["molecular_at_pressure"] + increment


def _interpolate_at(nodes, paths, depth):
    """``paths`` evaluated at ``depth``, piecewise linearly over ``nodes``."""
    depth = np.asarray(depth, dtype=np.float64)
    out = np.full(depth.shape, np.nan)
    for index in range(nodes.size - 1):
        low, high = nodes[index], nodes[index + 1]
        inside = (depth >= low) & (depth <= high)
        fraction = (depth - low) / (high - low)
        out = np.where(
            inside & ~np.isfinite(out),
            paths[index] + fraction * (paths[index + 1] - paths[index]),
            out,
        )
    return out


def _invert_piecewise(nodes, paths, measured):
    """The depth whose path matches ``measured``, by piecewise-linear inverse.

    ``paths`` is ``(node, *shape)`` and increases with the node, since more
    aerosol never darkens the path. Returns the depth, and NaN where the
    measurement falls outside what the nodes span - below the first means
    darker than clean air, above the last means brighter than the table
    explains, and both are the caller's to interpret.
    """
    measured = np.asarray(measured, dtype=np.float64)
    depth = np.full(measured.shape, np.nan)
    for index in range(nodes.size - 1):
        low, high = paths[index], paths[index + 1]
        span = high - low
        inside = (measured >= low) & (measured <= high) & (span > 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            fraction = (measured - low) / span
        depth = np.where(
            inside & ~np.isfinite(depth),
            nodes[index] + fraction * (nodes[index + 1] - nodes[index]),
            depth,
        )
    return depth


def retrieve_optical_depth(
    platform: str,
    *,
    toa,
    pressure_hpa,
    solar_zenith_deg,
    view_zenith_deg,
    relative_azimuth_deg,
    ozone_kg_m2,
    water_kg_m2,
    usable,
    bands=RETRIEVAL_BANDS,
):
    """Optical depth at 550 nm from the frame's own near-infrared bands.

    ``toa`` maps band number to top-of-atmosphere reflectance and must contain
    both of ``bands``. ``usable`` is the mask of pixels the inversion may run
    on: **clear water only**, so land, cloud, ice and every other bright or
    rejected pixel must already be excluded by the caller. Everything else is
    the geometry and the gas columns, as :func:`._atmosphere.atmosphere_terms`
    takes them.

    Returns a dict:

    ``optical_depth_550``
        The retrieval, NaN wherever it was refused.
    ``angstrom``
        The **fitted** exponent - a nuisance parameter that protects the depth,
        not a measurement of the aerosol. See the module docstring.
    ``residual``
        What is left unexplained at the second band after the fit, in
        reflectance. Around 0.0026 is normal; much more means the two bands
        could not be reconciled and the depth should be distrusted.
    ``refused``
        A dict of named boolean masks saying *why* each pixel has no value, so
        a quality band can report the reason rather than only the absence.
    """
    bands = tuple(int(band) for band in bands)
    missing = [band for band in bands if band not in toa]
    if missing:
        raise ValueError(
            f"the retrieval needs top-of-atmosphere reflectance for bands {bands}; "
            f"{missing} were not given"
        )

    usable = np.asarray(usable, dtype=bool)
    shape = usable.shape
    geometry = dict(
        pressure_hpa=pressure_hpa,
        solar_zenith_deg=solar_zenith_deg,
        view_zenith_deg=view_zenith_deg,
        relative_azimuth_deg=relative_azimuth_deg,
        ozone_kg_m2=ozone_kg_m2,
        water_kg_m2=water_kg_m2,
    )

    # Divide the gas out once per band: the inversion is against the aerosol
    # and molecular path, and multiplying a gas correction into it would count
    # absorption twice.
    apparent = {}
    for band in bands:
        transmittance = gas_transmittance(
            platform,
            band,
            ozone_kg_m2=ozone_kg_m2,
            water_kg_m2=water_kg_m2,
            solar_zenith_deg=solar_zenith_deg,
            view_zenith_deg=view_zenith_deg,
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            apparent[band] = np.asarray(toa[band], dtype=np.float64) / transmittance

    anchor, second = bands[-1], bands[0]

    # **Invert only the pixels that could survive.** The forward model is
    # evaluated once per exponent per band, and on a real frame roughly two
    # thirds of the pixels are land, cloud or ice that the refusal masks below
    # throw away afterwards - so running the whole frame spends most of its time
    # computing answers in order to discard them. Measured on a quicklook frame
    # the retrieval dominated everything else by forty to one, and this is the
    # cheap half of that.
    #
    # The masks are still assembled at full shape, so a refused pixel still says
    # why: what is narrowed is the arithmetic, not the reporting.
    candidate = (
        usable & np.isfinite(apparent[anchor]) & np.isfinite(apparent[second])
    )
    subset_shape = (int(np.count_nonzero(candidate)),)

    def scatter(values):
        """A result computed on the candidates, back at the frame's own shape."""
        full = np.full(shape, np.nan)
        full[candidate] = values
        return full

    best_cost = np.full(subset_shape, np.inf)
    best_depth = np.full(subset_shape, np.nan)
    best_angstrom = np.full(subset_shape, np.nan)
    best_residual = np.full(subset_shape, np.nan)
    cleanest = brightest = None
    sub_geometry = {
        name: np.asarray(value, dtype=np.float64)[candidate]
        if np.shape(value) == shape
        else np.full(subset_shape, float(np.asarray(value)))
        for name, value in geometry.items()
    }
    sub_apparent = {band: values[candidate] for band, values in apparent.items()}

    nodes = breakpoints(platform, anchor)
    # The tables, interpolated once. Everything the exponent search does from
    # here is arithmetic over these.
    pieces = (
        {
            band: _path_pieces(platform, band, shape=subset_shape, geometry=sub_geometry)
            for band in (anchor, second)
        }
        if subset_shape[0]
        else {}
    )
    for exponent in ANGSTROM_STEPS:
        if not subset_shape[0]:
            break
        alpha = np.full(subset_shape, float(exponent))
        anchor_paths = _blend_paths(pieces[anchor], alpha)
        second_paths = _blend_paths(pieces[second], alpha)
        if cleanest is None:
            # The path of an aerosol-free atmosphere at the anchor band, kept
            # to tell "no aerosol" from "darker than air", below. The first
            # breakpoint is zero, so this is exact rather than the lightest
            # haze the table happens to carry, and it does not depend on the
            # exponent - with no aerosol there is no spectral slope to have.
            cleanest = anchor_paths[0]
        # The brightest path any tabulated atmosphere can produce here, over
        # every exponent tried. A measurement above it cannot be explained by
        # aerosol at all, and the exact inversion returns NaN rather than
        # pinning to the ceiling - so without tracking this, such a pixel would
        # be refused with no reason given, which is the one outcome the reason
        # masks exist to prevent.
        brightest = anchor_paths[-1] if brightest is None else np.maximum(
            brightest, anchor_paths[-1]
        )

        # Invert the anchor band exactly, then score the depth it gives against
        # what the second band actually measured. The exponent that reconciles
        # the two wins - and is then kept as a fit parameter, not a
        # measurement; see the module docstring.
        depth = _invert_piecewise(nodes, anchor_paths, sub_apparent[anchor])
        predicted = _interpolate_at(nodes, second_paths, depth)
        here = np.abs(predicted - sub_apparent[second])
        here = np.where(np.isfinite(here), here, np.inf)

        better = here < best_cost
        best_cost = np.where(better, here, best_cost)
        best_depth = np.where(better, depth, best_depth)
        best_angstrom = np.where(better, float(exponent), best_angstrom)
        best_residual = np.where(better, here, best_residual)

    # Back to the frame's shape before the masks are assembled, so every pixel
    # is still reported on whether or not it was worth inverting. A pixel that
    # was never a candidate carries NaN here, and NaN fails every comparison
    # below - which is right: it is refused as not clear water or as missing
    # input, by the masks that actually apply to it.
    best_depth = scatter(best_depth)
    best_angstrom = scatter(best_angstrom)
    best_residual = scatter(best_residual)
    cleanest = scatter(cleanest) if cleanest is not None else np.full(shape, np.nan)
    brightest = scatter(brightest) if brightest is not None else np.full(shape, np.nan)

    # The top breakpoint is the table's own last node, so compare against it
    # with a tolerance: ``np.arange`` and float arithmetic put an exactly
    # pinned pixel a few ulps below the value it is pinned to.
    ceiling = float(nodes[-1]) - 1e-9
    refused = {
        # Not clear water: land, cloud, ice, anything bright or rejected. The
        # black-pixel assumption is not merely inaccurate there, it inverts to
        # nonsense, because ice and cloud reflect strongly where it assumes
        # nothing does.
        "not_clear_water": ~usable,
        # Darker than an aerosol-free atmosphere. Reporting zero here would be
        # a retrieval of "clean air" where the honest answer is that the
        # molecular model already over-explains the measurement.
        "darker_than_clean_air": usable & np.isfinite(apparent[anchor])
        & (apparent[anchor] < cleanest),
        # Pinned at the top of the search, so no tabulated atmosphere explains
        # the measurement. **Two very different pixels land here and both
        # should**: genuinely heavy haze beyond the table's 0.4, and a bright
        # surface - ice, snow, cloud - that the black-pixel assumption should
        # never have been pointed at. Either way the honest answer is that this
        # measurement is brighter than the table accounts for.
        "brighter_than_the_table_explains": usable
        & np.isfinite(apparent[anchor])
        & ((apparent[anchor] > brightest) | (best_depth >= ceiling)),
        # No measurement to invert.
        "missing_input": ~np.isfinite(apparent[anchor]) | ~np.isfinite(apparent[second]),
    }
    keep = usable.copy()
    for mask in refused.values():
        keep = keep & ~mask

    return {
        "optical_depth_550": np.where(keep, best_depth, np.nan),
        "angstrom": np.where(keep, best_angstrom, np.nan),
        "residual": np.where(keep, best_residual, np.nan),
        "refused": refused,
    }


def filled_loading(retrieved, *, product_depth=None, product_angstrom=None):
    """One loading field per pixel, and a record of where each value came from.

    The retrieval covers about two thirds of clear water and nothing else, so
    something has to stand in elsewhere. The order is measured first, stated
    assumption last:

    1. the retrieval, where it succeeded;
    2. a product's value, where one was supplied - over water that should be
       ``OL_2_WFR``'s own per-pixel ``w_aer``, which beats the coarse
       ``SY_2_AOD`` by a factor of two end to end;
    3. :data:`CLIMATOLOGICAL_DEPTH_550`, this archive's own median.

    Returns the two fields and a ``source`` array of small integers - 0
    retrieved, 1 from the product, 2 climatological - so provenance can state
    the fractions and a caller can refuse the third tier if it wants to.
    """
    depth = np.array(retrieved["optical_depth_550"], dtype=np.float64, copy=True)
    angstrom = np.array(retrieved["angstrom"], dtype=np.float64, copy=True)
    source = np.where(np.isfinite(depth), 0, 2).astype(np.int8)

    if product_depth is not None:
        from_product = ~np.isfinite(depth) & np.isfinite(product_depth)
        depth = np.where(from_product, product_depth, depth)
        if product_angstrom is not None:
            angstrom = np.where(from_product, product_angstrom, angstrom)
        source = np.where(from_product, 1, source).astype(np.int8)

    unfilled = ~np.isfinite(depth)
    depth = np.where(unfilled, CLIMATOLOGICAL_DEPTH_550, depth)
    angstrom = np.where(~np.isfinite(angstrom), CLIMATOLOGICAL_ANGSTROM, angstrom)
    source = np.where(unfilled, 2, source).astype(np.int8)
    return {"optical_depth_550": depth, "angstrom": angstrom, "source": source}


def retrieval_source_names():
    """What the integers in :func:`filled_loading`'s ``source`` array mean."""
    return {0: "retrieved", 1: "product", 2: "climatological"}
