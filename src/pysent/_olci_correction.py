"""Applying the atmospheric correction to a whole OLCI Level-1 frame.

The pieces all existed before this module: :mod:`pysent._atmosphere` turns
geometry and columns into the atmosphere's terms band by band,
:mod:`pysent._aerosol_retrieval` inverts the frame's own near-infrared for the
aerosol loading, and :mod:`pysent._olci_uncertainty` says how wrong the answer
is. What was missing is the frame-level step between them: read the geometry and
the meteorology off their tie-point grids once, decide where each pixel's
aerosol loading comes from, and carry a quality band that records what happened
to every pixel rather than only what came out.

**Four tiers, and the gaps between them are deliberate.** ``"none"`` returns the
top-of-atmosphere reflectance untouched. ``"gas"`` divides out ozone and water
vapour only - useful for isolating one term when diagnosing, and it publishes no
uncertainty because nothing was ever validated that way. ``"rayleigh"`` adds the
molecular scattering, which is the tier that makes a true-colour picture
defensible. ``"full"`` adds the aerosol tier on top.

**Nothing is on by default and nothing is fetched silently** - the plan's Q2 and
Q4. The caller names the tier, and names the aerosol source when it wants one.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from ._aerosol_retrieval import (
    CLIMATOLOGICAL_ANGSTROM,
    CLIMATOLOGICAL_DEPTH_550,
    filled_loading,
    retrieval_source_names,
    retrieve_optical_depth,
)
from ._atmosphere import (
    aerosol_grid_limits,
    atmosphere_terms,
    gas_transmittance,
    pixel_pressure,
    platform_of,
    relative_azimuth,
    surface_from_toa,
)
from ._olci_l1 import GEOMETRY_MEMBER, METEO_MEMBER, tie_point_field, toa_reflectance
from ._olci_uncertainty import loading_sensitivity, uncertainty, uncertainty_note

CORRECTION_TIERS = ("none", "gas", "rayleigh", "full")

# The tiers that actually divide an atmosphere out. ``"none"`` is in
# ``CORRECTION_TIERS`` so a caller can ask for it explicitly and get the frozen
# behaviour, but it is not a correction.
CORRECTING_TIERS = ("gas", "rayleigh", "full")

# The solar-zenith range every validation figure in the plan was measured in.
# There is deliberately **no refusal** outside it - finding A9 showed accuracy
# does not degrade towards the horizon inside this range, and a ceiling drawn
# from two frames would have refused the better half of the archive. But "no
# evidence of degradation inside 63-80 degrees" is not "accurate at 85", so a
# pixel outside the range is flagged rather than either refused or passed off as
# validated.
VALIDATED_SOLAR_ZENITH_DEG = (63.0, 80.0)

# What the quality band's bits mean. One band, one bit per thing that happened,
# because a pixel can be several of these at once and a single enumerated code
# would have to pick one.
QUALITY_BITS: dict[str, int] = {
    "corrected": 0,
    "aerosol_retrieved": 1,
    "aerosol_from_product": 2,
    "aerosol_climatological": 3,
    "haze_beyond_table": 4,
    "bright_surface": 5,
    "negative_reflectance": 6,
    "uncertainty_inferred": 7,
    "uncertainty_unmeasured": 8,
    "outside_validated_geometry": 9,
}


def quality_bit_names() -> dict[int, str]:
    """What each bit of the quality band means, for the tags and the result."""
    return {bit: name for name, bit in QUALITY_BITS.items()}


def frame_atmosphere(
    root: str, scratch: Path, shape: tuple[int, int], *, decimate: int
) -> dict[str, np.ndarray]:
    """The geometry and column fields the correction needs, on the image grid.

    Six tie-point expansions and one full-resolution read. They are gathered in
    one place because every band needs the same ones and the expansion is the
    expensive part - doing it per band would multiply the cost of the whole
    correction by the band count for no gain.
    """
    from ._sen3 import extract_member, find_member, open_sen3_variable, read_packed

    def tie(member: str, variable: str) -> np.ndarray:
        return tie_point_field(root, member, variable, scratch, shape)

    solar_zenith = tie(GEOMETRY_MEMBER, "SZA")
    view_zenith = tie(GEOMETRY_MEMBER, "OZA")
    azimuth = relative_azimuth(tie(GEOMETRY_MEMBER, "SAA"), tie(GEOMETRY_MEMBER, "OAA"))

    dataset = open_sen3_variable(
        extract_member(root, find_member(root, "geo_coordinates.nc"), scratch), "altitude"
    )
    altitude = read_packed(dataset.GetRasterBand(1), decimate=decimate, log10=False)
    dataset = None
    if altitude.shape != shape:
        # A decimation that does not divide evenly leaves a row or column over.
        # Trimming is right here rather than padding: altitude varies smoothly,
        # and the alternative is an arithmetic failure on a frame that is
        # otherwise fine.
        altitude = altitude[: shape[0], : shape[1]]

    return {
        "solar_zenith_deg": solar_zenith,
        "view_zenith_deg": view_zenith,
        "relative_azimuth_deg": azimuth,
        "pressure_hpa": pixel_pressure(tie(METEO_MEMBER, "sea_level_pressure"), altitude),
        "ozone_kg_m2": tie(METEO_MEMBER, "total_ozone"),
        "water_kg_m2": tie(METEO_MEMBER, "total_columnar_water_vapour"),
    }


def _geometry_only(atmosphere: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """The keys :func:`pysent._atmosphere.atmosphere_terms` takes."""
    return {
        key: atmosphere[key]
        for key in (
            "pressure_hpa", "solar_zenith_deg", "view_zenith_deg",
            "relative_azimuth_deg", "ozone_kg_m2", "water_kg_m2",
        )
    }


def resolve_loading(
    platform: str,
    *,
    aerosol: Any,
    atmosphere: dict[str, np.ndarray],
    toa: dict[int, np.ndarray],
    water: np.ndarray,
    shape: tuple[int, int],
) -> dict[str, Any]:
    """Where every pixel's aerosol loading comes from, and what it is.

    ``aerosol`` is the caller's named source, never a silent fetch (Q4):

    - a number - one optical depth at 550 nm for the whole frame;
    - a ``(depth, angstrom)`` pair;
    - ``"retrieve"`` - invert the frame's own near-infrared over clear water,
      and fall back for everything it refuses;
    - ``"climatological"`` - this archive's own median, stated as an assumption.

    The returned ``source`` array records which tier answered each pixel, which
    is what the uncertainty band indexes on: a retrieved loading and a guessed
    one are not equally trustworthy and must not share a figure.
    """
    names = retrieval_source_names()
    codes = {name: code for code, name in names.items()}

    refused: dict[str, np.ndarray] = {}
    retrieved: dict[str, np.ndarray] | None = None
    product_depth = product_angstrom = None

    if aerosol == "retrieve":
        retrieved = retrieve_optical_depth(
            platform,
            toa=toa,
            usable=water,
            **_geometry_only(atmosphere),
        )
        refused = retrieved["refused"]
    elif isinstance(aerosol, (tuple, list)) and len(aerosol) == 2:
        product_depth = np.full(shape, float(aerosol[0]))
        product_angstrom = np.full(shape, float(aerosol[1]))
    elif isinstance(aerosol, (int, float)) and not isinstance(aerosol, bool):
        product_depth = np.full(shape, float(aerosol))
        product_angstrom = np.full(shape, CLIMATOLOGICAL_ANGSTROM)
    elif aerosol != "climatological":
        raise ValueError(
            "aerosol must be a number, a (depth, angstrom) pair, 'retrieve' or "
            f"'climatological', not {aerosol!r}"
        )

    if retrieved is None:
        retrieved = {
            "optical_depth_550": np.full(shape, np.nan),
            "angstrom": np.full(shape, np.nan),
        }
    filled = filled_loading(
        retrieved, product_depth=product_depth, product_angstrom=product_angstrom
    )

    # The table's own top. Past it the correction is clamped to the edge row,
    # which is the right behaviour - the value beyond is unknown - but a clamped
    # pixel has to say so, or a smoke plume comes back looking like ordinary
    # haze. This is what ``aerosol_grid_limits`` was added for.
    # The *lowest* top across the bands in play, not the highest: a loading is
    # beyond the table if any band being corrected cannot reach it. Every band
    # shares the same depth grid today - 0.05 to 0.4 on both platforms - so the
    # two agree, which is exactly why taking the generous one would have been a
    # latent under-flagging bug rather than a visible one.
    ceiling = min(
        aerosol_grid_limits(platform, band)["optical_depth_550"][1] for band in toa
    )
    beyond = filled["optical_depth_550"] > ceiling

    return {
        "optical_depth_550": filled["optical_depth_550"],
        "angstrom": filled["angstrom"],
        "source": filled["source"],
        "source_names": names,
        "source_codes": codes,
        "refused": refused,
        "haze_beyond_table": beyond,
        "table_ceiling": float(ceiling),
    }


def correct_frame(
    input_dataset: str,
    root: str,
    scratch: Path,
    *,
    bands: list[int],
    shape: tuple[int, int],
    decimate: int,
    solar_zenith_for_toa: np.ndarray,
    land: np.ndarray,
    bright: np.ndarray,
    rejected: np.ndarray,
    tier: str = "none",
    aerosol: Any = None,
    aerosol_uncertainty: Any = None,
) -> dict[str, Any]:
    """Surface reflectance, its uncertainty and a quality band, for one frame.

    Returns the corrected bands in the order asked for, alongside the
    uncertainty band per band, the shared quality band, and provenance detailed
    enough to reproduce a pixel.

    ``aerosol_uncertainty`` is the loading's own standard error, if the caller
    has one - ``SY_2_AOD`` publishes ``AOD_*_uncertainty``, the retrieval does
    not. Supplying it adds a propagated term to the uncertainty band in
    quadrature. It is **not** the main term and must not be mistaken for one:
    see :mod:`pysent._olci_uncertainty` for the measurement showing it is about
    a tenth of the disagreement actually observed. Without it the band is the
    measured residual alone, which is the honest floor rather than a gap.
    """
    if tier not in CORRECTION_TIERS:
        raise ValueError(
            f"atmospheric_correction must be one of {', '.join(CORRECTION_TIERS)}, "
            f"not {tier!r}"
        )
    platform = platform_of(input_dataset)
    atmosphere = frame_atmosphere(root, scratch, shape, decimate=decimate)
    geometry = _geometry_only(atmosphere)

    toa = {
        band: toa_reflectance(
            root, band, scratch, decimate=decimate,
            solar_zenith_degrees=solar_zenith_for_toa,
        )
        for band in bands
    }

    if tier == "none":
        return {
            "tier": tier,
            "surfaces": {band: values.astype(np.float32) for band, values in toa.items()},
            "uncertainties": {},
            "quality": np.zeros(shape, dtype=np.uint16),
            "provenance": {"atmospheric_correction": "none"},
            "loading": None,
        }

    loading = None
    if tier == "full":
        if aerosol is None:
            raise ValueError(
                "the 'full' tier needs an aerosol source: a number, a "
                "(depth, angstrom) pair, 'retrieve' or 'climatological'"
            )
        # The retrieval needs its own bands, which are not necessarily the ones
        # being corrected, so they are read here rather than assumed present.
        from ._aerosol_retrieval import RETRIEVAL_BANDS

        retrieval_toa = dict(toa)
        if aerosol == "retrieve":
            for band in RETRIEVAL_BANDS:
                if band not in retrieval_toa:
                    retrieval_toa[band] = toa_reflectance(
                        root, band, scratch, decimate=decimate,
                        solar_zenith_degrees=solar_zenith_for_toa,
                    )
        loading = resolve_loading(
            platform,
            aerosol=aerosol,
            atmosphere=atmosphere,
            toa=retrieval_toa,
            water=~land & ~bright & ~rejected,
            shape=shape,
        )

    quality = np.zeros(shape, dtype=np.uint16)

    def mark(name: str, where: np.ndarray) -> None:
        """Set a bit, additively.

        The three bits set inside the per-band loop below -
        ``negative_reflectance``, ``uncertainty_inferred`` and
        ``uncertainty_unmeasured`` - therefore mean "in **any** band", because
        there is one quality band for the whole product rather than one per
        band. That is the useful reading for a caller deciding whether to trust
        a pixel at all, but it is not "in every band", and a reader who wants
        per-band detail has the uncertainty bands themselves.
        """
        quality[np.asarray(where, dtype=bool)] |= np.uint16(1 << QUALITY_BITS[name])

    mark("corrected", ~rejected)
    mark("bright_surface", bright)
    low, high = VALIDATED_SOLAR_ZENITH_DEG
    zenith = atmosphere["solar_zenith_deg"]
    mark("outside_validated_geometry", (zenith < low) | (zenith > high))

    if loading is not None:
        mark("aerosol_retrieved", loading["source"] == loading["source_codes"]["retrieved"])
        mark("aerosol_from_product", loading["source"] == loading["source_codes"]["product"])
        mark(
            "aerosol_climatological",
            loading["source"] == loading["source_codes"]["climatological"],
        )
        mark("haze_beyond_table", loading["haze_beyond_table"])

    surfaces: dict[int, np.ndarray] = {}
    uncertainties: dict[int, np.ndarray] = {}
    for band in bands:
        if tier == "gas":
            terms = {
                "gas_transmittance": gas_transmittance(
                    platform, band,
                    ozone_kg_m2=atmosphere["ozone_kg_m2"],
                    water_kg_m2=atmosphere["water_kg_m2"],
                    solar_zenith_deg=atmosphere["solar_zenith_deg"],
                    view_zenith_deg=atmosphere["view_zenith_deg"],
                )
            }
        else:
            depth = loading["optical_depth_550"] if loading is not None else None
            angstrom = loading["angstrom"] if loading is not None else None
            terms = atmosphere_terms(
                platform, band, optical_depth_550=depth, angstrom=angstrom, **geometry
            )

        surface = surface_from_toa(toa[band], **terms)
        surfaces[band] = np.where(rejected, np.nan, surface).astype(np.float32)

        # The sensitivity is only worth two extra forward evaluations per band
        # when there is a loading uncertainty to multiply it by. Computing it
        # unconditionally and then discarding it was the first version's waste.
        sensitivity = None
        if tier == "full" and loading is not None and aerosol_uncertainty is not None:
            sensitivity = loading_sensitivity(
                platform, band, toa=toa[band],
                optical_depth_550=loading["optical_depth_550"],
                angstrom=loading["angstrom"], geometry=geometry,
            )
        values, why = uncertainty(
            band,
            land=land,
            tier=tier,
            aerosol_source=loading["source"] if loading is not None else None,
            depth_uncertainty=aerosol_uncertainty,
            sensitivity=sensitivity,
        )
        uncertainties[band] = np.where(rejected, np.nan, values).astype(np.float32)
        mark("uncertainty_inferred", why["inferred_from_neighbour"])
        mark("uncertainty_unmeasured", why["unmeasured"] & ~rejected)
        mark("negative_reflectance", np.nan_to_num(surface, nan=0.0) < 0.0)

    provenance = {
        "atmospheric_correction": tier,
        "uncertainty_meaning": uncertainty_note(tier),
        "validated_solar_zenith_deg": "%g-%g" % VALIDATED_SOLAR_ZENITH_DEG,
    }
    if loading is not None:
        finite = np.isfinite(loading["optical_depth_550"])
        provenance.update({
            "aerosol_source": str(aerosol),
            "aerosol_table_ceiling": f"{loading['table_ceiling']:g}",
            "aerosol_optical_depth_550_median": (
                f"{float(np.nanmedian(loading['optical_depth_550'])):.4f}"
                if finite.any() else "none"
            ),
            "aerosol_angstrom_median": (
                f"{float(np.nanmedian(loading['angstrom'])):.3f}"
                if finite.any() else "none"
            ),
        })
    return {
        "tier": tier,
        "surfaces": surfaces,
        "uncertainties": uncertainties,
        "quality": quality,
        "provenance": provenance,
        "loading": loading,
    }
