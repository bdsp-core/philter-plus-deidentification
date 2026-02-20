#!/bin/bash
#
# PRODUCTION DEPLOYMENT - Full de-identification on 4 × c6i.32xlarge
#
# Same approach as test_ec2_schema.sh:
#   1. Create tarball of project locally
#   2. For each instance: SCP tarball, SSH in, install deps, start processing
#
# Usage:
#   ./deploy_production.sh
#
# Monitoring:
#   ./check_status.sh
#

set -e

BUCKET="bdsp-site-mgb"
INPUT_PREFIX="I0001_Notes/"
REGION="us-east-1"
AWS_PROFILE="bidmc"
OUTPUT_PREFIX="philter-deidentify"
KEY_FILE="/Users/anjanarayapureddy/Desktop/Philter/philter.pem"
WORKERS=120
BATCH_SIZE=10000

# 4 × c6i.32xlarge instances
WORKER_IPS=(
    "100.53.244.90"      # Worker-0
    "34.206.72.182"      # Worker-1
    "44.203.218.128"     # Worker-2
    "44.211.88.59"       # Worker-3
)

NUM_INSTANCES=${#WORKER_IPS[@]}

echo "=========================================="
echo "Philter Production Deployment"
echo "=========================================="
echo ""
echo "Will deploy to $NUM_INSTANCES instances:"
for i in $(seq 0 $((NUM_INSTANCES - 1))); do
    echo "  Worker-$i: ${WORKER_IPS[$i]}"
done
echo ""
echo "  Input:   s3://$BUCKET/$INPUT_PREFIX"
echo "  Output:  s3://$BUCKET/$OUTPUT_PREFIX/output/"
echo "  Workers: $WORKERS per instance ($((WORKERS * NUM_INSTANCES)) total)"
echo ""
read -p "Continue? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then echo "Aborted."; exit 1; fi

# ============================================================================
# Get bidmc credentials
# ============================================================================

echo ""
echo "Retrieving credentials from $AWS_PROFILE profile..."
AWS_ACCESS_KEY=$(aws configure get aws_access_key_id --profile $AWS_PROFILE 2>/dev/null) || true
AWS_SECRET_KEY=$(aws configure get aws_secret_access_key --profile $AWS_PROFILE 2>/dev/null) || true
AWS_SESSION_TOKEN=$(aws configure get aws_session_token --profile $AWS_PROFILE 2>/dev/null) || true

if [ -z "$AWS_ACCESS_KEY" ] || [ -z "$AWS_SECRET_KEY" ]; then
    echo "✗ Could not retrieve credentials from $AWS_PROFILE profile"
    echo "  Make sure ~/.aws/credentials has a [$AWS_PROFILE] section with valid keys"
    exit 1
fi
echo "✓ Credentials retrieved (Access Key: ${AWS_ACCESS_KEY:0:10}...)"

# ============================================================================
# Create tarball of project and upload partition assignments
# ============================================================================

echo ""
echo "Creating project tarball..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARBALL="/tmp/philter-project.tar.gz"

tar -czf "$TARBALL" \
    -C "$(dirname "$SCRIPT_DIR")" \
    --exclude='.git' \
    --exclude='data' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.pem' \
    --exclude='.env' \
    --exclude='*.tar.gz' \
    "$(basename "$SCRIPT_DIR")"

TARBALL_SIZE=$(du -h "$TARBALL" | cut -f1)
echo "✓ Tarball created: $TARBALL ($TARBALL_SIZE)"

# Create partition assignments
echo ""
echo "Creating partition assignments..."
SUBFOLDERS=$(aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$INPUT_PREFIX \
    --region $REGION | grep "PRE " | awk '{print $2}')

SUBFOLDER_ARRAY=($SUBFOLDERS)
TOTAL=${#SUBFOLDER_ARRAY[@]}
echo "  Found $TOTAL subfolders"

PER_INSTANCE=$(( ($TOTAL + $NUM_INSTANCES - 1) / $NUM_INSTANCES ))

for i in $(seq 0 $((NUM_INSTANCES - 1))); do
    START=$(( i * PER_INSTANCE ))
    > /tmp/partition_${i}.txt
    for j in $(seq $START $(( START + PER_INSTANCE - 1 ))); do
        if [ $j -lt $TOTAL ]; then
            echo "s3://$BUCKET/$INPUT_PREFIX${SUBFOLDER_ARRAY[$j]}" >> /tmp/partition_${i}.txt
        fi
    done
    COUNT=$(grep -c . /tmp/partition_${i}.txt 2>/dev/null || echo 0)
    echo "  Partition $i: $COUNT subfolders"
    cat /tmp/partition_${i}.txt | sed 's/^/    /'
    aws s3 --profile $AWS_PROFILE cp /tmp/partition_${i}.txt \
        s3://$BUCKET/$OUTPUT_PREFIX/assignments/partition_${i}.txt \
        --region $REGION --quiet
done
echo "✓ Assignments uploaded to S3"

# ============================================================================
# Deploy to each instance (same pattern as test_ec2_schema.sh)
# ============================================================================

for PARTITION in $(seq 0 $((NUM_INSTANCES - 1))); do

    IP=${WORKER_IPS[$PARTITION]}

    echo ""
    echo "=========================================="
    echo "Deploying Worker-$PARTITION ($IP)"
    echo "=========================================="

    echo ""
    echo "Uploading project to Worker-$PARTITION via SCP..."
    scp -i "$KEY_FILE" \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=30 \
        "$TARBALL" ec2-user@$IP:/tmp/philter-project.tar.gz

    echo "✓ Project uploaded to Worker-$PARTITION"

    echo ""
    echo "Connecting to Worker-$PARTITION ($IP) ..."

    ssh -i "$KEY_FILE" \
        -o StrictHostKeyChecking=no \
        -o ConnectTimeout=30 \
        -o ServerAliveInterval=60 \
        ec2-user@$IP bash <<REMOTE
set -e

echo "=========================================="
echo "Worker-$PARTITION Setup Started: \$(date)"
echo "=========================================="

# -----------------------------------------------
# Step 1: Install dependencies
# -----------------------------------------------
echo ""
echo "Step 1: Installing dependencies..."

# Check if Python 3.9 is available, install if not
if ! command -v python3.9 &> /dev/null; then
    echo "Installing Python 3.9..."
    sudo amazon-linux-extras install python3.9 -y 2>/dev/null || sudo yum install python39 -y 2>/dev/null || true
fi

# Install pip for Python 3.9
sudo yum install -y python39-pip 2>/dev/null || true
if ! python3.9 -m pip --version &> /dev/null; then
    curl -sS https://bootstrap.pypa.io/get-pip.py | sudo python3.9
fi

# Use whichever python3 is available
PYTHON=\$(command -v python3.9 || command -v python3)
echo "Using Python: \$PYTHON (\$(\$PYTHON --version))"

# Install pip packages
\$PYTHON -m pip install --quiet --upgrade pip 2>/dev/null || true
\$PYTHON -m pip install --quiet pyarrow pandas boto3 s3fs nltk 2>/dev/null || \
    \$PYTHON -m pip install pyarrow pandas boto3 s3fs nltk

# Download NLTK data required by Philter (POS tagging for NER)
echo "Downloading NLTK data..."
\$PYTHON -c "import nltk; nltk.download('averaged_perceptron_tagger', quiet=True); nltk.download('averaged_perceptron_tagger_eng', quiet=True); nltk.download('punkt', quiet=True); nltk.download('punkt_tab', quiet=True); print('NLTK data downloaded')"

echo "✓ Dependencies installed"

# -----------------------------------------------
# Step 2: Extract project from tarball
# -----------------------------------------------
echo ""
echo "Step 2: Setting up Philter project..."

cd /home/ec2-user
rm -rf philter-plus-deidentification
tar -xzf /tmp/philter-project.tar.gz
cd philter-plus-deidentification

# Install requirements if present
if [ -f "requirements.txt" ]; then
    \$PYTHON -m pip install --quiet -r requirements.txt 2>/dev/null || true
fi

echo "✓ Project extracted and ready"

# -----------------------------------------------
# Step 3: Configure AWS credentials
# -----------------------------------------------
echo ""
echo "Step 3: Configuring AWS credentials..."

mkdir -p ~/.aws
cat > ~/.aws/credentials <<CREDS
[default]
aws_access_key_id = ${AWS_ACCESS_KEY}
aws_secret_access_key = ${AWS_SECRET_KEY}
aws_session_token = ${AWS_SESSION_TOKEN}
CREDS
cat > ~/.aws/config <<CONF
[default]
region = ${REGION}
output = json
CONF
chmod 600 ~/.aws/credentials
echo "✓ AWS credentials configured"

# -----------------------------------------------
# Step 4: Download partition assignment
# -----------------------------------------------
echo ""
echo "Step 4: Downloading partition assignment..."

aws s3 cp s3://${BUCKET}/${OUTPUT_PREFIX}/assignments/partition_${PARTITION}.txt /tmp/my_subfolders.txt
echo "Subfolders assigned to Worker-$PARTITION:"
cat /tmp/my_subfolders.txt

# -----------------------------------------------
# Step 5: Start processing in background (nohup)
# -----------------------------------------------
echo ""
echo "Step 5: Starting de-identification processing..."
echo "  Workers: ${WORKERS}"
echo "  Batch size: ${BATCH_SIZE}"
echo "  Config: configs/philter_one.json"

cd /home/ec2-user/philter-plus-deidentification

PYTHON=\$(command -v python3.9 || command -v python3)

sudo touch /var/log/deidentify.log
sudo chown ec2-user:ec2-user /var/log/deidentify.log

nohup bash -c '
PYTHON=\$(command -v python3.9 || command -v python3)
SUBFOLDER_NUM=0
TOTAL_START=\$(date +%s)

echo "=========================================="
echo "Partition ${PARTITION} processing started: \$(date)"
echo "=========================================="

while IFS= read -r SUBFOLDER_PATH; do
    [ -z "\$SUBFOLDER_PATH" ] && continue

    FOLDER_NAME=\$(echo "\$SUBFOLDER_PATH" | sed "s|s3://||" | tr "/" "_" | sed "s/_\$//")

    echo ""
    echo "=============================="
    echo "Processing: \$SUBFOLDER_PATH"
    echo "Output to:  s3://${BUCKET}/${OUTPUT_PREFIX}/output/\${FOLDER_NAME}/"
    echo "Started at: \$(date)"
    echo "=============================="

    \$PYTHON process_parquet_aws.py \
        --input-path "\$SUBFOLDER_PATH" \
        --output-path "s3://${BUCKET}/${OUTPUT_PREFIX}/output/\${FOLDER_NAME}/" \
        --workers ${WORKERS} \
        --batch-size ${BATCH_SIZE} \
        --philter-config configs/philter_one.json \
        2>&1

    SUBFOLDER_NUM=\$(( SUBFOLDER_NUM + 1 ))
    echo "Completed subfolder \$SUBFOLDER_NUM at \$(date)"

done < /tmp/my_subfolders.txt

TOTAL_END=\$(date +%s)
TOTAL_ELAPSED=\$(( TOTAL_END - TOTAL_START ))
TOTAL_HOURS=\$(echo "scale=1; \$TOTAL_ELAPSED / 3600" | bc)

echo "=========================================="
echo "ALL SUBFOLDERS COMPLETE at \$(date)"
echo "Partition ${PARTITION} finished"
echo "Total subfolders: \$SUBFOLDER_NUM"
echo "Total time: \${TOTAL_HOURS} hours"
echo "=========================================="

echo "Partition ${PARTITION} completed at \$(date). Subfolders: \$SUBFOLDER_NUM. Time: \${TOTAL_HOURS}h" \
    | tee /tmp/processing-complete.log
aws s3 cp /tmp/processing-complete.log \
    s3://${BUCKET}/${OUTPUT_PREFIX}/logs/partition_${PARTITION}_complete.log
aws s3 cp /var/log/deidentify.log \
    s3://${BUCKET}/${OUTPUT_PREFIX}/logs/partition_${PARTITION}_deidentify.log

' >> /var/log/deidentify.log 2>&1 &

PID=\$!
echo \$PID > /tmp/deidentify.pid

echo ""
echo "=========================================="
echo "Worker-$PARTITION STARTED"
echo "  PID: \$PID"
echo "  Log: tail -f /var/log/deidentify.log"
echo "=========================================="
REMOTE

    echo "✓ Worker-$PARTITION deployed and running on $IP"

done

# ============================================================================
# SUMMARY
# ============================================================================

echo ""
echo ""
echo "=========================================="
echo "✓ PRODUCTION DEPLOYMENT COMPLETE"
echo "=========================================="
echo ""
echo "All $NUM_INSTANCES workers are now processing in background."
echo ""
echo "Monitoring:"
echo ""
echo "  1. Quick status:       ./check_status.sh"
echo ""
echo "  2. Completion logs:    aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$OUTPUT_PREFIX/logs/"
echo ""
echo "  3. Output files:       aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$OUTPUT_PREFIX/output/ --recursive --summarize"
echo ""
echo "  4. SSH to any worker:"
for i in $(seq 0 $((NUM_INSTANCES - 1))); do
    echo "     Worker-$i: ssh -i $KEY_FILE ec2-user@${WORKER_IPS[$i]}"
done
echo "     Then:   tail -f /var/log/deidentify.log"
echo ""
echo "  5. Download results:"
echo "     aws s3 --profile $AWS_PROFILE sync s3://$BUCKET/$OUTPUT_PREFIX/output/ ./deidentified_output/"
echo ""
echo "=========================================="
