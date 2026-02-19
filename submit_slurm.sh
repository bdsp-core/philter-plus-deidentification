#!/bin/bash
#
# Submit and monitor Philter SLURM array job
#
# Usage:
#   ./submit_slurm.sh
#

set -e

echo "=========================================="
echo "Philter SLURM Submission"
echo "=========================================="

# ============================================================================
# Pre-flight checks
# ============================================================================

SCRATCH_DIR="/scratch/$USER"

echo ""
echo "1. Checking scratch directory..."
if [ -d "$SCRATCH_DIR" ]; then
    echo "   ✓ Scratch: $SCRATCH_DIR"
    SCRATCH_FREE=$(df -h "$SCRATCH_DIR" | awk 'NR==2{print $4}')
    echo "   Free space: $SCRATCH_FREE"
else
    echo "   ✗ Scratch dir not found: $SCRATCH_DIR"
    echo "   Update SCRATCH_DIR in slurm_job.sh and this script"
    exit 1
fi

if [ -d "${SCRATCH_DIR}/I0001_Notes" ]; then
    echo "   ✓ Input data found: ${SCRATCH_DIR}/I0001_Notes"
else
    echo "   ✗ Input data NOT found: ${SCRATCH_DIR}/I0001_Notes"
    echo "   Copy your data to scratch first."
    exit 1
fi

echo ""
echo "2. Checking conda environment..."
conda activate philter 2>/dev/null && echo "   ✓ philter env found" || {
    echo "   ✗ philter conda environment not found"
    echo "   Run: bash setup_cluster_env.sh"
    exit 1
}

echo ""
echo "2. Checking required files..."
for f in process_parquet_aws.py philter.py keyword_removal.py configs/philter_one.json slurm_job.sh; do
    if [ -f "$f" ]; then
        echo "   ✓ $f"
    else
        echo "   ✗ $f NOT FOUND"
        exit 1
    fi
done

echo ""
echo "3. Creating logs directory..."
mkdir -p logs
echo "   ✓ logs/"

# ============================================================================
# Show current queue status
# ============================================================================

echo ""
echo "4. Current queue (your jobs):"
squeue -u $USER 2>/dev/null || echo "   (no jobs running)"

# ============================================================================
# Show partition info
# ============================================================================

echo ""
echo "5. Partition availability:"
sinfo -p basic --format="%-10P %-5a %-10l %-6D %-8C %-8m" 2>/dev/null || true

# ============================================================================
# Submit
# ============================================================================

echo ""
echo "About to submit: 8-task array job on 'basic' partition"
echo "  Each task: 1 node, 24 cores, 350GB RAM, up to 96 hours"
echo ""
read -p "Submit? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
fi

JOB_ID=$(sbatch --parsable slurm_job.sh)
echo ""
echo "=========================================="
echo "✓ Submitted job array: $JOB_ID"
echo "=========================================="
echo ""
echo "Monitor commands:"
echo "  squeue -u $USER                          # job status"
echo "  squeue -j $JOB_ID                        # this job"
echo "  tail -f logs/philter_${JOB_ID}_0.out     # task 0 log"
echo "  scancel $JOB_ID                          # cancel all tasks"
echo ""
echo "Tasks:"
for i in $(seq 0 7); do
    SUBFOLDERS=("Notes_parquet_15_and_before" "Notes_parquet_16_17" "Notes_parquet_18_19" "Notes_parquet_20" "Notes_parquet_21" "Notes_parquet_22" "Notes_parquet_23" "Notes_parquet_24")
    echo "  Task $i → ${SUBFOLDERS[$i]}"
done
echo ""
echo "Job ID saved — to cancel: scancel $JOB_ID"
echo $JOB_ID > /tmp/philter-slurm-jobid.txt
