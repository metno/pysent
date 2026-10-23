"""Inspect a remote Sentinel-3 .zip WITHOUT downloading it.

* reads the zip central directory with HTTP range requests (bytes fetched are
  counted) and prints every member: uncompressed size, compressed size, and
  whether it is STORED or DEFLATED -- this decides whether /vsizip//vsicurl/
  can range-read a single variable efficiently;
* parses ``xfdumanifest.xml``: product type, processing baseline / version,
  start/stop times, footprint posList (closed? crosses the antimeridian?).

    python3 s3_zipinfo.py URL [--manifest]
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile


class RangeFile(io.RawIOBase):
    """Read-only seekable file over HTTP range requests."""

    def __init__(self, url: str):
        self.url = url
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=60) as r:
            self.size = int(r.headers["Content-Length"])
        self.pos = 0
        self.fetched = 0
        self.requests = 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def read(self, n=-1):
        if n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        req = urllib.request.Request(self.url, headers={"Range": f"bytes={self.pos}-{self.pos + n - 1}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        self.pos += len(data)
        self.fetched += len(data)
        self.requests += 1
        return data

    def readinto(self, b):
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


def manifest_facts(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)
    facts: dict = {}

    def first(local):
        for el in root.iter():
            if el.tag.split("}")[-1] == local:
                return el
        return None

    for tag in ("productName", "productType", "baselineCollection", "processingVersion"):
        pass
    # Attributes of interest anywhere in the tree
    for el in root.iter():
        local = el.tag.split("}")[-1]
        if local in ("productName", "productType", "baselineCollection", "baseline", "creationTime",
                     "startTime", "stopTime", "orbitNumber", "relativeOrbitNumber", "cycleNumber",
                     "processingName", "processingTime", "softwareName", "softwareVersion", "facilityName"):
            txt = (el.text or "").strip()
            if txt:
                facts.setdefault(local, txt)
            for k, v in el.attrib.items():
                if k in ("version", "name", "site", "organisation", "facilityName", "baselineCollection"):
                    facts.setdefault(f"{local}@{k}", v)
    pos = None
    for el in root.iter():
        if el.tag.split("}")[-1] == "posList":
            pos = (el.text or "").split()
            break
    if pos:
        vals = list(map(float, pos))
        pts = list(zip(vals[0::2], vals[1::2]))  # (lat, lon) in GML 4326 order for S3 manifests
        facts["footprint_npoints"] = len(pts)
        facts["footprint_closed"] = pts[0] == pts[-1]
        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        facts["footprint_lat"] = (min(lats), max(lats))
        facts["footprint_lon"] = (min(lons), max(lons))
        jumps = [abs(b - a) for a, b in zip(lons, lons[1:])]
        facts["footprint_max_lon_jump"] = max(jumps) if jumps else 0
        facts["footprint_crosses_antimeridian"] = bool(jumps) and max(jumps) > 180
    return facts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--manifest", action="store_true")
    args = ap.parse_args()
    rf = RangeFile(args.url)
    zf = zipfile.ZipFile(rf)
    infos = zf.infolist()
    print(f"zip size {rf.size/1e6:.1f} MB, {len(infos)} entries; central directory cost: "
          f"{rf.requests} requests, {rf.fetched/1e3:.0f} kB")
    comp = {zipfile.ZIP_STORED: "STORED", zipfile.ZIP_DEFLATED: "DEFLATED"}
    tot_u = tot_c = 0
    for i in infos:
        if i.is_dir():
            continue
        tot_u += i.file_size
        tot_c += i.compress_size
        name = i.filename.split("/")[-1]
        print(f"  {name:60s} {i.file_size/1e6:9.2f} MB  -> {i.compress_size/1e6:9.2f} MB  {comp.get(i.compress_type, i.compress_type)}")
    print(f"  total uncompressed {tot_u/1e6:.1f} MB, compressed {tot_c/1e6:.1f} MB")
    if args.manifest:
        man = next(i for i in infos if i.filename.endswith("xfdumanifest.xml"))
        facts = manifest_facts(zf.read(man))
        for k, v in facts.items():
            print(f"  manifest {k} = {v}")


if __name__ == "__main__":
    main()
