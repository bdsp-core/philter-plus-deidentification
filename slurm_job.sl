#!/bin/bash
#SBATCH --job-name=philter-deid
#SBATCH --account=mlscwest
#SBATCH --array=0-7%4
#SBATCH --partition=dgx-a100
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=120
#SBATCH --mem=256G
#SBATCH --time=4-00:00:00
#SBATCH --output=logs/philter_%A_%a.out
#SBATCH --error=logs/philter_%A_%a.err
#SBATCH --mail-type=ALL
#SBATCH --mail-user=YOUR_EMAIL@mgh.harvard.edu

# ============================================================================
# CONFIGURATION - EDIT THESE
# ============================================================================

SCRATCH_DIR="/vast/scratch/$USER"
INPUT_BASE="${SCRATCH_DIR}/I0001_Notes"
OUTPUT_BASE="${SCRATCH_DIR}/philter-deidentify/output"
MINIFORGE_PATH="$HOME/miniforge3"
PHILTER_CONFIG="configs/philter_one.json"
WORKERS=60    # 120 caused OOM; 60 workers stable at ~40 GB memory

# ============================================================================
# SUBFOLDER ASSIGNMENTS (array task ID → subfolder)
# ============================================================================

SUBFOLDERS=(
    "Notes_parquet_15_and_before"
    "Notes_parquet_16_17"
    "Notes_parquet_18_19"
    "Notes_parquet_20"
    "Notes_parquet_21"
    "Notes_parquet_22"
    "Notes_parquet_23"
    "Notes_parquet_24"
)

SUBFOLDER="${SUBFOLDERS[$SLURM_ARRAY_TASK_ID]}"
INPUT_PATH="${INPUT_BASE}/${SUBFOLDER}/"
OUTPUT_PATH="${OUTPUT_BASE}/${SUBFOLDER}/"

# ============================================================================
# ENVIRONMENT
# ============================================================================

echo "=========================================="
echo "Philter SLURM Job"
echo "=========================================="
echo "Date:      $(date)"
echo "Node:      $SLURM_NODELIST"
echo "Task ID:   $SLURM_ARRAY_TASK_ID"
echo "Subfolder: $SUBFOLDER"
echo "Input:     $INPUT_PATH"
echo "Output:    $OUTPUT_PATH"
echo "Workers:   $WORKERS"
echo "CPUs:      $SLURM_CPUS_PER_TASK"
echo "=========================================="

module load miniforge
source "${MINIFORGE_PATH}/bin/activate"
conda activate philter

echo "Python: $(which python3) ($(python3 --version))"

# ============================================================================
# RUN DE-IDENTIFICATION
# ============================================================================

cd "$SLURM_SUBMIT_DIR"

echo ""
echo "Starting processing at $(date)"
echo ""

python3 process_parquet_aws.py \
    --input-path "$INPUT_PATH" \
    --output-path "$OUTPUT_PATH" \
    --partition-id $SLURM_ARRAY_TASK_ID \
    --workers $WORKERS \
    --philter-config "$PHILTER_CONFIG"

EXIT_CODE=$?

echo ""
echo "=========================================="
if [ $EXIT_CODE -eq 0 ]; then
    echo "COMPLETED SUCCESSFULLY at $(date)"
    echo "Task $SLURM_ARRAY_TASK_ID: $SUBFOLDER"
else
    echo "FAILED (exit code $EXIT_CODE) at $(date)"
    echo "Task $SLURM_ARRAY_TASK_ID: $SUBFOLDER"
fi
echo "=========================================="

exit $EXIT_CODE
