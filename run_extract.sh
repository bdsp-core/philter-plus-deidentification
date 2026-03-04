#!/bin/bash
# Extract not-deidentified records from all 8 input subfolders.
# Writes filtered parquet files to s3://bdsp-site-mgb/philter-deidentify/rerun_input/
# Run from: /home/ec2-user/philter-plus-deidentification/

set -e

BUCKET="s3://bdsp-site-mgb"
INPUT_BASE="${BUCKET}/I0001_Notes"
OUTPUT_BASE="${BUCKET}/philter-deidentify/output"
RERUN_BASE="${BUCKET}/philter-deidentify/rerun_input"
LOG="/home/ec2-user/extract.log"
PYTHON=$(command -v python3.9 || command -v python3)

cd /home/ec2-user/philter-plus-deidentification

echo "======================================" | tee -a $LOG
echo "Extraction started: $(date)" | tee -a $LOG
echo "======================================" | tee -a $LOG

# [1/8] Notes_parquet_15_and_before → partition_0
echo "" | tee -a $LOG
echo "[1/8] Notes_parquet_15_and_before" | tee -a $LOG
$PYTHON extract_not_deidentified.py \
    --input-path   "${INPUT_BASE}/Notes_parquet_15_and_before/" \
    --output-paths "${OUTPUT_BASE}/partition_0/" \
    --rerun-path   "${RERUN_BASE}/Notes_parquet_15_and_before/" \
    2>&1 | tee -a $LOG

# [2/8] Notes_parquet_16_17 → partition_1 + partition_2
echo "" | tee -a $LOG
echo "[2/8] Notes_parquet_16_17" | tee -a $LOG
$PYTHON extract_not_deidentified.py \
    --input-path   "${INPUT_BASE}/Notes_parquet_16_17/" \
    --output-paths "${OUTPUT_BASE}/partition_1/,${OUTPUT_BASE}/partition_2/" \
    --rerun-path   "${RERUN_BASE}/Notes_parquet_16_17/" \
    2>&1 | tee -a $LOG

# [3/8] Notes_parquet_18_19 → partition_3,4,5,6
echo "" | tee -a $LOG
echo "[3/8] Notes_parquet_18_19" | tee -a $LOG
$PYTHON extract_not_deidentified.py \
    --input-path   "${INPUT_BASE}/Notes_parquet_18_19/" \
    --output-paths "${OUTPUT_BASE}/partition_3/,${OUTPUT_BASE}/partition_4/,${OUTPUT_BASE}/partition_5/,${OUTPUT_BASE}/partition_6/" \
    --rerun-path   "${RERUN_BASE}/Notes_parquet_18_19/" \
    2>&1 | tee -a $LOG

# [4/8] Notes_parquet_20 → partition_7 + half_a + half_b + part09
echo "" | tee -a $LOG
echo "[4/8] Notes_parquet_20" | tee -a $LOG
$PYTHON extract_not_deidentified.py \
    --input-path   "${INPUT_BASE}/Notes_parquet_20/" \
    --output-paths "${OUTPUT_BASE}/partition_7/,${OUTPUT_BASE}/partition_7_half_a/,${OUTPUT_BASE}/partition_7_half_b/,${OUTPUT_BASE}/partition_7_part09/" \
    --rerun-path   "${RERUN_BASE}/Notes_parquet_20/" \
    2>&1 | tee -a $LOG

# [5/8] Notes_parquet_21 → partition_8 + partition_9
echo "" | tee -a $LOG
echo "[5/8] Notes_parquet_21" | tee -a $LOG
$PYTHON extract_not_deidentified.py \
    --input-path   "${INPUT_BASE}/Notes_parquet_21/" \
    --output-paths "${OUTPUT_BASE}/partition_8/,${OUTPUT_BASE}/partition_9/" \
    --rerun-path   "${RERUN_BASE}/Notes_parquet_21/" \
    2>&1 | tee -a $LOG

# [6/8] Notes_parquet_22 → partition_10 + partition_11
echo "" | tee -a $LOG
echo "[6/8] Notes_parquet_22" | tee -a $LOG
$PYTHON extract_not_deidentified.py \
    --input-path   "${INPUT_BASE}/Notes_parquet_22/" \
    --output-paths "${OUTPUT_BASE}/partition_10/,${OUTPUT_BASE}/partition_11/" \
    --rerun-path   "${RERUN_BASE}/Notes_parquet_22/" \
    2>&1 | tee -a $LOG

# [7/8] Notes_parquet_23 → partition_12,13,14
echo "" | tee -a $LOG
echo "[7/8] Notes_parquet_23" | tee -a $LOG
$PYTHON extract_not_deidentified.py \
    --input-path   "${INPUT_BASE}/Notes_parquet_23/" \
    --output-paths "${OUTPUT_BASE}/partition_12/,${OUTPUT_BASE}/partition_13/,${OUTPUT_BASE}/partition_14/" \
    --rerun-path   "${RERUN_BASE}/Notes_parquet_23/" \
    2>&1 | tee -a $LOG

# [8/8] Notes_parquet_24 → partition_15,16,17,18
echo "" | tee -a $LOG
echo "[8/8] Notes_parquet_24" | tee -a $LOG
$PYTHON extract_not_deidentified.py \
    --input-path   "${INPUT_BASE}/Notes_parquet_24/" \
    --output-paths "${OUTPUT_BASE}/partition_15/,${OUTPUT_BASE}/partition_16/,${OUTPUT_BASE}/partition_17/,${OUTPUT_BASE}/partition_18/" \
    --rerun-path   "${RERUN_BASE}/Notes_parquet_24/" \
    2>&1 | tee -a $LOG

echo "" | tee -a $LOG
echo "======================================" | tee -a $LOG
echo "Extraction complete: $(date)" | tee -a $LOG
echo "Rerun input at: ${RERUN_BASE}/" | tee -a $LOG
echo "======================================" | tee -a $LOG
