"""Old vs proposed platform detection, on real catalogue records of all four families.

Ground truth is the leading token of the record title (S1A_.., S2B_.., S3A_.., S5P_..).
Detection is fed what the service feeds it: identifier, title, and the reference URLs.

    python3 detect_audit.py SAMPLE.jsonl [--fresh]    # --fresh also harvests S1/S2 records
"""
from __future__ import annotations

import collections
import json
import re
import sys

import catalogue_survey as cs

OLD = re.compile(r"S[12](?:[A-Z])?")
# Proposed: a whole token, not preceded or followed by a letter/digit.  Underscore, slash,
# dot, dash and start/end are boundaries.  S5P carries no unit letter.
NEW = re.compile(r"(?<![A-Z0-9])S(?:5P|[123][A-Z]?)(?![A-Z0-9])")


def detect(rx, *values, families):
    token = ""
    for value in values:
        if not value:
            continue
        matches = rx.findall(value.upper())
        if matches:
            exact = next((m for m in matches if len(m) == 3), None)
            token = exact or matches[0]
            if exact:
                break
    fam = token[:3] if token.startswith("S5P") else token[:2]
    return fam if fam in families else "unknown"


def truth(title: str) -> str:
    m = re.match(r"S(5P|[123])", title)
    return "S" + m.group(1) if m else "?"


def fresh(per_type: int):
    out = []
    for label, like in (("S1/GRD", "S1%_GRD%"), ("S1/SLC", "S1%_SLC%"), ("S2/MSIL1C", "S2%MSIL1C%"), ("S2/MSIL2A", "S2%MSIL2A%")):
        total = cs.hits(like)
        if not total:
            continue
        for rec in cs.sample(like, total, per_type):
            rec["label"], rec["total"] = label, total
            out.append(rec)
        print(f"  {label}: {total} records", file=sys.stderr)
    return out


def main() -> None:
    records = [json.loads(line) for line in open(sys.argv[1])]
    records = [r for r in records if r["id"]]
    if "--fresh" in sys.argv:
        records += fresh(40)
    print(f"{len(records)} records")
    for name, rx, fams in (("OLD (S1/S2 only)", OLD, {"S1", "S2"}), ("NEW (S1/S2/S3/S5P)", NEW, {"S1", "S2", "S3", "S5P"})):
        conf = collections.Counter()
        for r in records:
            urls = list(r["refs"].values())
            got = detect(rx, r["id"], r["title"], *urls, families=fams)
            conf[(truth(r["title"]), got)] += 1
        print(f"\n== {name}: truth -> detected")
        for (t, g), v in sorted(conf.items()):
            print(f"  {t:4s} -> {g:8s} {v:5d}{'   <-- WRONG' if t != g else ''}")
    # URL / path only (what remains when the service only has a file path)
    conf = collections.Counter()
    for r in records:
        u = r["refs"].get("WWW:DOWNLOAD-1.0-http--download", "")
        if not u:
            continue
        path = u.split("nbsArchive/")[-1]
        conf[(truth(r["title"]), detect(NEW, path, families={"S1", "S2", "S3", "S5P"}))] += 1
    print("\n== NEW, archive path only: truth -> detected")
    for (t, g), v in sorted(conf.items()):
        print(f"  {t:4s} -> {g:8s} {v:5d}{'   <-- WRONG' if t != g else ''}")


if __name__ == "__main__":
    main()
