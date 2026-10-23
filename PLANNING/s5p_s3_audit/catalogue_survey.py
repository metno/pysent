"""Survey the NBS CSW catalogue for Sentinel-3 and Sentinel-5P records.

Stdlib only (no OWSLib).  The catalogue holds ~2.7 M Sentinel-3 records and the
server caps a page at 10, so a full harvest is not possible.  Instead:

* ``resulttype=hits`` gives the exact number of records per product type, and
* a sample of records spread across each type (``startposition`` strides) gives
  the reference schemes, ``dc:format`` and bbox shapes.

    python3 catalogue_survey.py --out sample.jsonl [--per-type 30]
    python3 catalogue_survey.py --report-only sample.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import time
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

#: (family, title LIKE pattern, label).  ``_`` is a single-character wildcard in
#: LIKE, which is harmless here (it can only over-match by one character).
S5P_STREAMS = ("NRTI", "OFFL", "RPRO")
S5P_TYPES = [
    "L1B_RA_BD1", "L1B_RA_BD2", "L1B_RA_BD3", "L1B_RA_BD4", "L1B_RA_BD5", "L1B_RA_BD6", "L1B_RA_BD7", "L1B_RA_BD8",
    "L2__AER_AI", "L2__AER_LH", "L2__CH4___", "L2__CLOUD_", "L2__CO____", "L2__HCHO__", "L2__NO2___",
    "L2__NP_BD3", "L2__NP_BD6", "L2__NP_BD7", "L2__O3____", "L2__O3__PR", "L2__O3_TCL", "L2__SO2___",
]
S3_TYPES = [
    "OL_1_EFR", "OL_1_ERR", "OL_2_LFR", "OL_2_LRR", "OL_2_WFR", "OL_2_WRR",
    "SL_1_RBT", "SL_2_LST", "SL_2_WST", "SL_2_FRP", "SL_2_AOD",
    "SY_2_SYN", "SY_2_VGP", "SY_2_VG1", "SY_2_V10", "SY_2_AOD",
    "SR_1_SRA_A_", "SR_2_LAN_HY", "SR_2_LAN_LI", "SR_2_LAN_SI", "SR_2_WAT",
]


def _get(params: dict[str, str]) -> ET.Element:
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                return ET.fromstring(response.read())
        except Exception as exc:  # network flake: back off and retry
            print(f"  retry {attempt + 1}: {exc}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("catalogue did not answer")


def _query(like: str, result_type: str, start: int = 1, maxrecords: int = 10, element_set: str = "full"):
    return _get({
        "service": "CSW", "version": "2.0.2", "request": "GetRecords",
        "typenames": "csw:Record", "elementsetname": element_set, "resulttype": result_type,
        "maxrecords": str(maxrecords), "startposition": str(start),
        "constraintlanguage": "CQL_TEXT", "constraint_language_version": "1.1.0",
        "constraint": f"dc:title like '{like}'",
        "outputschema": "http://www.opengis.net/cat/csw/2.0.2",
    }).find("csw:SearchResults", NS)


def hits(like: str) -> int:
    return int(_query(like, "hits", maxrecords=1, element_set="brief").get("numberOfRecordsMatched", "0"))


def _records(results: ET.Element):
    for rec in results.findall("csw:Record", NS):
        refs = {r.get("scheme"): (r.text or "").strip() for r in rec.findall("dct:references", NS)}
        yield {
            "id": rec.findtext("dc:identifier", default="", namespaces=NS),
            "title": rec.findtext("dc:title", default="", namespaces=NS),
            "format": rec.findtext("dc:format", default="", namespaces=NS),
            "refs": refs,
            "lower": rec.findtext("ows:BoundingBox/ows:LowerCorner", default="", namespaces=NS),
            "upper": rec.findtext("ows:BoundingBox/ows:UpperCorner", default="", namespaces=NS),
        }


def sample(like: str, total: int, per_type: int):
    """Pages of 10 spread evenly over the result set."""
    pages = max(1, per_type // 10)
    stride = max(10, total // pages)
    for i in range(pages):
        start = min(1 + i * stride, max(1, total - 9))
        yield from _records(_query(like, "results", start=start))


def harvest(per_type: int):
    plan = []
    for stream in S5P_STREAMS:
        for typ in S5P_TYPES:
            plan.append((f"S5P/{stream}/{typ}", f"S5P_{stream}_{typ}_%"))
    for typ in S3_TYPES:
        plan.append((f"S3/{typ}", f"%{typ}%"))
    for label, like in plan:
        total = hits(like)
        print(f"{label:34s} {total:>9d}", file=sys.stderr)
        if total:
            for rec in sample(like, total, per_type):
                rec["label"], rec["total"] = label, total
                yield rec
        else:
            yield {"label": label, "total": 0, "title": "", "id": "", "format": "", "refs": {}, "lower": "", "upper": ""}


def _corner(text: str):
    parts = text.split()
    return (float(parts[0]), float(parts[1])) if len(parts) >= 2 else None


def report(records: list[dict]) -> None:
    by_label: dict[str, list[dict]] = collections.defaultdict(list)
    totals: dict[str, int] = {}
    for r in records:
        by_label[r["label"]].append(r)
        totals[r["label"]] = r["total"]

    print("== Records per product type (exact, from resulttype=hits) and sample statistics")
    print(f"{'type':34s} {'records':>9s}  n  schemes  format  fullglobe poleN poleS  lat-span(median)")
    for label in sorted(by_label):
        sample_ = [r for r in by_label[label] if r["id"]]
        if not sample_:
            print(f"{label:34s} {totals[label]:>9d}")
            continue
        schemes = sorted({s.split(":")[0].split("-")[0] for r in sample_ for s in r["refs"]})
        fmts = sorted({r["format"] for r in sample_})
        full = pole_n = pole_s = 0
        spans = []
        for r in sample_:
            lo, up = _corner(r["lower"]), _corner(r["upper"])
            if not lo or not up:
                continue
            # CSW corner order here is "lat lon" (urn:x-ogc:def:crs:EPSG:6.11:4326)
            if lo[0] <= -89.9 and up[0] >= 89.9 and lo[1] <= -179.9 and up[1] >= 179.9:
                full += 1
            pole_n += up[0] >= 89.9
            pole_s += lo[0] <= -89.9
            spans.append(up[0] - lo[0])
        spans.sort()
        med = spans[len(spans) // 2] if spans else float("nan")
        print(f"{label:34s} {totals[label]:>9d} {len(sample_):>3d}  {','.join(schemes):8s} {','.join(fmts):8s} "
              f"{full:>4d} {pole_n:>5d} {pole_s:>5d}  {med:7.1f}")

    print("\n== Reference schemes seen per family")
    for fam in ("S5P", "S3"):
        c = collections.Counter(
            tuple(sorted(r["refs"])) for r in records if r["id"] and r["label"].startswith(fam)
        )
        for k, v in c.most_common():
            print(f"  {fam}: {v:5d}  {k}")

    print("\n== Detector ambiguity: does the CURRENT regex S[12][A-Z]? fire on S3/S5P text?")
    rx = re.compile(r"S[12](?:[A-Z])?")
    fire = collections.Counter()
    examples = {}
    for r in records:
        if not r["id"]:
            continue
        blob = " ".join([r["id"], r["title"], *r["refs"].values()]).upper()
        m = rx.findall(blob)
        key = (r["label"].split("/")[0], bool(m))
        fire[key] += 1
        if m:
            examples.setdefault(key, (r["id"], sorted(set(m))))
    for k, v in sorted(fire.items()):
        print(f"  {k}: {v}   example={examples.get(k)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--report-only")
    ap.add_argument("--per-type", type=int, default=30)
    args = ap.parse_args()
    if args.report_only:
        records = [json.loads(line) for line in open(args.report_only)]
    else:
        records = []
        with open(args.out, "w") as handle:
            for rec in harvest(args.per_type):
                records.append(rec)
                handle.write(json.dumps(rec) + "\n")
    report(records)


if __name__ == "__main__":
    main()
