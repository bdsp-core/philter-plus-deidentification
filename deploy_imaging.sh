#!/bin/bash
#
# DEPLOYMENT for Imaging Reports De-identification
#
# Instances are created MANUALLY by the user. This script:
#   1. Splits 96 imaging report files evenly across N instances by file index range
#   2. Uploads project code to S3
#   3. Tags each instance with its partition number
#   4. SSHes into each instance SEQUENTIALLY to install deps, deploy code, start watchdog
#
# Key differences from deploy_aws.sh:
#   - Single flat folder (no subfolders)
#   - 40 workers (not 60)
#   - No SSO credentials injected — instances use IAM role (no 12h expiry)
#   - Watchdog on every instance (auto-restarts Python on crash)
#   - Sequential SSH deploy (no parallel background tasks — prevents duplicate launches)
#   - --note-type imagingreport passed to process_parquet_aws.py
#
# Usage:
#   ./deploy_imaging.sh <instance-id-1> <instance-id-2> ... <instance-id-N>
#

set -e

# ============================================================================
# CONFIGURATION
# ============================================================================

BUCKET="bdsp-site-mgb"
INPUT_PREFIX="I0001_ImagingReports/"
OUTPUT_PREFIX="philter-deidentify/imaging_output"
REGION="us-east-1"
AWS_PROFILE="bidmc"
KEY_FILE="/Users/anjanarayapureddy/Desktop/Philter/philter.pem"
NUM_WORKERS=60                            # 60 workers: tested stable at ~40GB RAM, fits 128GB on c6i.16xlarge
TOTAL_FILES=96                            # Known total: 96 parquet files in the folder
NOTE_TYPE="imagingreport"                 # Uses ReportTXT + OrderProcedureID columns
SSH_USER="ec2-user"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=30 -o ServerAliveInterval=60"

# ============================================================================
# VALIDATE INPUT
# ============================================================================

if [ $# -eq 0 ]; then
    echo "Usage: ./deploy_imaging.sh <instance-id-1> <instance-id-2> ... <instance-id-N>"
    echo ""
    echo "Create instances manually in AWS Console with IAM role attached, then pass their IDs here."
    echo "The script will SSH into each SEQUENTIALLY to install deps, deploy code, and start processing."
    echo ""
    echo "Example (4 instances):"
    echo "  ./deploy_imaging.sh i-0abc123 i-0def456 i-0ghi789 i-0jkl012"
    exit 1
fi

INSTANCE_IDS=("$@")
NUM_INSTANCES=${#INSTANCE_IDS[@]}

# Calculate file range per instance
FILES_PER=$(( (TOTAL_FILES + NUM_INSTANCES - 1) / NUM_INSTANCES ))

echo "=========================================="
echo "AWS Philter Imaging Reports Deployment"
echo "=========================================="
echo ""
echo "Configuration:"
echo "  Input:        s3://$BUCKET/$INPUT_PREFIX"
echo "  Output:       s3://$BUCKET/$OUTPUT_PREFIX/partition_{N}/"
echo "  Total files:  $TOTAL_FILES parquet files"
echo "  Instances:    $NUM_INSTANCES"
echo "  Files/inst:   $FILES_PER"
echo "  Workers/inst: $NUM_WORKERS"
echo "  Note type:    $NOTE_TYPE (ReportTXT + OrderProcedureID)"
echo "  Credentials:  IAM instance role (no SSO injection, no 12h expiry)"
echo "  Watchdog:     Yes (auto-restart on crash)"
echo "  Deploy mode:  Sequential (no parallel SSH)"
echo ""
echo "File range per instance:"
for i in $(seq 0 $((NUM_INSTANCES - 1))); do
    START=$(( i * FILES_PER ))
    END=$(( START + FILES_PER ))
    if [ $END -gt $TOTAL_FILES ]; then END=$TOTAL_FILES; fi
    echo "  Partition $i: files [$START:$END] (${INSTANCE_IDS[$i]})"
done
echo ""
read -p "Continue? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
fi

# ============================================================================
# STEP 1: Verify S3 Bucket Access
# ============================================================================

echo ""
echo "Step 1: Verifying S3 bucket access..."
if aws s3 --profile $AWS_PROFILE ls s3://$BUCKET --region $REGION > /dev/null 2>&1; then
    echo "  ✓ Bucket accessible: s3://$BUCKET"
else
    echo "  ✗ Cannot access s3://$BUCKET"
    exit 1
fi

# ============================================================================
# STEP 2: Upload Project Tarball to S3
# ============================================================================

echo ""
echo "Step 2: Creating and uploading project tarball to S3..."

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
    "$(basename "$SCRIPT_DIR")"

TARBALL_SIZE=$(du -h "$TARBALL" | cut -f1)
echo "  Tarball size: $TARBALL_SIZE"

aws s3 --profile $AWS_PROFILE cp "$TARBALL" \
    s3://$BUCKET/$OUTPUT_PREFIX/config/philter-project.tar.gz \
    --region $REGION

echo "  ✓ Project uploaded to s3://$BUCKET/$OUTPUT_PREFIX/config/philter-project.tar.gz"

if [ -f "$SCRIPT_DIR/configs/philter_one.json" ]; then
    aws s3 --profile $AWS_PROFILE cp "$SCRIPT_DIR/configs/philter_one.json" \
        s3://$BUCKET/$OUTPUT_PREFIX/config/philter_one.json --region $REGION
    echo "  ✓ Uploaded philter_one.json"
fi

# ============================================================================
# STEP 3: Get Instance Public IPs and Tag Them
# ============================================================================

echo ""
echo "Step 3: Getting instance public IPs and tagging..."

declare -a INSTANCE_IPS

for i in $(seq 0 $((NUM_INSTANCES - 1))); do
    INSTANCE_ID="${INSTANCE_IDS[$i]}"

    PUBLIC_IP=$(aws ec2 --profile $AWS_PROFILE describe-instances \
        --region $REGION \
        --instance-ids "$INSTANCE_ID" \
        --query 'Reservations[0].Instances[0].PublicIpAddress' \
        --output text)

    if [ "$PUBLIC_IP" = "None" ] || [ -z "$PUBLIC_IP" ]; then
        echo "  ✗ No public IP for $INSTANCE_ID (partition $i)"
        INSTANCE_IPS+=("")
        continue
    fi

    INSTANCE_IPS+=("$PUBLIC_IP")

    aws ec2 --profile $AWS_PROFILE create-tags \
        --region $REGION \
        --resources "$INSTANCE_ID" \
        --tags "Key=Name,Value=Philter-Imaging-${i}" \
               "Key=Partition,Value=${i}" \
               "Key=Project,Value=Philter-ImagingReports" || true

    echo "  Partition $i: $INSTANCE_ID → $PUBLIC_IP"
done

# ============================================================================
# STEP 4: Deploy to Each Instance via SSH (SEQUENTIAL — prevents duplicates)
# ============================================================================

echo ""
echo "Step 4: Deploying to $NUM_INSTANCES instances via SSH (sequential)..."
echo "  Each instance: install deps → download code → start watchdog"
echo ""

for i in $(seq 0 $((NUM_INSTANCES - 1))); do
    IP="${INSTANCE_IPS[$i]}"
    INSTANCE_ID="${INSTANCE_IDS[$i]}"

    if [ -z "$IP" ]; then
        echo "  ✗ Skipping partition $i (no IP)"
        continue
    fi

    FILE_START=$(( i * FILES_PER ))
    FILE_END=$(( FILE_START + FILES_PER ))
    if [ $FILE_END -gt $TOTAL_FILES ]; then FILE_END=$TOTAL_FILES; fi

    echo "----------------------------------------"
    echo "Deploying partition $i → $IP"
    echo "  Files: [$FILE_START:$FILE_END]"
    echo "  Instance: $INSTANCE_ID"
    echo "----------------------------------------"

    # Generate setup script for this instance
    cat > /tmp/imaging_setup_${i}.sh <<SETUP_SCRIPT
#!/bin/bash
set -e
exec > >(tee /home/ec2-user/philter-setup.log) 2>&1

echo "=========================================="
echo "Philter Imaging Setup - Partition $i"
echo "Started: \$(date)"
echo "=========================================="

# ---- Install Python dependencies ----
PYTHON=\$(command -v python3.9 || command -v python3)
echo "Using: \$PYTHON (\$(\$PYTHON --version))"

sudo dnf install -y -q python3-pip 2>/dev/null || \$PYTHON -m ensurepip --upgrade 2>/dev/null || true
\$PYTHON -m pip install --upgrade pip -q
\$PYTHON -m pip install -q pyarrow pandas boto3 s3fs nltk

# ---- NLTK data ----
echo "Downloading NLTK data..."
\$PYTHON -c "
import nltk
nltk.download('averaged_perceptron_tagger', quiet=True)
nltk.download('averaged_perceptron_tagger_eng', quiet=True)
nltk.download('punkt', quiet=True)
nltk.download('punkt_tab', quiet=True)
print('NLTK data downloaded')
"

echo "✓ Dependencies installed"

# NOTE: Clear any old expired SSO credentials from the previous Notes run.
# boto3 checks ~/.aws/credentials before falling back to IAM role — if expired
# credentials are present, it fails instead of falling back. Remove them so the
# IAM role (AmazonSSMRoleForInstancesQuickSetup) is used automatically.
rm -f ~/.aws/credentials
echo "✓ Old credentials cleared — using IAM role (no expiry)"

# ---- Download project from S3 ----
echo "Downloading project from S3..."
cd /home/ec2-user
rm -rf philter-plus-deidentification
aws s3 cp s3://${BUCKET}/${OUTPUT_PREFIX}/config/philter-project.tar.gz /tmp/philter-project.tar.gz
tar -xzf /tmp/philter-project.tar.gz
cd philter-plus-deidentification
\$PYTHON -m pip install -q -r requirements.txt 2>/dev/null || true
echo "✓ Project deployed"

# ---- Write watchdog script ----
# Watchdog: auto-restarts Python process on crash (exit != 0).
# Checkpoint system ensures we resume from the last completed file.
cat > /home/ec2-user/start_watchdog.sh <<'WATCHDOG'
#!/bin/bash
PYTHON=\$(command -v python3.9 || command -v python3)
cd /home/ec2-user/philter-plus-deidentification

echo "=========================================="
echo "Watchdog starting - Partition ${i}"
echo "Input:    s3://${BUCKET}/${INPUT_PREFIX}"
echo "Output:   s3://${BUCKET}/${OUTPUT_PREFIX}/partition_${i}/"
echo "Workers:  ${NUM_WORKERS}"
echo "Files:    [${FILE_START}:${FILE_END}]"
echo "NoteType: ${NOTE_TYPE}"
echo "Started:  \$(date)"
echo "=========================================="

ATTEMPT=0
while true; do
    ATTEMPT=\$(( ATTEMPT + 1 ))
    echo ""
    echo "--- Attempt \$ATTEMPT started at \$(date) ---"

    # Kill orphaned workers from previous crash before restarting
    pkill -9 -f "process_parquet_aws" 2>/dev/null || true
    sleep 2

    \$PYTHON process_parquet_aws.py \
        --input-path  "s3://${BUCKET}/${INPUT_PREFIX}" \
        --output-path "s3://${BUCKET}/${OUTPUT_PREFIX}/partition_${i}/" \
        --partition-id ${i} \
        --workers ${NUM_WORKERS} \
        --batch-size 10000 \
        --file-start ${FILE_START} \
        --file-end   ${FILE_END} \
        --note-type  ${NOTE_TYPE} \
        --philter-config configs/philter_one.json

    EXIT_CODE=\$?

    if [ \$EXIT_CODE -eq 0 ]; then
        echo ""
        echo "=========================================="
        echo "DONE - Partition ${i} complete at \$(date)"
        echo "Attempt: \$ATTEMPT"
        echo "=========================================="
        aws s3 cp /home/ec2-user/deidentify.log \
            s3://${BUCKET}/${OUTPUT_PREFIX}/logs/partition_${i}_complete.log 2>/dev/null || true
        break
    fi

    echo "Process exited with code \$EXIT_CODE at \$(date)"
    echo "Restarting in 10 seconds... (checkpoint will skip completed files)"
    sleep 10
done
WATCHDOG

chmod +x /home/ec2-user/start_watchdog.sh
echo "✓ Watchdog script written"

# ---- Start watchdog in background via nohup ----
echo ""
echo "Starting watchdog (background, persistent)..."
nohup /home/ec2-user/start_watchdog.sh > /home/ec2-user/deidentify.log 2>&1 &
WATCHDOG_PID=\$!
echo \$WATCHDOG_PID > /home/ec2-user/watchdog.pid
echo "✓ Watchdog started (PID: \$WATCHDOG_PID)"
echo "✓ Log: /home/ec2-user/deidentify.log"
echo "✓ Setup complete at \$(date)"
echo ""
echo "Monitor with:"
echo "  tail -f ~/deidentify.log"
SETUP_SCRIPT

    # Wait for SSH to be available (up to 5 minutes)
    echo "  Waiting for SSH on $IP..."
    for attempt in $(seq 1 30); do
        if ssh $SSH_OPTS -i "$KEY_FILE" $SSH_USER@${IP} "echo ssh_ok" > /dev/null 2>&1; then
            echo "  ✓ SSH ready (attempt $attempt)"
            break
        fi
        if [ $attempt -eq 30 ]; then
            echo "  ✗ SSH timeout for partition $i ($IP) after 5 min"
            exit 1
        fi
        sleep 10
    done

    # SCP setup script to instance
    echo "  Uploading setup script..."
    scp -o StrictHostKeyChecking=no -o ConnectTimeout=30 -o ServerAliveInterval=60 \
        -i "$KEY_FILE" /tmp/imaging_setup_${i}.sh $SSH_USER@${IP}:/tmp/setup.sh

    # Run setup script synchronously (wait for it to complete before moving to next instance)
    echo "  Running setup (installs deps, downloads code, starts watchdog)..."
    ssh $SSH_OPTS -i "$KEY_FILE" $SSH_USER@${IP} "bash /tmp/setup.sh"

    echo ""
    echo "  ✓ Partition $i deployed and watchdog running on $IP"
    echo ""
done

# ============================================================================
# SUMMARY
# ============================================================================

echo ""
echo "=========================================="
echo "✓ DEPLOYMENT COMPLETE"
echo "=========================================="
echo ""
echo "  Input:     s3://$BUCKET/$INPUT_PREFIX"
echo "  Output:    s3://$BUCKET/$OUTPUT_PREFIX/partition_{N}/"
echo "  Instances: $NUM_INSTANCES"
echo "  Workers:   $NUM_WORKERS per instance"
echo "  Note type: $NOTE_TYPE"
echo ""
echo "Partition assignments:"
for i in $(seq 0 $((NUM_INSTANCES - 1))); do
    IP="${INSTANCE_IPS[$i]}"
    START=$(( i * FILES_PER ))
    END=$(( START + FILES_PER ))
    if [ $END -gt $TOTAL_FILES ]; then END=$TOTAL_FILES; fi
    echo "  Partition $i: ${INSTANCE_IDS[$i]} ($IP) → files [$START:$END]"
done
echo ""
echo "Monitoring:"
echo ""
echo "  1. SSH to any instance:"
echo "     ssh -i $KEY_FILE $SSH_USER@<public-ip>"
echo "     tail -f ~/deidentify.log"
echo ""
echo "  2. Check output file counts (run every 30-60 min):"
for i in $(seq 0 $((NUM_INSTANCES - 1))); do
    echo "     aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$OUTPUT_PREFIX/partition_$i/ | wc -l"
done
echo ""
echo "  3. Check completion logs:"
echo "     aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$OUTPUT_PREFIX/logs/"
echo ""
echo "  4. After completion — generate stats:"
echo "     python3 generate_stats.py \\"
echo "         --input-path  s3://$BUCKET/$INPUT_PREFIX \\"
OUTPUT_ARGS=""
for i in $(seq 0 $((NUM_INSTANCES - 1))); do
    OUTPUT_ARGS="${OUTPUT_ARGS}s3://$BUCKET/$OUTPUT_PREFIX/partition_$i/,"
done
OUTPUT_ARGS="${OUTPUT_ARGS%,}"
echo "         --output-paths $OUTPUT_ARGS \\"
echo "         --stats-file   ./imaging_stats.csv \\"
echo "         --note-type    $NOTE_TYPE"
echo ""
echo "=========================================="
echo ${INSTANCE_IDS[@]} > /tmp/philter-imaging-instance-ids.txt
echo "Instance IDs saved to: /tmp/philter-imaging-instance-ids.txt"
