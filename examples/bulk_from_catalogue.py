#!/usr/bin/env python3
"""Bulk-convert products chosen from the NBS catalogue instead of a directory.

    python examples/bulk_from_catalogue.py --uuid-file uuids.txt --output-dir /out
    python examples/bulk_from_catalogue.py --query "S2C_MSIL2A%T33WXP%" --limit 50 --output-dir /out

Each catalogue record is resolved to the local archive copy of its SAFE zip
(``NBS_ARCHIVE_ROOT``, or ``--archive-root``). Where that file is not present,
the record's download URL is used directly through GDAL's virtual file system
(``/vsizip//vsicurl/https://...``), which reads the product over HTTP range
requests rather than downloading it whole.

Everything after that is :mod:`bulk_convert`: same options, same layout, same
resume behaviour. Needs the ``csw`` extra (``pip install "pysent[csw]"``).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bulk_convert  # noqa: E402  - a sibling example, not an installed module


def resolve_uuids(
    uuids: Sequence[str],
    *,
    endpoint: str | None = None,
    archive_root: str | None = None,
    log=lambda message: print(message, file=sys.stderr, flush=True),
) -> list[str]:
    """Turn catalogue UUIDs into scene references pysent can open."""
    from pysent.archive import resolve_safe_archive_from_uuid

    scenes: list[str] = []
    for uuid in uuids:
        try:
            resolution = resolve_safe_archive_from_uuid(uuid, endpoint=endpoint, archive_root=archive_root)
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop the run
            log(f"  {uuid}: {type(exc).__name__}: {exc}")
            continue
        if resolution.exists:
            scenes.append(str(resolution.safe_path))
        elif resolution.download_url:
            # Read it where it lies; GDAL fetches only the byte ranges it needs.
            scenes.append(f"/vsizip//vsicurl/{resolution.download_url}")
            log(f"  {uuid}: not in the local archive, reading over HTTP")
        else:
            log(f"  {uuid}: no local file and no download URL in the record")
    return scenes


def search_catalogue(query: str, *, endpoint: str | None = None, limit: int = 100) -> list[str]:
    """UUIDs of catalogue records matching a free-text pattern (``%`` wildcards).

    Uses OWSLib directly: pysent's ``csw`` module looks up records by id, it
    does not search.
    """
    from owslib.csw import CatalogueServiceWeb
    from owslib.fes import PropertyIsLike

    from pysent.csw import DEFAULT_NBS_SENTINEL_CSW_ENDPOINT

    catalogue = CatalogueServiceWeb(endpoint or DEFAULT_NBS_SENTINEL_CSW_ENDPOINT, timeout=60)
    uuids: list[str] = []
    start = 1
    while len(uuids) < limit:
        catalogue.getrecords2(
            constraints=[PropertyIsLike("csw:AnyText", query)],
            maxrecords=min(50, limit - len(uuids)),
            startposition=start,
        )
        batch = list(catalogue.records)
        if not batch:
            break
        uuids.extend(str(identifier) for identifier in batch)
        start += len(batch)
    return uuids[:limit]


def build_parser():
    parser = bulk_convert.build_parser()
    parser.description = __doc__.splitlines()[0]
    catalogue = parser.add_argument_group("catalogue")
    catalogue.add_argument("--uuid", nargs="+", metavar="UUID", help="catalogue record identifiers")
    catalogue.add_argument("--uuid-file", type=Path, help="file of identifiers, one per line")
    catalogue.add_argument("--query", help="free-text search, %% as wildcard, e.g. 'S2C_MSIL2A%%T33WXP%%'")
    catalogue.add_argument("--limit", type=int, default=100, help="maximum records for --query (default: 100)")
    catalogue.add_argument("--endpoint", help="CSW endpoint (default: NBS_SENTINEL_CSW_ENDPOINT)")
    catalogue.add_argument("--archive-root", help="local archive mount (default: NBS_ARCHIVE_ROOT)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    uuids: list[str] = list(args.uuid or [])
    if args.uuid_file:
        uuids += [line.strip() for line in args.uuid_file.read_text().splitlines()
                  if line.strip() and not line.startswith("#")]
    if args.query:
        uuids += search_catalogue(args.query, endpoint=args.endpoint, limit=args.limit)
    if not uuids:
        parser.error("give --uuid, --uuid-file and/or --query")

    uuids = list(dict.fromkeys(uuids))
    print(f"resolving {len(uuids)} catalogue record(s)", file=sys.stderr)
    scenes = resolve_uuids(uuids, endpoint=args.endpoint, archive_root=args.archive_root)
    # Anything named on the command line as well is simply added.
    scenes += bulk_convert.discover_scenes(args.input_dir, args.input_list)
    scenes = list(dict.fromkeys(scenes))
    if not scenes:
        print("no scenes resolved", file=sys.stderr)
        return 1

    args.workers = bulk_convert.resolve_workers(
        args.workers,
        threads_per_worker=args.threads_per_worker,
        mem_per_worker_gb=args.mem_per_worker_gb,
    )
    if args.dry_run:
        print(f"{len(scenes)} scene(s), {args.workers} worker(s) x {args.threads_per_worker} GDAL thread(s)")
        for scene in scenes[:20]:
            print(f"  {bulk_convert.scene_family(scene)} {scene}")
        if len(scenes) > 20:
            print(f"  ... and {len(scenes) - 20} more")
        return 0

    summary = bulk_convert.run_bulk(scenes, output_dir=args.output_dir, args=args)
    print(summary.line(), file=sys.stderr)
    return 1 if summary.failed or summary.interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
