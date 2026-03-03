#!/bin/bash
# Run generate_stats.py for all 8 input subfolders → combined stats_all.csv
# Upload result to S3 when done.
# Run from: /home/ec2-user/philter-plus-deidentification/

set -e

BUCKET="s3://bdsp-site-mgb"
INPUT_BASE="${BUCKET}/I0001_Notes"
OUTPUT_BASE="${BUCKET}/philter-deidentify/output"
STATS_FILE="/home/ec2-user/stats_all.csv"
LOG="/home/ec2-user/stats.log"
PYTHON=$(command -v python3.9 || command -v python3)

cd /home/ec2-user/philter-plus-deidentification

echo "=============================" | tee -a $LOG
echo "Stats generation started: $(date)" | tee -a $LOG
echo "=============================" | tee -a $LOG

# Wait for P1-helper (partition_7_half_b) to finish uploading to S3
echo "Waiting for partition_7_half_b to appear on S3..." | tee -a $LOG
while true; do
    COUNT=$(aws s3 ls ${OUTPUT_BASE}/partition_7_half_b/ | grep -c "batch_" || true)
    if [ "$COUNT" -gt 1 ]; then
        echo "partition_7_half_b ready ($COUNT batch files). Proceeding." | tee -a $LOG
        break
    fi
    echo "  Not ready yet ($COUNT files). Waiting 60s..." | tee -a $LOG
    sleep 60
done

# ---------------------------------------------------------------
# Subfolder 1: Notes_parquet_15_and_before → partition_0
# ---------------------------------------------------------------
echo "" | tee -a $LOG
echo "[1/8] Notes_parquet_15_and_before" | tee -a $LOG
$PYTHON generate_stats.py \
    --input-path  "${INPUT_BASE}/Notes_parquet_15_and_before/" \
    --output-paths "${OUTPUT_BASE}/partition_0/" \
    --stats-file  "$STATS_FILE" \
    2>&1 | tee -a $LOG

# ---------------------------------------------------------------
# Subfolder 2: Notes_parquet_16_17 → partition_1 + partition_2
# ---------------------------------------------------------------
echo "" | tee -a $LOG
echo "[2/8] Notes_parquet_16_17" | tee -a $LOG
$PYTHON generate_stats.py \
    --input-path  "${INPUT_BASE}/Notes_parquet_16_17/" \
    --output-paths "${OUTPUT_BASE}/partition_1/,${OUTPUT_BASE}/partition_2/" \
    --stats-file  "$STATS_FILE" \
    --append \
    2>&1 | tee -a $LOG

# ---------------------------------------------------------------
# Subfolder 3: Notes_parquet_18_19 → partition_3,4,5,6
# ---------------------------------------------------------------
echo "" | tee -a $LOG
echo "[3/8] Notes_parquet_18_19" | tee -a $LOG
$PYTHON generate_stats.py \
    --input-path  "${INPUT_BASE}/Notes_parquet_18_19/" \
    --output-paths "${OUTPUT_BASE}/partition_3/,${OUTPUT_BASE}/partition_4/,${OUTPUT_BASE}/partition_5/,${OUTPUT_BASE}/partition_6/" \
    --stats-file  "$STATS_FILE" \
    --append \
    2>&1 | tee -a $LOG

# ---------------------------------------------------------------
# Subfolder 4: Notes_parquet_20 → partition_7 + half_a + half_b + part09
# ---------------------------------------------------------------
echo "" | tee -a $LOG
echo "[4/8] Notes_parquet_20" | tee -a $LOG
$PYTHON generate_stats.py \
    --input-path  "${INPUT_BASE}/Notes_parquet_20/" \
    --output-paths "${OUTPUT_BASE}/partition_7/,${OUTPUT_BASE}/partition_7_half_a/,${OUTPUT_BASE}/partition_7_half_b/,${OUTPUT_BASE}/partition_7_part09/" \
    --stats-file  "$STATS_FILE" \
    --append \
    2>&1 | tee -a $LOG

# ---------------------------------------------------------------
# Subfolder 5: Notes_parquet_21 → partition_8 + partition_9
# ---------------------------------------------------------------
echo "" | tee -a $LOG
echo "[5/8] Notes_parquet_21" | tee -a $LOG
$PYTHON generate_stats.py \
    --input-path  "${INPUT_BASE}/Notes_parquet_21/" \
    --output-paths "${OUTPUT_BASE}/partition_8/,${OUTPUT_BASE}/partition_9/" \
    --stats-file  "$STATS_FILE" \
    --append \
    2>&1 | tee -a $LOG

# ---------------------------------------------------------------
# Subfolder 6: Notes_parquet_22 → partition_10 + partition_11
# ---------------------------------------------------------------
echo "" | tee -a $LOG
echo "[6/8] Notes_parquet_22" | tee -a $LOG
$PYTHON generate_stats.py \
    --input-path  "${INPUT_BASE}/Notes_parquet_22/" \
    --output-paths "${OUTPUT_BASE}/partition_10/,${OUTPUT_BASE}/partition_11/" \
    --stats-file  "$STATS_FILE" \
    --append \
    2>&1 | tee -a $LOG

# ---------------------------------------------------------------
# Subfolder 7: Notes_parquet_23 → partition_12,13,14
# ---------------------------------------------------------------
echo "" | tee -a $LOG
echo "[7/8] Notes_parquet_23" | tee -a $LOG
$PYTHON generate_stats.py \
    --input-path  "${INPUT_BASE}/Notes_parquet_23/" \
    --output-paths "${OUTPUT_BASE}/partition_12/,${OUTPUT_BASE}/partition_13/,${OUTPUT_BASE}/partition_14/" \
    --stats-file  "$STATS_FILE" \
    --append \
    2>&1 | tee -a $LOG

# ---------------------------------------------------------------
# Subfolder 8: Notes_parquet_24 → partition_15,16,17,18
# ---------------------------------------------------------------
echo "" | tee -a $LOG
echo "[8/8] Notes_parquet_24" | tee -a $LOG
$PYTHON generate_stats.py \
    --input-path  "${INPUT_BASE}/Notes_parquet_24/" \
    --output-paths "${OUTPUT_BASE}/partition_15/,${OUTPUT_BASE}/partition_16/,${OUTPUT_BASE}/partition_17/,${OUTPUT_BASE}/partition_18/" \
    --stats-file  "$STATS_FILE" \
    --append \
    2>&1 | tee -a $LOG

# ---------------------------------------------------------------
# Upload stats_all.csv to S3
# ---------------------------------------------------------------
echo "" | tee -a $LOG
echo "Uploading stats_all.csv to S3..." | tee -a $LOG
aws s3 cp "$STATS_FILE" "${BUCKET}/philter-deidentify/stats_all.csv"
echo "Done: $(date)" | tee -a $LOG
echo "Stats available at: ${BUCKET}/philter-deidentify/stats_all.csv" | tee -a $LOG
