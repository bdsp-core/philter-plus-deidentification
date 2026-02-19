#!/bin/bash
#
# SLURM TEST JOB - Validates environment, data, and de-identification pipeline
#
# Cost: minimal (~5 minutes on 1 node)
#
# Usage:
#   sbatch test_slurm_job.sh
#

# ============================================================================
# SLURM DIRECTIVES
# ============================================================================
#SBATCH --job-name=philter-test
#SBATCH --partition=basic
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=00:30:00
#SBATCH --output=logs/philter_test_%j.out
#SBATCH --error=logs/philter_test_%j.err

# ============================================================================
# CONFIGURATION
# ============================================================================

SCRATCH_DIR="/scratch/$USER"
PHILTER_CONFIG="configs/philter_one.json"
SAMPLE_SIZE=100
WORKERS=4

# ============================================================================
# RUN TEST
# ============================================================================

echo "=========================================="
echo "Philter Cluster Test Job"
echo "=========================================="
echo "Date:   $(date)"
echo "Node:   $SLURM_NODELIST"
echo "Job ID: $SLURM_JOB_ID"
echo "=========================================="

# Activate conda environment
source ~/.bashrc
conda activate philter 2>/dev/null || {
    echo "ERROR: conda environment 'philter' not found."
    echo "Run: bash setup_cluster_env.sh"
    exit 1
}

echo "Python: $(which python3) ($(python3 --version))"
echo ""

cd "$SLURM_SUBMIT_DIR"

python3 test_cluster.py \
    --scratch-dir "$SCRATCH_DIR" \
    --sample-size $SAMPLE_SIZE \
    --workers $WORKERS \
    --philter-config "$PHILTER_CONFIG"

EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "TEST PASSED at $(date)"
else
    echo "TEST FAILED (exit code $EXIT_CODE) at $(date)"
    echo "Check logs: logs/philter_test_${SLURM_JOB_ID}.out"
fi

exit $EXIT_CODE
