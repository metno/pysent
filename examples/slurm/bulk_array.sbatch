#!/bin/bash
# Convert a list of SAFE products with a Slurm array job: one array task takes
# every Nth line of the list, and inside a task one worker process handles one
# scene at a time.
#
#   sbatch --array=0-9 examples/slurm/bulk_array.sbatch scenes.txt /out
#
# Sizing: --cpus-per-task and --mem below are per array task. bulk_convert sizes
# its workers from exactly those (SLURM_CPUS_PER_TASK and SLURM_MEM_PER_NODE),
# so a task with 16 CPUs and 24 GB runs 8 workers x 2 GDAL threads. Budget about
# 1.2 GB per worker, plus roughly 1.2 GB of scratch per scene in flight.
#
#SBATCH --job-name=pysent-bulk
#SBATCH --cpus-per-task=16
#SBATCH --mem=24G
#SBATCH --time=04:00:00
#SBATCH --output=logs/pysent-bulk-%A_%a.out
#SBATCH --error=logs/pysent-bulk-%A_%a.out

set -euo pipefail

SCENE_LIST="${1:?usage: sbatch --array=0-N bulk_array.sbatch <scene-list> <output-dir> [extra bulk_convert args]}"
OUTPUT_DIR="${2:?usage: sbatch --array=0-N bulk_array.sbatch <scene-list> <output-dir> [extra bulk_convert args]}"
shift 2

TASK_ID="${SLURM_ARRAY_TASK_ID:-0}"
TASK_COUNT="${SLURM_ARRAY_TASK_COUNT:-1}"
EXAMPLES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Node-local scratch for the intermediates: they are written once, read once and
# deleted, so keep them off the shared filesystem.
SCRATCH="${SLURM_TMPDIR:-${TMPDIR:-/tmp}}/pysent-${SLURM_JOB_ID:-local}-${TASK_ID}"
mkdir -p "$SCRATCH" "$OUTPUT_DIR" logs
trap 'rm -rf "$SCRATCH"' EXIT

# This task's share of the list: line numbers where (n - 1) % TASK_COUNT == TASK_ID.
SHARE="$SCRATCH/scenes.txt"
awk -v task="$TASK_ID" -v count="$TASK_COUNT" \
    'NF && $0 !~ /^[[:space:]]*#/ { if ((n++ % count) == task) print }' "$SCENE_LIST" > "$SHARE"

echo "array task $TASK_ID of $TASK_COUNT: $(wc -l < "$SHARE") scene(s), ${SLURM_CPUS_PER_TASK:-?} CPUs"
if [ ! -s "$SHARE" ]; then
    echo "nothing to do for this task"
    exit 0
fi

# --workers auto reads SLURM_CPUS_PER_TASK and SLURM_MEM_PER_NODE.
# A non-zero exit means at least one scene failed: check the results log.
srun python3 "$EXAMPLES_DIR/bulk_convert.py" \
    --input-list "$SHARE" \
    --output-dir "$OUTPUT_DIR" \
    --scratch "$SCRATCH" \
    --workers auto \
    --threads-per-worker 2 \
    --results-log "$OUTPUT_DIR/bulk_results.${SLURM_JOB_ID:-local}_${TASK_ID}.jsonl" \
    "$@"
