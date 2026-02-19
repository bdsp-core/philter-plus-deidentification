#!/bin/bash
#SBATCH --job-name=philter-test
#SBATCH --account=mlscwest
#SBATCH --partition=basic
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=0-00:30:00
#SBATCH --output=logs/philter_test_%j.out
#SBATCH --error=logs/philter_test_%j.err
#SBATCH --mail-type=ALL
#SBATCH --mail-user=YOUR_EMAIL@mgh.harvard.edu

# ============================================================================
# CONFIGURATION
# ============================================================================

SCRATCH_DIR="/vast/scratch/$USER"
MINIFORGE_PATH="$HOME/miniforge3"
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

module load miniforge
source "${MINIFORGE_PATH}/bin/activate"
conda activate philter

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
    echo "Check: logs/philter_test_${SLURM_JOB_ID}.out"
fi

exit $EXIT_CODE
