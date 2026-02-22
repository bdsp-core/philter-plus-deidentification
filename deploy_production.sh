#!/bin/bash
#
# PRODUCTION DEPLOYMENT - Full de-identification on 4 × c6i.32xlarge
#
# Same approach as test_ec2_schema.sh:
#   1. Create tarball of project locally
#   2. For each instance: SCP tarball, SSH in, install deps
#   3. Copy data FROM S3 to EC2 local disk (AWS session = 12hrs)
#   4. Run de-identification on local data (runs ~3 days, no S3 needed)
#   5. After completion, refresh credentials and copy results back to S3
#
# Usage:
#   ./deploy_production.sh
#
# Monitoring:
#   ./check_status.sh
#   ssh -i <key> ec2-user@<ip> "tail -f /var/log/deidentify.log"
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
    "34.238.27.34"       # Worker-0
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
echo "  Output:  local on each EC2, then upload to S3 later"
echo "  Workers: $WORKERS per instance ($((WORKERS * NUM_INSTANCES)) total)"
echo ""
echo "Data flow:"
echo "  1. Copy Parquet files from S3 → EC2 local disk (within 12hr session)"
echo "  2. De-identify locally (~3 days, no S3 access needed)"
echo "  3. After completion, refresh credentials and upload results to S3"
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
# Create tarball of project
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

# ============================================================================
# Create partition assignments (9 subfolders → 4 instances)
# ============================================================================

echo ""
echo "Creating partition assignments..."
SUBFOLDERS=$(aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$INPUT_PREFIX \
    --region $REGION | grep "PRE " | awk '{print $2}')

SUBFOLDER_ARRAY=($SUBFOLDERS)
TOTAL=${#SUBFOLDER_ARRAY[@]}
echo "  Found $TOTAL subfolders"

# Round-robin assignment so all instances get work
for i in $(seq 0 $((NUM_INSTANCES - 1))); do
    > /tmp/partition_${i}.txt
done
for j in $(seq 0 $((TOTAL - 1))); do
    INSTANCE=$(( j % NUM_INSTANCES ))
    echo "${SUBFOLDER_ARRAY[$j]}" >> /tmp/partition_${INSTANCE}.txt
done

for i in $(seq 0 $((NUM_INSTANCES - 1))); do
    COUNT=$(grep -c . /tmp/partition_${i}.txt 2>/dev/null || echo 0)
    echo "  Partition $i: $COUNT subfolders"
    cat /tmp/partition_${i}.txt | sed 's/^/    /'
done
echo "✓ Partition assignments created"

# ============================================================================
# Deploy to each instance (same pattern as test_ec2_schema.sh)
# ============================================================================

for PARTITION in $(seq 0 $((NUM_INSTANCES - 1))); do

    IP=${WORKER_IPS[$PARTITION]}
    PARTITION_FILE="/tmp/partition_${PARTITION}.txt"

    echo ""
    echo "=========================================="
    echo "Deploying Worker-$PARTITION ($IP)"
    echo "=========================================="

    # Read subfolder names for this partition
    SUBFOLDERS_LIST=$(cat "$PARTITION_FILE" | tr '\n' ' ')

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
# Step 4: Start download + processing in background
#         Everything runs inside nohup — SSH exits immediately
# -----------------------------------------------
echo ""
echo "Step 4: Starting background job (S3 download + processing)..."

mkdir -p /home/ec2-user/input
mkdir -p /home/ec2-user/output

cd /home/ec2-user/philter-plus-deidentification

PYTHON=\$(command -v python3.9 || command -v python3)

sudo touch /var/log/deidentify.log
sudo chown ec2-user:ec2-user /var/log/deidentify.log

nohup bash -c '
PYTHON=\$(command -v python3.9 || command -v python3)

echo "=========================================="
echo "Worker-${PARTITION} background job started: \$(date)"
echo "=========================================="

# ----- Phase 1: Download data from S3 to local disk -----
echo ""
echo "PHASE 1: Downloading data from S3 to local disk..."
echo "  (AWS credentials valid for ~12 hours)"
echo ""

SUBFOLDERS="${SUBFOLDERS_LIST}"
for SUBFOLDER in \$SUBFOLDERS; do
    CLEAN_NAME=\$(echo "\$SUBFOLDER" | sed "s|/$||")
    echo "  Downloading: s3://${BUCKET}/${INPUT_PREFIX}\${SUBFOLDER}"
    echo "  Destination: /home/ec2-user/input/\${CLEAN_NAME}/"
    mkdir -p /home/ec2-user/input/\${CLEAN_NAME}
    aws s3 sync "s3://${BUCKET}/${INPUT_PREFIX}\${SUBFOLDER}" \
        "/home/ec2-user/input/\${CLEAN_NAME}/"
    FILE_COUNT=\$(ls /home/ec2-user/input/\${CLEAN_NAME}/*.parquet 2>/dev/null | wc -l)
    FOLDER_SIZE=\$(du -sh /home/ec2-user/input/\${CLEAN_NAME}/ | cut -f1)
    echo "  Downloaded: \${FILE_COUNT} files, \${FOLDER_SIZE}"
    echo ""
done

TOTAL_SIZE=\$(du -sh /home/ec2-user/input/ | cut -f1)
echo "All data downloaded: \${TOTAL_SIZE}"
echo "Disk usage:"
df -h /home/ec2-user | head -2
echo ""

# ----- Phase 2: Process locally (no S3 needed) -----
echo "=========================================="
echo "PHASE 2: De-identification started: \$(date)"
echo "All data is LOCAL - no AWS access needed from here"
echo "=========================================="

cd /home/ec2-user/philter-plus-deidentification
SUBFOLDER_NUM=0
TOTAL_START=\$(date +%s)

for INPUT_DIR in /home/ec2-user/input/*/; do
    [ ! -d "\$INPUT_DIR" ] && continue

    FOLDER_NAME=\$(basename "\$INPUT_DIR")
    OUTPUT_DIR="/home/ec2-user/output/\${FOLDER_NAME}"
    mkdir -p "\$OUTPUT_DIR"

    echo ""
    echo "=============================="
    echo "Processing: \$INPUT_DIR"
    echo "Output to:  \$OUTPUT_DIR"
    echo "Started at: \$(date)"
    echo "=============================="

    \$PYTHON process_parquet_aws.py \
        --input-path "\$INPUT_DIR" \
        --output-path "\$OUTPUT_DIR" \
        --workers ${WORKERS} \
        --batch-size ${BATCH_SIZE} \
        --philter-config configs/philter_one.json \
        2>&1

    SUBFOLDER_NUM=\$(( SUBFOLDER_NUM + 1 ))
    echo ""
    echo "Completed subfolder \$SUBFOLDER_NUM (\$FOLDER_NAME) at \$(date)"

done

TOTAL_END=\$(date +%s)
TOTAL_ELAPSED=\$(( TOTAL_END - TOTAL_START ))
TOTAL_HOURS=\$(echo "scale=1; \$TOTAL_ELAPSED / 3600" | bc)

echo ""
echo "=========================================="
echo "ALL SUBFOLDERS COMPLETE at \$(date)"
echo "Partition ${PARTITION} finished"
echo "Total subfolders: \$SUBFOLDER_NUM"
echo "Total time: \${TOTAL_HOURS} hours"
echo "=========================================="
echo ""
echo "OUTPUT IS LOCAL at /home/ec2-user/output/"
echo "To upload to S3, refresh credentials and run:"
echo "  aws s3 sync /home/ec2-user/output/ s3://${BUCKET}/${OUTPUT_PREFIX}/output/"
echo ""
du -sh /home/ec2-user/output/*/

' >> /var/log/deidentify.log 2>&1 &

PID=\$!
echo \$PID > /tmp/deidentify.pid

echo ""
echo "=========================================="
echo "Worker-$PARTITION STARTED IN BACKGROUND"
echo "  PID: \$PID"
echo "  Log: tail -f /var/log/deidentify.log"
echo ""
echo "  Phase 1: Downloading from S3 (runs now)"
echo "  Phase 2: De-identification (starts after download)"
echo "  Both phases run in background — SSH will exit"
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
echo "All $NUM_INSTANCES workers are now processing LOCALLY."
echo "AWS credentials are NOT needed during processing."
echo ""
echo "Data flow:"
echo "  ✓ S3 → EC2 local disk     (done during setup)"
echo "  → Local de-identification  (running now, ~3 days)"
echo "  → EC2 local disk → S3     (you do this after completion)"
echo ""
echo "Monitoring:"
echo ""
echo "  SSH to any worker:"
for i in $(seq 0 $((NUM_INSTANCES - 1))); do
    echo "     Worker-$i: ssh -i $KEY_FILE ec2-user@${WORKER_IPS[$i]}"
done
echo "     Then:   tail -f /var/log/deidentify.log"
echo ""
echo "After completion (~3 days):"
echo ""
echo "  1. Refresh AWS credentials in ~/.aws/credentials"
echo ""
echo "  2. Upload results from each instance:"
for i in $(seq 0 $((NUM_INSTANCES - 1))); do
    echo "     ssh -i $KEY_FILE ec2-user@${WORKER_IPS[$i]} \\"
    echo "       'aws s3 sync /home/ec2-user/output/ s3://$BUCKET/$OUTPUT_PREFIX/output/'"
    echo ""
done
echo "  3. Download all results locally:"
echo "     aws s3 --profile $AWS_PROFILE sync s3://$BUCKET/$OUTPUT_PREFIX/output/ ./deidentified_output/"
echo ""
echo "=========================================="
