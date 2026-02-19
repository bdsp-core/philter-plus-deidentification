#!/bin/bash
#
# ONE-TIME SETUP on Martinos MLSC cluster
#
# Run this ONCE from the login node after cloning the repo.
# Follow the Martinos miniforge guide before running this:
# https://it.martinos.org/help/migrating-anaconda-miniconda-install-to-a-miniforge-install/
# https://it.martinos.org/help/python/
#
# Usage:
#   bash setup_cluster_env.sh
#

set -e

MINIFORGE_PATH="$HOME/miniforge3"

echo "=========================================="
echo "Philter Environment Setup (Martinos MLSC)"
echo "=========================================="
echo ""

# ============================================================================
# Step 1: Load miniforge module
# ============================================================================

echo "Step 1: Loading miniforge..."
module load miniforge
source "${MINIFORGE_PATH}/bin/activate"
echo "  ✓ miniforge loaded: $(conda --version)"

# ============================================================================
# Step 2: Create conda environment
# ============================================================================

echo ""
echo "Step 2: Creating 'philter' environment (Python 3.9)..."

if conda env list | grep -q "^philter "; then
    echo "  ✓ Environment 'philter' already exists — skipping creation"
else
    conda create -n philter python=3.9 -y
    echo "  ✓ Created environment 'philter'"
fi

# ============================================================================
# Step 3: Install packages
# ============================================================================

echo ""
echo "Step 3: Installing packages..."
conda activate philter

pip install --upgrade pip --quiet

# Parquet + S3
pip install pyarrow pandas --quiet

# Project requirements
if [ -f requirements.txt ]; then
    pip install -r requirements.txt --quiet
    echo "  ✓ requirements.txt installed"
else
    echo "  ⚠ requirements.txt not found — installing known dependencies"
    pip install nltk regex --quiet
fi

echo "  ✓ All packages installed"

# ============================================================================
# Step 4: Verify
# ============================================================================

echo ""
echo "Step 4: Verifying..."
python3 -c "
import pyarrow, pandas
print('  ✓ pyarrow:', pyarrow.__version__)
print('  ✓ pandas:', pandas.__version__)
"

# Try importing philter
python3 -c "import philter; print('  ✓ philter: importable')" 2>/dev/null || \
    echo "  ⚠ philter not importable — check you are in the repo root"

python3 -c "import keyword_removal; print('  ✓ keyword_removal: importable')" 2>/dev/null || \
    echo "  ⚠ keyword_removal not importable — check you are in the repo root"

# ============================================================================
# Step 5: Create scratch folder
# ============================================================================

echo ""
echo "Step 5: Setting up scratch directory..."
SCRATCH_DIR="/vast/scratch/$USER"
mkdir -p "${SCRATCH_DIR}/I0001_Notes"
mkdir -p "${SCRATCH_DIR}/philter-deidentify/output"
mkdir -p "${SCRATCH_DIR}/philter-deidentify/logs"
chmod 700 "$SCRATCH_DIR"
echo "  ✓ Scratch: $SCRATCH_DIR"
echo "  ✓ Input dir:  ${SCRATCH_DIR}/I0001_Notes   ← copy your data here"
echo "  ✓ Output dir: ${SCRATCH_DIR}/philter-deidentify/output"

# ============================================================================
# Summary
# ============================================================================

echo ""
echo "=========================================="
echo "Setup complete."
echo ""
echo "Next steps:"
echo ""
echo "  1. Copy data to scratch:"
echo "     rsync -av /your/local/data/ ${SCRATCH_DIR}/I0001_Notes/"
echo ""
echo "  2. Create logs dir in repo:"
echo "     mkdir -p logs"
echo ""
echo "  3. Edit YOUR_EMAIL in slurm_job.sl and test_slurm_job.sl"
echo ""
echo "  4. SSH to job-submission node:"
echo "     ssh mlsc"
echo ""
echo "  5. Run test job first:"
echo "     sbatch test_slurm_job.sl"
echo ""
echo "  6. Check log:"
echo "     tail -f logs/philter_test_<jobid>.out"
echo ""
echo "  7. If test passes, run full job:"
echo "     sbatch slurm_job.sl"
echo "=========================================="
