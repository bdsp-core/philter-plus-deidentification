#!/bin/bash
#
# DEPLOYMENT for AWS Parquet De-identification
#
# Instances are created MANUALLY by the user. This script:
#   1. Creates partition assignments and uploads to S3
#   2. Uploads project code to S3
#   3. Tags each instance with its partition number
#   4. SSHes into each instance to install deps, deploy code, start processing
#
# Usage:
#   ./deploy_aws.sh <instance-id-1> <instance-id-2> ... <instance-id-N>
#

set -e

# ============================================================================
# CONFIGURATION
# ============================================================================

BUCKET="bdsp-site-mgb"                    # S3 bucket (input and output)
INPUT_PREFIX="I0001_Notes/"               # Input subfolder prefix
OUTPUT_PREFIX="philter-deidentify"        # Output/logs/config prefix
REGION="us-east-1"
AWS_PROFILE="bidmc"
KEY_FILE="/Users/anjanarayapureddy/Desktop/Philter/philter.pem"
NUM_WORKERS=60                            # Workers per instance (tested OOM-safe at 32 GB)
SSH_USER="ec2-user"
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=30 -o ServerAliveInterval=60"

# Subfolder split counts — how many instances each subfolder gets
# Proportional to record count, must sum to total instance count
# 9 subfolders: 4+4+3+2+2+2+1+1+1 = 20
get_split_count() {
    case "$1" in
        "Notes_parquet_18_19/")         echo 4 ;;  # ~93M records
        "Notes_parquet_24/")            echo 4 ;;  # ~91M records
        "Notes_parquet_23/")            echo 3 ;;  # ~70M records
        "Notes_parquet_16_17/")         echo 2 ;;  # ~64M records
        "Notes_parquet_22/")            echo 2 ;;  # ~62M records
        "Notes_parquet_21/")            echo 2 ;;  # ~60M records
        "Notes_parquet_20/")            echo 1 ;;  # ~55M records
        "Notes_parquet_25_26/")         echo 1 ;;  # additional subfolder
        "Notes_parquet_15_and_before/") echo 1 ;;  # ~17M records
        *)                              echo 1 ;;  # default: no split
    esac
}

# ============================================================================
# VALIDATE INPUT
# ============================================================================

if [ $# -eq 0 ]; then
    echo "Usage: ./deploy_aws.sh <instance-id-1> <instance-id-2> ... <instance-id-N>"
    echo ""
    echo "Create instances manually in AWS Console, then pass their IDs here."
    echo "The script will SSH into each to install deps, deploy code, and start processing."
    echo ""
    echo "Example:"
    echo "  ./deploy_aws.sh i-0abc123 i-0def456 i-0ghi789"
    exit 1
fi

INSTANCE_IDS=("$@")
NUM_INSTANCES=${#INSTANCE_IDS[@]}

echo "=========================================="
echo "AWS Philter Production Deployment"
echo "=========================================="
echo ""
echo "Configuration:"
echo "  Input:     s3://$BUCKET/$INPUT_PREFIX"
echo "  Output:    s3://$BUCKET/$OUTPUT_PREFIX/output/"
echo "  Instances: $NUM_INSTANCES (user-created)"
echo "  Workers:   $NUM_WORKERS per instance ($((NUM_INSTANCES * NUM_WORKERS)) total)"
echo ""
echo "Instance IDs:"
for id in "${INSTANCE_IDS[@]}"; do
    echo "  $id"
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
# STEP 2: List Subfolders and Create Partition Assignments
# ============================================================================

echo ""
echo "Step 2: Creating partition assignments..."
echo "  Splitting subfolders proportionally across $NUM_INSTANCES instances"

SUBFOLDERS=$(aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$INPUT_PREFIX \
    --region $REGION \
    | grep "PRE " \
    | awk '{print $2}')

if [ -z "$SUBFOLDERS" ]; then
    echo "  ✗ No subfolders found under s3://$BUCKET/$INPUT_PREFIX"
    exit 1
fi

SUBFOLDER_ARRAY=($SUBFOLDERS)
echo "  Found ${#SUBFOLDER_ARRAY[@]} subfolders in S3"

# Create partition assignments
# Format per line: SUBFOLDER_S3_PATH FILE_START FILE_END
#   FILE_START: 0-based index of first file to process
#   FILE_END: exclusive end index, or -1 for "all remaining files"
PARTITION_NUM=0

for sf in "${SUBFOLDER_ARRAY[@]}"; do
    # Get split count for this subfolder (default 1 = no split)
    PARTS=$(get_split_count "$sf")

    if [ "$PARTS" -gt 1 ]; then
        # Count parquet files in this subfolder
        FILE_COUNT=$(aws s3 --profile $AWS_PROFILE ls "s3://$BUCKET/$INPUT_PREFIX$sf" \
            --region $REGION | grep -c '\.parquet' || echo 0)
        FILES_PER_PART=$(( FILE_COUNT / PARTS ))
        REMAINDER=$(( FILE_COUNT % PARTS ))

        echo "  Splitting $sf ($FILE_COUNT files) → $PARTS partitions ($FILES_PER_PART files each)"

        CURRENT_START=0
        for p in $(seq 1 $PARTS); do
            # Distribute remainder across first partitions
            CHUNK=$FILES_PER_PART
            if [ $p -le $REMAINDER ]; then
                CHUNK=$(( CHUNK + 1 ))
            fi

            CURRENT_END=$(( CURRENT_START + CHUNK ))

            # Last part gets -1 (all remaining) to avoid off-by-one
            if [ $p -eq $PARTS ]; then
                echo "s3://$BUCKET/$INPUT_PREFIX$sf $CURRENT_START -1" > /tmp/partition_${PARTITION_NUM}.txt
                echo "    Partition $PARTITION_NUM: files [$CURRENT_START:end]"
            else
                echo "s3://$BUCKET/$INPUT_PREFIX$sf $CURRENT_START $CURRENT_END" > /tmp/partition_${PARTITION_NUM}.txt
                echo "    Partition $PARTITION_NUM: files [$CURRENT_START:$CURRENT_END]"
            fi

            aws s3 --profile $AWS_PROFILE cp /tmp/partition_${PARTITION_NUM}.txt \
                s3://$BUCKET/$OUTPUT_PREFIX/assignments/partition_${PARTITION_NUM}.txt \
                --region $REGION --quiet

            CURRENT_START=$CURRENT_END
            PARTITION_NUM=$((PARTITION_NUM + 1))
        done
    else
        echo "  $sf → partition $PARTITION_NUM (all files)"
        echo "s3://$BUCKET/$INPUT_PREFIX$sf 0 -1" > /tmp/partition_${PARTITION_NUM}.txt
        aws s3 --profile $AWS_PROFILE cp /tmp/partition_${PARTITION_NUM}.txt \
            s3://$BUCKET/$OUTPUT_PREFIX/assignments/partition_${PARTITION_NUM}.txt \
            --region $REGION --quiet
        PARTITION_NUM=$((PARTITION_NUM + 1))
    fi
done

echo "  ✓ Created $PARTITION_NUM partition assignments"

if [ $PARTITION_NUM -ne $NUM_INSTANCES ]; then
    echo ""
    echo "  ⚠ WARNING: $PARTITION_NUM partitions but $NUM_INSTANCES instances."
    read -p "  Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 1
    fi
fi

# ============================================================================
# STEP 3: Upload Project Tarball to S3
# ============================================================================

echo ""
echo "Step 3: Creating and uploading project tarball to S3..."

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
# STEP 4: Get AWS Credentials (to inject into instances)
# ============================================================================

echo ""
echo "Step 4: Retrieving AWS credentials from $AWS_PROFILE profile..."

AWS_ACCESS_KEY=$(aws configure get aws_access_key_id --profile $AWS_PROFILE 2>/dev/null) || true
AWS_SECRET_KEY=$(aws configure get aws_secret_access_key --profile $AWS_PROFILE 2>/dev/null) || true
AWS_SESSION_TOKEN=$(aws configure get aws_session_token --profile $AWS_PROFILE 2>/dev/null) || true

if [ -z "$AWS_ACCESS_KEY" ] || [ -z "$AWS_SECRET_KEY" ]; then
    echo "  ✗ Could not retrieve credentials from $AWS_PROFILE profile"
    exit 1
fi

echo "  ✓ Credentials retrieved (Access Key: ${AWS_ACCESS_KEY:0:10}...)"

# ============================================================================
# STEP 5: Get Instance Public IPs and Tag Them
# ============================================================================

echo ""
echo "Step 5: Getting instance public IPs and tagging..."

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
        --tags "Key=Name,Value=Philter-Worker-${i}" \
               "Key=Partition,Value=${i}" \
               "Key=Project,Value=Philter-Deidentify"

    echo "  Partition $i: $INSTANCE_ID → $PUBLIC_IP"
done

# ============================================================================
# STEP 6: Deploy to Each Instance via SSH (in parallel)
# ============================================================================

echo ""
echo "Step 6: Deploying to all $NUM_INSTANCES instances via SSH (parallel)..."
echo "  Each instance: install deps → configure creds → download code → start processing"
echo ""

DEPLOY_PIDS=()

for i in $(seq 0 $((NUM_INSTANCES - 1))); do
    IP="${INSTANCE_IPS[$i]}"
    if [ -z "$IP" ]; then
        echo "  Skipping partition $i (no IP)"
        continue
    fi

    if [ $i -ge $PARTITION_NUM ]; then
        echo "  Skipping partition $i (no assignment)"
        continue
    fi

    (
        LOGFILE="/tmp/deploy_partition_${i}.log"
        echo "--- Deploying partition $i to $IP ---" > "$LOGFILE"

        # Generate the setup script for this instance
        cat > /tmp/instance_setup_${i}.sh <<SETUP_SCRIPT
#!/bin/bash
set -e
exec > >(tee /home/ec2-user/philter-setup.log) 2>&1

echo "=========================================="
echo "Philter Setup - Partition $i"
echo "Started: \$(date)"
echo "=========================================="

# ---- Install Python dependencies ----
PYTHON=\$(command -v python3.9 || command -v python3)
echo "Using: \$PYTHON (\$(\$PYTHON --version))"

# AL2023 doesn't have pip pre-installed — use ensurepip first, then dnf for pip package
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

# ---- AWS credentials ----
echo "Configuring AWS credentials..."
mkdir -p ~/.aws
cat > ~/.aws/credentials <<CREDS
[default]
aws_access_key_id = ${AWS_ACCESS_KEY}
aws_secret_access_key = ${AWS_SECRET_KEY}
aws_session_token = ${AWS_SESSION_TOKEN}
CREDS

cat > ~/.aws/config <<CFG
[default]
region = ${REGION}
output = json
CFG
chmod 600 ~/.aws/credentials
echo "✓ AWS credentials configured"

# ---- Download project from S3 ----
echo "Downloading project from S3..."
cd /home/ec2-user
rm -rf philter-plus-deidentification
aws s3 cp s3://${BUCKET}/${OUTPUT_PREFIX}/config/philter-project.tar.gz /tmp/philter-project.tar.gz
tar -xzf /tmp/philter-project.tar.gz
cd philter-plus-deidentification
\$PYTHON -m pip install -q -r requirements.txt 2>/dev/null || true
echo "✓ Project deployed"

# ---- Download assignment ----
aws s3 cp s3://${BUCKET}/${OUTPUT_PREFIX}/assignments/partition_${i}.txt /tmp/my_assignment.txt
echo "Assignment:"
cat /tmp/my_assignment.txt

# ---- Start processing in background ----
echo ""
echo "Starting de-identification processing..."

nohup bash -c '
PYTHON=\$(command -v python3.9 || command -v python3)
cd /home/ec2-user/philter-plus-deidentification

TOTAL_START=\$(date +%s)
JOB_NUM=0

while IFS=" " read -r SUBFOLDER_PATH FILE_START FILE_END; do
    [ -z "\$SUBFOLDER_PATH" ] && continue
    JOB_NUM=\$(( JOB_NUM + 1 ))

    echo "=============================="
    echo "Job \$JOB_NUM: \$SUBFOLDER_PATH"
    echo "File range: \$FILE_START to \$FILE_END"
    echo "Started at: \$(date)"
    echo "=============================="

    RANGE_ARGS=""
    if [ "\$FILE_START" != "0" ]; then
        RANGE_ARGS="\$RANGE_ARGS --file-start \$FILE_START"
    fi
    if [ "\$FILE_END" != "-1" ]; then
        RANGE_ARGS="\$RANGE_ARGS --file-end \$FILE_END"
    fi

    \$PYTHON process_parquet_aws.py \
        --input-path "\$SUBFOLDER_PATH" \
        --output-path "s3://${BUCKET}/${OUTPUT_PREFIX}/output/partition_${i}/" \
        --partition-id ${i} \
        --workers ${NUM_WORKERS} \
        --batch-size 10000 \
        --philter-config configs/philter_one.json \
        \$RANGE_ARGS

    echo "Completed job \$JOB_NUM at \$(date)"
done < /tmp/my_assignment.txt

TOTAL_END=\$(date +%s)
TOTAL_ELAPSED=\$(( TOTAL_END - TOTAL_START ))
TOTAL_HOURS=\$(echo "scale=1; \$TOTAL_ELAPSED / 3600" | bc)

echo "=========================================="
echo "ALL JOBS COMPLETE at \$(date)"
echo "Partition ${i} finished"
echo "Total jobs: \$JOB_NUM"
echo "Total time: \${TOTAL_HOURS} hours"
echo "=========================================="

echo "Partition ${i} completed at \$(date). Jobs: \$JOB_NUM. Time: \${TOTAL_HOURS}h" > /tmp/complete.log
aws s3 cp /tmp/complete.log s3://${BUCKET}/${OUTPUT_PREFIX}/logs/partition_${i}_complete.log
aws s3 cp /home/ec2-user/deidentify.log s3://${BUCKET}/${OUTPUT_PREFIX}/logs/partition_${i}_deidentify.log

' > /home/ec2-user/deidentify.log 2>&1 &

PROC_PID=\$!
echo \$PROC_PID > /home/ec2-user/deidentify.pid
echo ""
echo "✓ Processing started in background (PID: \$PROC_PID)"
echo "✓ Log: /home/ec2-user/deidentify.log"
echo "✓ Setup complete at \$(date)"
SETUP_SCRIPT

        # Wait for SSH to be available
        echo "  Waiting for SSH on $IP..." >> "$LOGFILE"
        for attempt in $(seq 1 30); do
            if ssh $SSH_OPTS -i "$KEY_FILE" $SSH_USER@${IP} "echo ssh_ok" >> "$LOGFILE" 2>&1; then
                break
            fi
            if [ $attempt -eq 30 ]; then
                echo "  ✗ SSH timeout for partition $i ($IP)" | tee -a "$LOGFILE"
                exit 1
            fi
            sleep 10
        done

        # SCP setup script to instance
        scp $SSH_OPTS -i "$KEY_FILE" /tmp/instance_setup_${i}.sh $SSH_USER@${IP}:/tmp/setup.sh >> "$LOGFILE" 2>&1

        # Run setup script (installs deps, starts processing in background)
        ssh $SSH_OPTS -i "$KEY_FILE" $SSH_USER@${IP} "bash /tmp/setup.sh" >> "$LOGFILE" 2>&1

        echo "  ✓ Partition $i deployed and processing on $IP"

    ) &

    DEPLOY_PIDS+=($!)
    echo "  Started deploy for partition $i ($IP) [PID: $!]"
done

# Wait for all parallel deployments to finish
echo ""
echo "Waiting for all deployments to complete..."
echo "  (Each takes ~5-10 min for yum + pip install)"
FAILED=0
for pid in "${DEPLOY_PIDS[@]}"; do
    if ! wait $pid; then
        FAILED=$((FAILED + 1))
    fi
done

if [ $FAILED -gt 0 ]; then
    echo ""
    echo "⚠ $FAILED deployment(s) failed. Check /tmp/deploy_partition_*.log for details."
fi

# ============================================================================
# SUMMARY
# ============================================================================

echo ""
echo "=========================================="
echo "✓ DEPLOYMENT COMPLETE"
echo "=========================================="
echo ""
echo "  Input:     s3://$BUCKET/$INPUT_PREFIX"
echo "  Output:    s3://$BUCKET/$OUTPUT_PREFIX/output/"
echo "  Instances: $NUM_INSTANCES"
echo "  Workers:   $NUM_WORKERS per instance"
echo ""
echo "Instance assignments:"
for i in $(seq 0 $((NUM_INSTANCES - 1))); do
    IP="${INSTANCE_IPS[$i]}"
    if [ -n "$IP" ] && [ $i -lt $PARTITION_NUM ]; then
        ASSIGNMENT=$(cat /tmp/partition_${i}.txt 2>/dev/null || echo "unknown")
        echo "  Partition $i: ${INSTANCE_IDS[$i]} ($IP) → $ASSIGNMENT"
    fi
done
echo ""
echo "Monitoring:"
echo ""
echo "  1. SSH to any instance:"
echo "     ssh -i $KEY_FILE $SSH_USER@<public-ip>"
echo "     tail -f ~/deidentify.log"
echo ""
echo "  2. Check if processing is running:"
echo "     ssh -i $KEY_FILE $SSH_USER@<public-ip> 'ps aux | grep process_parquet'"
echo ""
echo "  3. Check completion logs in S3:"
echo "     aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$OUTPUT_PREFIX/logs/"
echo ""
echo "  4. Check output files:"
echo "     aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$OUTPUT_PREFIX/output/ --recursive --summarize"
echo ""
echo "  5. Download results when done:"
echo "     aws s3 --profile $AWS_PROFILE sync s3://$BUCKET/$OUTPUT_PREFIX/output/ ./deidentified_output/"
echo ""
echo "  6. Deploy logs (local):"
echo "     cat /tmp/deploy_partition_*.log"
echo ""
echo "=========================================="
echo "Instance IDs saved to: /tmp/philter-instance-ids.txt"
echo ${INSTANCE_IDS[@]} > /tmp/philter-instance-ids.txt
