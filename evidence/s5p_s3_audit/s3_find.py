"""Find recent, non-empty, typical-size products of one S3 type near a bbox.

    python3 s3_find.py OL_2_WFR 202609 [--bbox 0,60,30,75] [--n 10]

Prints title, uuid, size (HEAD), bbox.  Stubs (0 bytes) are flagged.
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from catalogue_survey import _get, NS, _records  # noqa: E402
from s3_pick import head_size  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("type")
    ap.add_argument("month", help="YYYYMM prefix of the sensing start")
    ap.add_argument("--bbox", default="")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--start", type=int, default=1)
    args = ap.parse_args()
    like = f"%{args.type}%{args.month}%"
    constraint = f"dc:title like '{like}'"
    if args.bbox:
        x0, y0, x1, y1 = args.bbox.split(",")
        constraint += f" AND BBOX(ows:BoundingBox, {x0}, {y0}, {x1}, {y1})"
    root = _get({
        "service": "CSW", "version": "2.0.2", "request": "GetRecords",
        "typenames": "csw:Record", "elementsetname": "full", "resulttype": "results",
        "maxrecords": "10", "startposition": str(args.start),
        "constraintlanguage": "CQL_TEXT", "constraint_language_version": "1.1.0",
        "constraint": constraint, "outputschema": "http://www.opengis.net/cat/csw/2.0.2",
    }).find("csw:SearchResults", NS)
    print("matched", root.get("numberOfRecordsMatched"))
    for rec in _records(root):
        url = rec["refs"].get("WWW:DOWNLOAD-1.0-http--download", "")
        size = head_size(url) if url else -1
        print(f"{rec['title']}  {rec['id']}  {size/1e6:.1f} MB  [{rec['lower']} | {rec['upper']}]  {url}")


if __name__ == "__main__":
    main()
