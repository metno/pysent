"""How many granules a day-over-a-region query returns, and whether the
catalogue can answer it directly.

A composite job is handed a *list* of inputs chosen by the service from a time
window and a bounding box. Everything about the job - its cost, its memory, its
failure modes - follows from how long that list is, and B.9 recorded it as "not
measured".

    python3 catalogue_day.py 20261009            # one day, all NO2 NRTI
    python3 catalogue_day.py 20261009 --bbox 2 57 32 72
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ENDPOINT = "http://157.249.72.203:8006/csw"
NS = {
    "csw": "http://www.opengis.net/cat/csw/2.0.2",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dct": "http://purl.org/dc/terms/",
    "ows": "http://www.opengis.net/ows",
}
# Mainland Norway plus its shelf and a slice of the Barents Sea.
NORDIC_BBOX = (2.0, 57.0, 32.0, 72.0)


def _get(params: dict[str, str]) -> ET.Element:
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=120) as response:
        return ET.fromstring(response.read())


def query(constraint: str, start: int = 1, maxrecords: int = 10, result_type: str = "results"):
    return _get({
        "service": "CSW", "version": "2.0.2", "request": "GetRecords",
        "typenames": "csw:Record", "elementsetname": "full", "resulttype": result_type,
        "maxrecords": str(maxrecords), "startposition": str(start),
        "constraintlanguage": "CQL_TEXT", "constraint_language_version": "1.1.0",
        "constraint": constraint,
        "outputschema": "http://www.opengis.net/cat/csw/2.0.2",
    }).find("csw:SearchResults", NS)


def records(results: ET.Element):
    for record in results.findall("csw:Record", NS):
        lower = record.findtext("ows:BoundingBox/ows:LowerCorner", default="", namespaces=NS)
        upper = record.findtext("ows:BoundingBox/ows:UpperCorner", default="", namespaces=NS)
        references = {r.get("scheme"): (r.text or "").strip() for r in record.findall("dct:references", NS)}
        yield {
            "id": record.findtext("dc:identifier", default="", namespaces=NS),
            "title": record.findtext("dc:title", default="", namespaces=NS),
            "lower": lower, "upper": upper, "refs": references,
        }


def box_of(record) -> tuple[float, float, float, float] | None:
    """The record's bbox as (west, south, east, north).

    The catalogue writes its corners **latitude first**, which the earlier audit
    established, and never wraps them at the antimeridian.
    """
    try:
        south, west = (float(v) for v in record["lower"].split())
        north, east = (float(v) for v in record["upper"].split())
    except ValueError:
        return None
    return (west, south, east, north)


def intersects(box, other) -> bool:
    return not (box[2] < other[0] or box[0] > other[2] or box[3] < other[1] or box[1] > other[3])


def all_records(constraint: str, limit: int = 400):
    """Every record for a constraint, paging 10 at a time."""
    out, start = [], 1
    total = int(query(constraint, maxrecords=1, result_type="hits").get("numberOfRecordsMatched", "0"))
    while len(out) < min(total, limit):
        page = query(constraint, start=start, maxrecords=10)
        batch = list(records(page))
        if not batch:
            break
        out.extend(batch)
        start += 10
    return total, out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("day", help="YYYYMMDD")
    parser.add_argument("--type", default="L2__NO2___")
    parser.add_argument("--stream", default="NRTI")
    parser.add_argument("--bbox", type=float, nargs=4, default=NORDIC_BBOX,
                        metavar=("W", "S", "E", "N"))
    parser.add_argument("--try-cql-bbox", action="store_true",
                        help="also ask the catalogue to do the bbox filter itself")
    args = parser.parse_args()

    like = f"S5P_{args.stream}_{args.type}_{args.day}%"
    total, found = all_records(f"dc:title like '{like}'")
    print(f"{like}: catalogue reports {total} records, fetched {len(found)}")

    region = tuple(args.bbox)
    boxes = [(r, box_of(r)) for r in found]
    usable = [(r, b) for r, b in boxes if b]
    hits = [(r, b) for r, b in usable if intersects(b, region)]
    print(f"  with a usable bbox: {len(usable)}")
    print(f"  intersecting {region}: {len(hits)}")
    without_ref = sum(1 for r, _ in hits if not r["refs"])
    print(f"  of those, with no download reference: {without_ref}")
    spans = [b for _, b in hits]
    if spans:
        print(f"  their latitude spans: "
              f"{min(b[1] for b in spans):.1f} to {max(b[3] for b in spans):.1f}")
        print(f"  longitude spans      : "
              f"{min(b[0] for b in spans):.1f} to {max(b[2] for b in spans):.1f}")
        full_world = sum(1 for b in spans if b[0] <= -179.9 and b[2] >= 179.9)
        print(f"  reporting a full -180..180 longitude: {full_world}")
    for record, box in hits[:6]:
        print(f"    {record['title'][9:40]}  {tuple(round(v, 1) for v in box)}")

    if args.try_cql_bbox:
        # Can the catalogue do the spatial filter itself? If it can, the service
        # need not fetch and filter; if it cannot, that is a service-side cost.
        west, south, east, north = region
        for constraint in (
            f"dc:title like '{like}' AND BBOX(ows:BoundingBox,{south},{west},{north},{east})",
            f"BBOX(ows:BoundingBox,{south},{west},{north},{east}) AND dc:title like '{like}'",
        ):
            try:
                results = query(constraint, maxrecords=1, result_type="hits")
                print(f"  CQL BBOX accepted: {results.get('numberOfRecordsMatched')} records "
                      f"for {constraint[:60]}...")
            except Exception as exc:
                print(f"  CQL BBOX rejected ({type(exc).__name__}): {str(exc)[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
