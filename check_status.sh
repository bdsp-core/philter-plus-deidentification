#!/bin/bash
#
# Check status of all Philter de-identification jobs
#
# Usage:
#   ./check_status.sh
#

BUCKET="bdsp-site-mgb"
OUTPUT_PREFIX="philter-deidentify"
AWS_PROFILE="bidmc"
REGION="us-east-1"
NUM_INSTANCES=4

echo "=========================================="
echo "Philter De-identification Job Status"
echo "$(date)"
echo "=========================================="

# -----------------------------------------------
# 1. Instance Status
# -----------------------------------------------
echo ""
echo "1. EC2 Instance Status:"
echo "-------------------------------------------"

aws ec2 --profile $AWS_PROFILE describe-instances \
    --region $REGION \
    --filters "Name=tag:Project,Values=Philter-Deidentify" "Name=instance-state-name,Values=running,stopped,stopping" \
    --query 'Reservations[].Instances[].[InstanceId,State.Name,PublicIpAddress,Tags[?Key==`Partition`].Value|[0],LaunchTime]' \
    --output table 2>/dev/null || echo "  No instances found"

# -----------------------------------------------
# 2. Completion Logs
# -----------------------------------------------
echo ""
echo "2. Completed Partitions:"
echo "-------------------------------------------"

COMPLETED=$(aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$OUTPUT_PREFIX/logs/ --region $REGION 2>/dev/null | grep "partition_.*_complete.log" | wc -l | tr -d ' ')
echo "  $COMPLETED / $NUM_INSTANCES partitions complete"
echo ""

aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$OUTPUT_PREFIX/logs/ --region $REGION 2>/dev/null | grep "complete" || echo "  (none yet)"

# Show completion details
for i in $(seq 0 $(($NUM_INSTANCES - 1))); do
    CONTENT=$(aws s3 --profile $AWS_PROFILE cp s3://$BUCKET/$OUTPUT_PREFIX/logs/partition_${i}_complete.log - 2>/dev/null) || true
    if [ -n "$CONTENT" ]; then
        echo "  Partition $i: $CONTENT"
    fi
done

# -----------------------------------------------
# 3. Checkpoints (progress per partition)
# -----------------------------------------------
echo ""
echo "3. Partition Progress (from checkpoints):"
echo "-------------------------------------------"

CHECKPOINT_FILES=$(aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$OUTPUT_PREFIX/output/ --recursive --region $REGION 2>/dev/null | grep "checkpoint.json" || true)

if [ -z "$CHECKPOINT_FILES" ]; then
    echo "  No checkpoints found yet (processing may not have started)"
else
    echo "$CHECKPOINT_FILES" | while read -r line; do
        KEY=$(echo "$line" | awk '{print $4}')
        if [ -n "$KEY" ]; then
            FOLDER=$(echo "$KEY" | sed "s|$OUTPUT_PREFIX/output/||" | sed 's|/checkpoint.json||')
            CHECKPOINT=$(aws s3 --profile $AWS_PROFILE cp "s3://$BUCKET/$KEY" - 2>/dev/null) || true
            if [ -n "$CHECKPOINT" ]; then
                COUNT=$(echo "$CHECKPOINT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f\"{d['processed_count']:,} records (batch {d['batch_num']}, {d['timestamp']})\")" 2>/dev/null) || COUNT="$CHECKPOINT"
                echo "  $FOLDER: $COUNT"
            fi
        fi
    done
fi

# -----------------------------------------------
# 4. Output Files
# -----------------------------------------------
echo ""
echo "4. Output Files:"
echo "-------------------------------------------"

OUTPUT_SUMMARY=$(aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$OUTPUT_PREFIX/output/ --recursive --summarize --region $REGION 2>/dev/null | tail -2)
echo "  $OUTPUT_SUMMARY"

# Count parquet files
PARQUET_COUNT=$(aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$OUTPUT_PREFIX/output/ --recursive --region $REGION 2>/dev/null | grep ".parquet" | wc -l | tr -d ' ')
echo "  Parquet files written: $PARQUET_COUNT"

# -----------------------------------------------
# Summary
# -----------------------------------------------
echo ""
echo "=========================================="
if [ "$COMPLETED" -eq "$NUM_INSTANCES" ] 2>/dev/null; then
    echo "✓ ALL PARTITIONS COMPLETE!"
    echo ""
    echo "Download results:"
    echo "  aws s3 --profile $AWS_PROFILE sync s3://$BUCKET/$OUTPUT_PREFIX/output/ ./deidentified_output/"
    echo ""
    echo "Terminate instances:"
    echo "  aws ec2 --profile $AWS_PROFILE terminate-instances --region $REGION --instance-ids \$(cat /tmp/philter-instance-ids.txt)"
else
    echo "In progress... ($COMPLETED/$NUM_INSTANCES complete)"
    echo "Run again later: ./check_status.sh"
fi
echo "=========================================="
