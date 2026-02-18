#!/bin/bash
#
# CLEANUP SCRIPT - Terminate instances and optionally delete S3 data
#
# Usage:
#   ./cleanup_aws.sh                    # Terminate instances only
#   ./cleanup_aws.sh --delete-s3        # Also delete S3 bucket
#

set -e

REGION="us-east-1"
AWS_PROFILE="bidmc"
DELETE_S3=false

# Parse arguments
if [[ "$1" == "--delete-s3" ]]; then
    DELETE_S3=true
fi

echo "=========================================="
echo "AWS Philter Cleanup Script"
echo "=========================================="
echo ""

# ============================================================================
# Find Philter Instances
# ============================================================================

echo "Finding Philter instances..."

INSTANCE_IDS=$(aws ec2 --profile $AWS_PROFILE describe-instances \
    --region $REGION \
    --filters \
        'Name=tag:Project,Values=Philter-Deidentify' \
        'Name=instance-state-name,Values=running,pending,stopping,stopped' \
    --query 'Reservations[].Instances[].InstanceId' \
    --output text)

if [ -z "$INSTANCE_IDS" ]; then
    echo "No Philter instances found."
else
    echo "Found instances: $INSTANCE_IDS"
    echo ""

    # Show instance details
    aws ec2 --profile $AWS_PROFILE describe-instances \
        --region $REGION \
        --instance-ids $INSTANCE_IDS \
        --query 'Reservations[].Instances[].[InstanceId,State.Name,Tags[?Key==`Name`].Value|[0],Tags[?Key==`Partition`].Value|[0]]' \
        --output table

    echo ""
    read -p "Terminate these instances? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Terminating instances..."
        aws ec2 --profile $AWS_PROFILE terminate-instances --region $REGION --instance-ids $INSTANCE_IDS
        echo "✓ Instances terminated"
    else
        echo "Skipped instance termination"
    fi
fi

# ============================================================================
# Find and Delete S3 Bucket
# ============================================================================

if [ "$DELETE_S3" = true ]; then
    BUCKET="bdsp-site-mgb"
    OUTPUT_PREFIX="philter-deidentify"

    echo ""
    echo "⚠️  This deletes de-identified OUTPUT only (not your original data)"
    echo "   Deleting: s3://$BUCKET/$OUTPUT_PREFIX/"
    echo "   SAFE:     s3://$BUCKET/I0001_Notes/ will NOT be touched"
    echo ""
    read -p "Delete output s3://$BUCKET/$OUTPUT_PREFIX/ ? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "Deleting s3://$BUCKET/$OUTPUT_PREFIX/..."
        aws s3 --profile $AWS_PROFILE rm s3://$BUCKET/$OUTPUT_PREFIX/ --recursive --region $REGION
        echo "✓ Deleted s3://$BUCKET/$OUTPUT_PREFIX/"
    else
        echo "Skipped output deletion"
        echo ""
        echo "To download results first:"
        echo "  aws s3 --profile $AWS_PROFILE sync s3://$BUCKET/$OUTPUT_PREFIX/output/ ./deidentified_output/"
    fi
fi

# ============================================================================
# SSH Key Pair
# ============================================================================

echo ""
KEY_NAME="philter-key"
KEY_FILE="./philter-key.pem"
read -p "Delete EC2 key pair '$KEY_NAME' from AWS? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    aws ec2 --profile $AWS_PROFILE delete-key-pair \
        --region $REGION \
        --key-name $KEY_NAME 2>/dev/null || true
    echo "✓ Key pair deleted from AWS: $KEY_NAME"
    echo ""
    if [ -f "$KEY_FILE" ]; then
        read -p "Delete local PEM file ($KEY_FILE)? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            rm -f "$KEY_FILE"
            echo "✓ PEM file deleted: $KEY_FILE"
        else
            echo "PEM file kept: $KEY_FILE"
        fi
    fi
else
    echo "Skipped key pair deletion"
fi

# ============================================================================
# Summary
# ============================================================================

echo ""
echo "=========================================="
echo "✓ CLEANUP COMPLETE"
echo "=========================================="
echo ""
echo "What was cleaned:"
if [ -n "$INSTANCE_IDS" ]; then
    echo "  ✓ Terminated instances"
fi
if [ "$DELETE_S3" = true ]; then
    echo "  ✓ Deleted S3 buckets"
else
    echo "  - S3 buckets not deleted (use --delete-s3 flag)"
fi
echo ""
