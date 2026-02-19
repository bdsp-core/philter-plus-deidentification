#!/bin/bash
#
# ONE-TIME SETUP: Create conda environment on the SLURM cluster
#
# Run this ONCE before submitting slurm_job.sh:
#   bash setup_cluster_env.sh
#

set -e

echo "=========================================="
echo "Setting up Philter conda environment"
echo "=========================================="

# Load conda (adjust module name to what your cluster uses)
# Try common module names:
module load miniconda 2>/dev/null || \
module load anaconda 2>/dev/null || \
module load conda 2>/dev/null || {
    echo "No conda module found. Trying conda in PATH..."
}

# Create environment
echo ""
echo "Creating conda environment 'philter' (Python 3.9)..."
conda create -n philter python=3.9 -y

# Activate
source activate philter || conda activate philter

echo "Installing packages..."
pip install --upgrade pip

# Core de-identification packages
pip install pyarrow pandas boto3 s3fs

# Install project requirements
if [ -f requirements.txt ]; then
    pip install -r requirements.txt
else
    echo "Warning: requirements.txt not found, installing known dependencies..."
    pip install nltk spacy regex
fi

echo ""
echo "=========================================="
echo "Environment setup complete."
echo ""
echo "Verify with:"
echo "  conda activate philter"
echo "  python3 -c 'import pyarrow, pandas, boto3; print(\"OK\")'"
echo ""
echo "Now submit jobs:"
echo "  mkdir -p logs"
echo "  sbatch slurm_job.sh"
echo "=========================================="
