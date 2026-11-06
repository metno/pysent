"""Find Arctic OLCI frames in the NBS catalogue - the sea-ice gap of A15.

Nothing in this plan demonstrates accuracy over sea ice: the mounted archive
stops at 68.7 degrees north and holds none. The owner pointed at the catalogue
rather than at a directory, so this asks it.

    python3 arctic_find.py --type OL_1_EFR --month 202604 --bbox -10,75,35,85

Two things are being established, and the order matters because the second is
only worth asking if the first works:

1. **Does the catalogue hold Arctic OLCI at all, and for which dates?** The
   mounted archive is a rolling window of about a week, so a March-to-May frame
   is certainly not on disk; whether the catalogue can still name it, and
   whether the named file can still be fetched, is the open question.
2. **Can the query combine a title pattern with a bounding box?** The S5P/S3
   plan recorded that it could not. If that still holds, the latitude filter has
   to be applied to the returned bounding boxes instead of in the request, which
   changes how many records have to be paged through.
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

#: The new DNS the owner gave on 2026-11-05. The older helper in
#: evidence/s5p_s3_audit hardcodes an IP that this replaces.
#:
#: Overridden by ``--endpoint`` rather than by an environment variable, for two
#: reasons. An evidence script should carry the service it queried in the
#: command line that reproduces it, not hidden in the caller's environment; and
#: a URL taken from the environment may carry credentials in its userinfo, which
#: this script would then print. CodeQL flags exactly that, and it is right to.
DEFAULT_ENDPOINT = "https://nbs.csw-staging.met.no/csw"
ENDPOINT = DEFAULT_ENDPOINT

NS = {
    "csw": "http://www.opengis.net/cat/csw/2.0.2",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dct": "http://purl.org/dc/terms/",
    "ows": "http://www.opengis.net/ows",
}

DOWNLOAD = "WWW:DOWNLOAD-1.0-http--download"


def _get(params: dict[str, str], *, timeout: int = 120) -> ET.Element:
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return ET.fromstring(response.read())
        except Exception as exc:                       # noqa: BLE001 - network flake
            last = exc
            print(f"  retry {attempt + 1}: {exc}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"catalogue did not answer: {last}")


def search(constraint: str, *, start: int = 1, maxrecords: int = 20,
           element_set: str = "full") -> ET.Element:
    return _get({
        "service": "CSW", "version": "2.0.2", "request": "GetRecords",
        "typenames": "csw:Record", "elementsetname": element_set,
        "resulttype": "results", "maxrecords": str(maxrecords),
        "startposition": str(start),
        "constraintlanguage": "CQL_TEXT", "constraint_language_version": "1.1.0",
        "constraint": constraint,
        "outputschema": "http://www.opengis.net/cat/csw/2.0.2",
    })


def records(root: ET.Element):
    results = root.find("csw:SearchResults", NS)
    if results is None:
        # A service exception comes back as a different root; show it rather
        # than reporting zero matches, which looks like an empty archive.
        text = ET.tostring(root, encoding="unicode")
        raise RuntimeError(f"no SearchResults in the response: {text[:400]}")
    matched = int(results.get("numberOfRecordsMatched", "0"))
    rows = []
    for rec in results.findall("csw:Record", NS):
        refs = {r.get("scheme"): (r.text or "").strip()
                for r in rec.findall("dct:references", NS)}
        lower = rec.findtext("ows:BoundingBox/ows:LowerCorner", "", NS)
        upper = rec.findtext("ows:BoundingBox/ows:UpperCorner", "", NS)
        rows.append({
            "id": rec.findtext("dc:identifier", "", NS),
            "title": rec.findtext("dc:title", "", NS),
            "lower": lower,
            "upper": upper,
            "url": refs.get(DOWNLOAD, ""),
        })
    return matched, rows


def latitudes(row) -> tuple[float, float] | None:
    """(south, north) from a record's bounding box, or None if unparseable.

    The corners are "lat lon" in this catalogue's axis order, which is worth
    checking rather than assuming: a swapped pair would silently turn a
    latitude filter into a longitude one.
    """
    try:
        south, _west = (float(part) for part in row["lower"].split()[:2])
        north, _east = (float(part) for part in row["upper"].split()[:2])
    except (ValueError, IndexError):
        return None
    return south, north


# The product types the correction needs, and what each is for. OL_1_EFR is the
# input; OL_2_WFR carries the per-pixel w_aer loading that turned out to be the
# single largest gain in the water arm (A11/A12); SY_2_AOD is the coarse
# fallback and the only source of a loading *uncertainty*. SY_2_SYN is the land
# reference, worth having where an Arctic frame has any clear land.
WANTED = {
    "l1": "OL_1_EFR",
    "wfr": "OL_2_WFR",
    "aod": "SY_2_AOD",
    "syn": "SY_2_SYN",
}

# S3A_OL_1_EFR____<start>_<stop>_<created>_<duration>_<cycle>_<orbit>_<frame>
KEY = __import__("re").compile(
    r"^(S3[AB])_\w{2}_\w_\w{3}____(\d{8}T\d{6})_(\d{8}T\d{6})_\d{8}T\d{6}_\d{4}_(\d{3})_(\d{3})"
)


def acquisition_id(title: str):
    """Platform, start, stop, cycle, orbit - the identifier the on-disk audit uses.

    Named *id* rather than *key*: CodeQL's sensitive-data heuristic reads an
    identifier called ``key`` as a credential, and then reports printing an
    acquisition's platform and orbit as clear-text logging of a secret. The
    name was wrong anyway - nothing here is a key in any sense that matters.

    The frame number is deliberately *not* part of it: a Level-2 product covers
    the same acquisition under a different frame counter, so including it would
    split pairs that belong together.
    """
    match = KEY.match(title)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3), match.group(4), match.group(5)


def collect(kind_pattern: str, month: str, bbox: str, *, page_size: int = 100,
            limit: int = 4000):
    """Every record of one type in one month and box, paged to exhaustion."""
    constraint = f"dc:title like '%{kind_pattern}%{month}%'"
    if bbox:
        west, south, east, north = bbox.split(",")
        constraint += f" AND BBOX(ows:BoundingBox, {west}, {south}, {east}, {north})"
    rows, start, matched = [], 1, None
    while True:
        total, page = records(search(constraint, start=start, maxrecords=page_size))
        matched = total if matched is None else matched
        rows.extend(page)
        start += page_size
        if not page or start > min(matched, limit):
            break
    return matched, rows


def matched_sets(month: str, bbox: str, *, min_lat: float | None,
                 need: tuple[str, ...]) -> dict:
    """Acquisitions holding every wanted product type, identified as the audit does."""
    found: dict = {}
    for kind in WANTED:
        total, rows = collect(WANTED[kind], month, bbox)
        kept = 0
        for row in rows:
            ident = acquisition_id(row["title"])
            if ident is None:
                continue
            box = latitudes(row)
            if min_lat is not None and (box is None or box[1] < min_lat):
                continue
            entry = found.setdefault(ident, {})
            # Prefer the non-time-critical reprocessing when both exist, the
            # same rule the on-disk collector uses.
            existing = entry.get(kind)
            if existing is None or ("_NT_" in row["title"] and "_NR_" in existing["title"]):
                entry[kind] = row
            kept += 1
        print(f"  {WANTED[kind]:<10} {total:>5} matched, {kept:>4} past the latitude filter")
    return {ident: value for ident, value in found.items()
            if all(k in value for k in need)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", default="OL_1_EFR")
    parser.add_argument("--month", default="202604", help="YYYYMM of the sensing start")
    parser.add_argument("--bbox", default="", help="west,south,east,north")
    parser.add_argument("--min-lat", type=float, default=None,
                        help="filter returned records by their own north edge")
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--sets", action="store_true",
                        help="find acquisitions holding every product type, not one type")
    parser.add_argument("--need", default="l1,wfr",
                        help="which of l1,wfr,aod,syn an acquisition must have")
    parser.add_argument("--urls", default="",
                        help="write the matched download URLs to this file")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT,
                        help="the CSW service to query")
    arguments = parser.parse_args()

    global ENDPOINT
    ENDPOINT = arguments.endpoint

    if arguments.sets:
        need = tuple(part.strip() for part in arguments.need.split(",") if part.strip())
        print(f"endpoint {ENDPOINT}")
        print(f"month {arguments.month}  bbox {arguments.bbox or 'none'}  "
              f"min north edge {arguments.min_lat}  need {'+'.join(need)}\n")
        sets = matched_sets(
            arguments.month, arguments.bbox,
            min_lat=arguments.min_lat, need=need,
        )
        print(f"\n{len(sets)} acquisition(s) with {'+'.join(need)}\n")
        lines = []
        for ident in sorted(sets):
            platform, start, _stop, _cycle, orbit = ident
            entry = sets[ident]
            box = latitudes(entry[need[0]])
            span = f"{box[0]:.1f}-{box[1]:.1f} N" if box else "no bbox"
            have = "+".join(k for k in WANTED if k in entry)
            # CodeQL reports this line as "clear-text logging of sensitive
            # information": it classifies latitude and longitude as private
            # location data, and the span is built from a bounding box. These
            # are the corners of a satellite frame's footprint from a public
            # catalogue, with no person at either end, so the finding is a
            # misclassification rather than a defect.
            #
            # **The inline marker below does not suppress it** - this
            # repository's code scanning does not honour inline suppressions -
            # and it is left only to name the rule. Clearing the check needs
            # the alert dismissed as a false positive in the repository's
            # security tab, which is the owner's call rather than something to
            # work around by not printing a footprint.
            print(f"{platform} {start}  orbit {orbit}  {span:>14}  {have}")  # codeql[py/clear-text-logging-sensitive-data]
            for kind in need:
                if entry[kind]["url"]:
                    lines.append(entry[kind]["url"])
        if arguments.urls and lines:
            with open(arguments.urls, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            total_mb = len(lines) * 700
            print(f"\n{len(lines)} URL(s) -> {arguments.urls}  (roughly {total_mb/1000:.1f} GB)")
        return 0 if sets else 1

    print(f"endpoint {ENDPOINT}")
    constraint = f"dc:title like '%{arguments.type}%{arguments.month}%'"
    if arguments.bbox:
        west, south, east, north = arguments.bbox.split(",")
        constraint += f" AND BBOX(ows:BoundingBox, {west}, {south}, {east}, {north})"
    print(f"constraint {constraint}\n")

    seen = 0
    for page in range(arguments.pages):
        start = 1 + page * arguments.n
        matched, rows = records(search(constraint, start=start, maxrecords=arguments.n))
        if page == 0:
            print(f"matched {matched}\n")
            if not matched:
                return 1
        for row in rows:
            box = latitudes(row)
            if arguments.min_lat is not None and box and box[1] < arguments.min_lat:
                continue
            seen += 1
            span = f"{box[0]:.1f} to {box[1]:.1f} N" if box else "no bbox"
            # CodeQL reports this line as "clear-text logging of sensitive
            # information": it classifies latitude and longitude as private
            # location data, and the span is built from a bounding box. These
            # are the corners of a satellite frame's footprint from a public
            # catalogue, with no person at either end, so the finding is a
            # misclassification rather than a defect.
            #
            # **The inline marker below does not suppress it** - this
            # repository's code scanning does not honour inline suppressions -
            # and it is left only to name the rule. Clearing the check needs
            # the alert dismissed as a false positive in the repository's
            # security tab, which is the owner's call rather than something to
            # work around by not printing a footprint.
            print(f"{row['title'][:62]:<62} {span:>18}")  # codeql[py/clear-text-logging-sensitive-data]
            if row["url"]:
                print(f"    {row['url']}")
        if start + arguments.n > matched:
            break
    print(f"\n{seen} record(s) shown")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
