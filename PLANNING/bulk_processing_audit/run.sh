#!/usr/bin/env bash
# Re-run the audit evidence for PLANNING/PLANNING_bulk_processing.md.
#
#   PLANNING/bulk_processing_audit/run.sh checks            # bug checks, synthetic data only
#   PLANNING/bulk_processing_audit/run.sh bench <DATA_DIR>  # benchmarks, needs the two real products
#
# DATA_DIR must hold the S2/S1 zips named in bench.py (download URLs are in
# tests/data/manifest.json). Everything runs in ubuntu:24.04 with apt GDAL 3.8,
# the same geo stack CI uses, pinned to 8 cores.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
image=pysent-audit:gdal38
docker build -q -t "$image" "$here" >/dev/null

out="$(mktemp -d)"
run() {
    # HOME=/tmp: numba needs a writable cache dir or `import pysent.s1` fails (finding B6).
    docker run --rm --cpuset-cpus=0-7 --user "$(id -u):$(id -g)" -e HOME=/tmp \
        -v "$repo:/src:ro" -v "$here:/s:ro" -v "$out:/out" "$@" 2>&1 \
        | grep -v "warnings.warn\|FutureWarning"
}

case "${1:-}" in
    checks)
        for check in s2_nodata_collision s2_all_nodata cachemax numba_threads s1_jpeg_alpha \
                     nested_pools s1_tps_option s2_percentiles_option; do
            echo "### $check"
            run --cpus=4 "$image" timeout 120 python3 /s/checks.py "$check" || true
        done
        ;;
    bench)
        data="$(cd "${2:?usage: run.sh bench <DATA_DIR>}" && pwd)"
        for bench in zip_members jp2_decode s2_one s2_one_60m warp_vs_translate s2_three s1_default s1_polynomial; do
            echo "### $bench"
            run -v "$data:/data:ro" "$image" python3 /s/bench.py "$bench"
        done
        echo "### jp2_decode with GDAL_NUM_THREADS=1"
        run -v "$data:/data:ro" -e GDAL_NUM_THREADS=1 "$image" python3 /s/bench.py jp2_decode
        ;;
    *)
        sed -n '2,9p' "$0"
        exit 2
        ;;
esac
rm -rf "$out"
