"""Pick one real, non-empty product per Sentinel-3 type (HEAD content-length).

For each type, sample records spread over the catalogue, HEAD the zip, report
stubs (0 bytes) and choose the smallest non-empty one.  Output: JSON lines.

    python3 s3_pick.py OL_2_WFR SY_2_VG1 ... > picks.jsonl
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from catalogue_survey import _query, _records, hits  # noqa: E402


def head_size(url: str) -> int:
    out = subprocess.run(["curl", "-sI", "-m", "20", url], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.lower().startswith("content-length"):
            return int(line.split(":")[1])
    return -1


def main() -> None:
    for typ in sys.argv[1:]:
        like = f"%{typ}%"
        total = hits(like)
        cands = []
        stride = max(10, total // 6)
        for i in range(6):
            start = min(1 + i * stride, max(1, total - 9))
            for rec in _records(_query(like, "results", start=start)):
                url = rec["refs"].get("WWW:DOWNLOAD-1.0-http--download")
                if url:
                    rec["url"] = url
                    rec["size"] = head_size(url)
                    cands.append(rec)
        stubs = sum(1 for c in cands if c["size"] == 0)
        ok = sorted((c for c in cands if c["size"] > 0), key=lambda c: c["size"])
        pick = ok[0] if ok else None
        print(json.dumps({"type": typ, "total": total, "sampled": len(cands), "stubs": stubs,
                          "sizes_mb": sorted(round(c["size"] / 1e6, 1) for c in cands),
                          "pick": pick}), flush=True)


if __name__ == "__main__":
    main()
