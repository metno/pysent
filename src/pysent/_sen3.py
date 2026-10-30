"""Read a Sentinel-3 SEN3 product: zipped or unpacked, local or remote.

A SEN3 product is a directory of NetCDF files, one variable or small group of
variables per file, plus an XML manifest. Usually it arrives as a ``.zip``. The
useful property, verified across eighteen product types, is that **every member
of every inspected zip is STORED** rather than deflated, so a single variable
can be pulled out of a 143 MB archive by reading only its own bytes - over HTTP
too, where listing a remote product costs 0.18 s and lifting one member 0.12 s.

Two traps shape how this module reads:

**GDAL's netCDF driver cannot open a file inside a zip without Linux
``userfaultfd``**, which Docker's default seccomp profile blocks. The error
names the missing syscall, so it looks like a container problem rather than a
data problem, and it would strike a worker in production while passing on a
developer's machine. The HDF5 driver opens the same member without it but
returns raw pixels with no scale, no fill value and no geotransform. So this
module does neither: it **copies the member out of the archive first** and opens
the copy. Because the member is STORED that copy is a byte-for-byte range read -
12.9 MB in 0.01 s locally - and the netCDF driver then reports everything.

**Reading a member through the multidimensional API with a stride is slow over
HTTP**: 17 s for a 3.7 MB flag file, against 0.6 s through the classic raster
API. Sentinel-3 variables are plain rasters, so the classic API is what is used
here, unlike Sentinel-5P where the multidimensional API is needed for the group
layout.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np
from osgeo import gdal

MANIFEST = "xfdumanifest.xml"

# Fields worth lifting out of the manifest. The processing baseline is here
# because the name is not a reliable source for it: a 2023 product labelled
# ``_NT_003`` carried the older variable names, so a pipeline that guesses the
# baseline from the filename reads the wrong variables.
_MANIFEST_FIELDS = ("productName", "productType", "baselineCollection", "processingVersion")
_MANIFEST_TIMES = {"startTime": "start", "stopTime": "end"}


def sen3_root(input_dataset: str) -> str:
    """The ``.SEN3`` directory of a product, as a path GDAL can list.

    Accepts a local or remote ``.zip``, an already ``/vsizip/``-prefixed path,
    or an unpacked ``.SEN3`` directory. Found by listing the archive rather than
    by trusting the naming convention, so a renamed zip still resolves.
    """
    reference = str(input_dataset).rstrip("/")
    if reference.upper().endswith(".SEN3"):
        return reference

    if reference.startswith(("/vsizip/", "/vsicurl/")):
        container = reference
    elif reference.startswith(("http://", "https://")):
        # Tested before the .zip suffix, because a remote archive ends in .zip
        # too and would otherwise be handed to /vsizip/ without /vsicurl/.
        container = f"/vsizip//vsicurl/{reference}"
    elif reference.lower().endswith(".zip"):
        container = f"/vsizip/{reference}"
    else:
        return reference

    entries = [entry for entry in (gdal.ReadDir(container) or []) if entry not in (".", "..")]
    directories = [entry.rstrip("/") for entry in entries if entry.rstrip("/").upper().endswith(".SEN3")]
    if len(directories) == 1:
        return f"{container}/{directories[0]}"
    expected = f"{PurePosixPath(reference).stem}.SEN3"
    if expected in directories:
        return f"{container}/{expected}"
    if directories:
        return f"{container}/{directories[0]}"
    # Some archives hold the members at the top level with no .SEN3 wrapper.
    return container


def sen3_members(root: str) -> list[str]:
    """The member file names of a product directory."""
    return sorted(entry.rstrip("/") for entry in (gdal.ReadDir(root) or []) if entry not in (".", ".."))


def check_members_stored(zip_path: str) -> list[str]:
    """Names of any members that are compressed rather than STORED.

    An empty list is the expected answer and means a single variable can be
    lifted out by a range read. A non-empty one is not an error - the product
    still reads - but every member then has to be inflated, so a remote read
    stops being cheap. Only a local archive is checked; a remote one would cost
    more to verify than to use.
    """
    path = str(zip_path)
    if path.startswith("/vsi") or not path.lower().endswith(".zip") or not Path(path).exists():
        return []
    with zipfile.ZipFile(path) as archive:
        return [
            info.filename
            for info in archive.infolist()
            if not info.filename.endswith("/") and info.compress_type != zipfile.ZIP_STORED
        ]


def read_sen3_manifest(root: str) -> dict[str, Any]:
    """Product type, processing baseline, times and footprint from the manifest.

    The footprint is the manifest's own ``posList``, a closed ring of 47-295
    points in latitude-longitude order, which is the real outline rather than
    the bounding box a catalogue records.
    """
    manifest_path = f"{root}/{MANIFEST}"
    handle = gdal.VSIFOpenL(manifest_path, "rb")
    if handle is None:
        raise ValueError(f"{manifest_path} is missing, so this does not look like a SEN3 product")
    try:
        size = gdal.VSIStatL(manifest_path).size
        raw = gdal.VSIFReadL(1, size, handle)
    finally:
        gdal.VSIFCloseL(handle)

    tree = ET.fromstring(raw)
    facts: dict[str, Any] = {"time": {}}
    for element in tree.iter():
        local = element.tag.split("}")[-1]
        text = (element.text or "").strip()
        if local in _MANIFEST_FIELDS and text:
            facts.setdefault(local, text)
        elif local in _MANIFEST_TIMES and text:
            facts["time"].setdefault(_MANIFEST_TIMES[local], text)
        elif local == "posList" and text and "footprint" not in facts:
            numbers = [float(value) for value in text.split()]
            # GML order in a SEN3 manifest is latitude then longitude.
            ring = [(longitude, latitude) for latitude, longitude in zip(numbers[0::2], numbers[1::2])]
            if ring and ring[0] != ring[-1]:
                ring.append(ring[0])
            facts["footprint"] = {"type": "Polygon", "coordinates": [ring]}

    facts.setdefault("baselineCollection", "")
    facts["baseline"] = _baseline_of(facts, root)
    return facts


def _baseline_of(facts: dict[str, Any], root: str) -> str:
    """The processing baseline as a three-digit string, or ``""``.

    Prefers the manifest's own field, then the trailing ``_NNN`` of the product
    name. Never guessed from anything else: reading the wrong baseline means
    looking for variable names that do not exist in the file.
    """
    collection = str(facts.get("baselineCollection") or "").strip()
    if collection.isdigit():
        return collection.zfill(3)
    name = str(facts.get("productName") or PurePosixPath(root).name)
    match = re.search(r"_(\d{3})(?:\.SEN3)?$", name.replace(".zip", ""))
    return match.group(1) if match else ""


def find_member(root: str, pattern: str) -> str:
    """The one member matching a shell pattern, or a clear complaint.

    Most SEN3 members have fixed names, but the sea-surface-temperature product
    carries two GHRSST files whose names are generated - they contain the
    acquisition and processing timestamps, as in
    ``20260614175338-MAR-L2P_GHRSST-SSTskin-SLSTRB-NTC_DUAL_...nc`` - so they
    can only be found by shape. Matching exactly one is required: two matches
    means the pattern is too loose to say which view was used, which for DUAL
    against NADIR is a difference in the measurement itself.
    """
    import fnmatch

    members = sen3_members(root)
    matches = [member for member in members if fnmatch.fnmatch(member, pattern)]
    if not matches:
        raise ValueError(
            f"no member of this product matches {pattern!r}; it has: {', '.join(members)}"
        )
    if len(matches) > 1:
        raise ValueError(
            f"{len(matches)} members match {pattern!r} ({', '.join(matches)}); "
            "the pattern has to name one"
        )
    return matches[0]


def extract_member(root: str, member: str, scratch: Path) -> str:
    """Copy one member out of the product and return the local path.

    This is the step that makes the netCDF driver usable at all: it refuses to
    open a file inside a zip unless ``userfaultfd`` is available, which a
    container usually forbids. The member is STORED, so the copy is a plain
    range read and costs almost nothing.
    """
    if any(character in member for character in "*?["):
        member = find_member(root, member)
    destination = Path(scratch) / Path(member).name
    source = f"{root}/{member}"
    if gdal.VSIStatL(source) is None:
        available = ", ".join(sen3_members(root)) or "nothing"
        raise ValueError(f"{member} is not in this product; it has: {available}")
    if gdal.CopyFile(source, str(destination)) != 0:
        raise ValueError(f"{source} could not be copied out of the product")
    return str(destination)


def open_sen3_variable(local_member: str, variable: str) -> Any:
    """Open one variable of an extracted member as a GDAL dataset."""
    dataset = gdal.Open(f'NETCDF:"{local_member}":{variable}')
    if dataset is None:
        raise ValueError(f"{variable} is not a variable of {Path(local_member).name}")
    return dataset


# A log-packed variable says so in its own units: CHL_NN is "lg(re mg.m-3)",
# TSM_NN "lg(re g.m-3)", kd_490 "lg(re m-1)". Checked across both processing
# baselines and against the variables that are *not* log-packed (IWV "kg.m-2",
# PAR "microeinstein.m-2.s-1", GIFAPAR and OTCI with no units at all), so the
# convention can be detected rather than hard-coded per product - which also
# means it keeps working when a baseline renames a variable.
_LOG_UNITS_PREFIX = "lg("


def is_log_packed(band: Any) -> bool:
    """True when the band's own units say it holds a base-10 logarithm."""
    units = str(band.GetMetadata().get("units", "") or "").strip().lower()
    return units.startswith(_LOG_UNITS_PREFIX)


def read_packed(band: Any, *, decimate: int = 1, log10: bool | str = "auto") -> np.ndarray:
    """Read a band as float32, fill as NaN, ``scale``/``offset`` applied.

    ``decimate`` reads every n-th pixel by asking GDAL for a smaller buffer,
    which lets a 355-megapixel Synergy band be previewed without holding it.

    ``log10`` applies the final ``10**x`` that the OLCI water products need:
    they store ``log10(value)`` packed into a byte, so a pipeline that forgets
    it maps chlorophyll onto roughly -3..2 instead of 0.001..100 - a plausible
    looking picture of the wrong quantity. The default ``"auto"`` reads the
    band's units, which name the packing; pass ``True`` or ``False`` to force it.
    """
    width = max(1, band.XSize // max(1, int(decimate)))
    height = max(1, band.YSize // max(1, int(decimate)))
    raw = band.ReadAsArray(buf_xsize=width, buf_ysize=height)
    if raw is None:
        raise ValueError("the band could not be read")

    values = raw.astype(np.float64)
    nodata = band.GetNoDataValue()
    missing = np.zeros(raw.shape, dtype=bool) if nodata is None else (raw == nodata)

    scale = band.GetScale()
    offset = band.GetOffset()
    if scale not in (None, 1.0):
        values = values * float(scale)
    if offset not in (None, 0.0):
        values = values + float(offset)
    if log10 == "auto":
        log10 = is_log_packed(band)
    if log10:
        values = np.power(10.0, values)

    return np.where(missing, np.nan, values).astype(np.float32)


def read_flag_table(band: Any) -> list[dict[str, Any]]:
    """The band's own flag definitions, as ``{"name", "mask", "value"}`` rows.

    Read from the file rather than hard-coded, because the bit layouts differ
    per product and per baseline - an OLCI water-quality flag is a 64-bit word
    with 57 non-contiguous bits - and because a name is readable where a bit
    number is not.

    Sentinel-3 mixes two CF conventions in one variable. A single bit has
    ``mask == value`` (``land`` is bit 8). A small enumeration shares a mask
    across several values: the Synergy status word puts ``clear``, ``unfilled``,
    ``uncertain`` and ``cloud`` in the low two bits as values 0, 1, 2 and 3 of
    mask 3. Both forms come back as rows here, so a caller selects by name
    without needing to know which form it is dealing with.
    """
    metadata = band.GetMetadata()
    names = str(metadata.get("flag_meanings", "")).split()
    masks = _flag_numbers(metadata.get("flag_masks"))
    values = _flag_numbers(metadata.get("flag_values"))

    if not names:
        return []
    if not masks and not values:
        return []
    if not masks:
        masks = values
    if not values:
        values = masks

    rows = []
    for index, name in enumerate(names):
        if index >= len(masks) or index >= len(values):
            break
        rows.append({"name": name, "mask": int(masks[index]), "value": int(values[index])})
    return rows


def _flag_numbers(text: Any) -> list[int]:
    if text is None:
        return []
    return [int(float(item)) for item in re.findall(r"-?\d+(?:\.\d+)?", str(text))]


def flag_selection(
    flags: np.ndarray,
    table: list[dict[str, Any]],
    names: list[str],
    *,
    allow_missing: bool = False,
) -> tuple[np.ndarray, list[str]]:
    """Where any of ``names`` is set, and which of them this product lacks.

    A name the file does not define raises by default, because a silently
    ignored flag is a mask that quietly does nothing - and a mask that does
    nothing looks exactly like a product with unusually good data.

    ``allow_missing`` is for a *default* mask rather than a requested one. Flag
    sets differ between processing baselines: baseline 004 of an OLCI water
    product defines 57 flags and baseline 003 only 53, so a default list that
    names ``CLOUD_SHADOW`` is correct for one and impossible for the other.
    Refusing the whole product there would be wrong, so the missing names are
    skipped and returned, for the caller to record rather than discard.
    """
    known = {row["name"]: row for row in table}
    missing = [name for name in names if name not in known]
    if missing and not allow_missing:
        raise ValueError(
            f"flag(s) {', '.join(missing)} are not defined in this product; it has: "
            f"{', '.join(sorted(known)) or 'no flags'}"
        )

    selected = np.zeros(flags.shape, dtype=bool)
    for name in names:
        row = known.get(name)
        if row is None:
            continue
        selected |= (flags.astype(np.int64) & row["mask"]) == row["value"]
    return selected, missing


def resolve_variable(
    root: str,
    candidates: list[tuple[str, str]],
    scratch: Path,
) -> tuple[str, str, str]:
    """Find the first ``(member, variable)`` candidate this product actually has.

    Variable names move between processing baselines: the same quantity is
    ``trsp.nc:kd_490`` under baseline 004 and ``trsp.nc:KD490_M07`` under 003,
    and the chlorophyll variants are ``chlor_a.nc:chlor_a`` against
    ``chl_oc4me.nc:CHL_OC4ME``. The packing moves too - that pair differs in
    both scale and offset - which is why the values are read from the file
    rather than from a table.

    Returns ``(local_path, member, variable)``. Raises with everything it looked
    for when none matches, because "variable not found" on its own does not say
    whether the product is unexpected or the table is out of date.
    """
    import fnmatch

    members = set(sen3_members(root))
    tried: list[str] = []
    for member, variable in candidates:
        tried.append(f"{member}:{variable}")
        if member not in members:
            # A generated member name is given as a pattern.
            matches = [candidate for candidate in members if fnmatch.fnmatch(candidate, member)]
            if not matches:
                continue
            if len(matches) > 1:
                raise ValueError(
                    f"{len(matches)} members match {member!r} ({', '.join(sorted(matches))}); "
                    "the pattern has to name one"
                )
            # Report the member actually used, not the pattern. For sea surface
            # temperature that is the difference between the two-view and the
            # nadir-only retrieval, which is a difference in the measurement.
            member = matches[0]
        local = extract_member(root, member, scratch)
        try:
            dataset = gdal.Open(f'NETCDF:"{local}":{variable}')
        except RuntimeError:
            # With GDAL exceptions enabled a missing variable raises rather than
            # returning None, and a miss is the expected outcome in this loop.
            dataset = None
        if dataset is not None:
            dataset = None
            return local, member, variable
    raise ValueError(
        f"none of {', '.join(tried)} is in this product. Its members are: "
        f"{', '.join(sorted(members))}. A processing baseline this table does not cover "
        "would look exactly like this."
    )
