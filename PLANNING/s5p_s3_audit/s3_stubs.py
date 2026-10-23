"""Do zero-byte (stub) S3 files correlate with timeliness (NR / ST / NT) and date?

    python3 s3_stubs.py 202609 OL_2_WFR SL_2_LST ...
Samples 30 records per type for the month and tabulates size==0 by timeliness suffix.
"""
import collections, re, sys
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from catalogue_survey import _get, NS, _records
from s3_pick import head_size

month = sys.argv[1]
tab = collections.defaultdict(lambda: [0, 0])
dates_zero = collections.defaultdict(list)
for typ in sys.argv[2:]:
    for start in (1, 11, 21):
        root = _get({"service": "CSW", "version": "2.0.2", "request": "GetRecords", "typenames": "csw:Record",
                     "elementsetname": "full", "resulttype": "results", "maxrecords": "10", "startposition": str(start),
                     "constraintlanguage": "CQL_TEXT", "constraint_language_version": "1.1.0",
                     "constraint": f"dc:title like '%{typ}%{month}%'",
                     "outputschema": "http://www.opengis.net/cat/csw/2.0.2"}).find("csw:SearchResults", NS)
        for rec in _records(root):
            url = rec["refs"].get("WWW:DOWNLOAD-1.0-http--download")
            if not url:
                tab[(typ, "no-url")][1] += 1
                continue
            m = re.search(r"_(NR|ST|NT)_\d{3}$", rec["title"])
            tl = m.group(1) if m else "?"
            sz = head_size(url)
            tab[(typ, tl)][1] += 1
            if sz == 0:
                tab[(typ, tl)][0] += 1
                dates_zero[(typ, tl)].append(rec["title"].split("____")[1][:8])
for k in sorted(tab):
    z, n = tab[k]
    print(f"{k[0]:10s} {k[1]:6s} stubs {z:2d}/{n:2d}  dates(zero) {sorted(set(dates_zero[k]))[:12]}")
