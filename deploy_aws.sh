#!/bin/bash
#
# ONE-COMMAND DEPLOYMENT for AWS Parquet De-identification
#
# Usage:
#   ./deploy_aws.sh
#
# This script will:
#   1. Verify S3 bucket access and partition input subfolders
#   2. Upload project tarball + config to S3 (private repo — can't git clone)
#   3. Create/use SSH key pair
#   4. Launch 4 × c6i.32xlarge Spot instances
#   5. Each instance auto-downloads project from S3 and starts processing
#
# Monitoring:
#   ./check_status.sh
#

set -e

# ============================================================================
# CONFIGURATION
# ============================================================================

BUCKET="bdsp-site-mgb"                    # S3 bucket (input and output)
INPUT_PREFIX="I0001_Notes/"               # Input subfolder prefix
OUTPUT_PREFIX="philter-deidentify"        # Output/logs/config prefix
NUM_INSTANCES=4                           # 4 × c6i.32xlarge
INSTANCE_TYPE="c6i.32xlarge"
REGION="us-east-1"
MAX_SPOT_PRICE="5.50"                     # Max spot price (on-demand = $5.44)
AWS_PROFILE="bidmc"
KEY_NAME="bdsp-ec2"
KEY_FILE="/Users/anjanarayapureddy/Desktop/Philter/bdsp-ec2.pem"
VPC_ID="vpc-0b19ba4d16f0f4695"            # bdsp-webapp-vpc
SUBNET_ID="subnet-032f4ed8e15acf550"      # bdsp-webapp-subnet-public1-us-east-1a
SG_IDS_COMMA="sg-0350d41bfbbc1f0b6"      # launch-wizard-20
EBS_SIZE=100                              # GB per instance

# ============================================================================
# SUMMARY
# ============================================================================

echo "=========================================="
echo "AWS Philter Production Deployment"
echo "=========================================="
echo ""
echo "Configuration:"
echo "  Input:     s3://$BUCKET/$INPUT_PREFIX"
echo "  Output:    s3://$BUCKET/$OUTPUT_PREFIX/output/"
echo "  Instances: $NUM_INSTANCES × $INSTANCE_TYPE (Spot)"
echo "  Region:    $REGION"
echo "  Workers:   120 per instance (480 total)"
echo "  Spot Max:  \$$MAX_SPOT_PRICE/hour"
echo "  EBS:       ${EBS_SIZE}GB gp3 per instance"
echo ""
echo "Estimated:"
echo "  Time: ~2.5-3 days"
echo "  Cost: ~\$300-350 (Spot)"
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
    echo "✓ Bucket accessible: s3://$BUCKET"
else
    echo "✗ Cannot access s3://$BUCKET"
    exit 1
fi

# ============================================================================
# STEP 1b: List and Partition Input Subfolders
# ============================================================================

echo ""
echo "Step 1b: Listing subfolders in s3://$BUCKET/$INPUT_PREFIX ..."

SUBFOLDERS=$(aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$INPUT_PREFIX \
    --region $REGION \
    | grep "PRE " \
    | awk '{print $2}')

if [ -z "$SUBFOLDERS" ]; then
    echo "No subfolders found under s3://$BUCKET/$INPUT_PREFIX"
    echo "Treating as a flat folder."
    for i in $(seq 0 $(($NUM_INSTANCES - 1))); do
        echo "s3://$BUCKET/$INPUT_PREFIX" > /tmp/partition_${i}.txt
        aws s3 --profile $AWS_PROFILE cp /tmp/partition_${i}.txt \
            s3://$BUCKET/$OUTPUT_PREFIX/assignments/partition_${i}.txt
    done
else
    SUBFOLDER_ARRAY=($SUBFOLDERS)
    TOTAL=${#SUBFOLDER_ARRAY[@]}
    echo "Found $TOTAL subfolders:"
    for sf in "${SUBFOLDER_ARRAY[@]}"; do echo "  s3://$BUCKET/$INPUT_PREFIX$sf"; done

    # Split evenly across instances
    PER_INSTANCE=$(( ($TOTAL + $NUM_INSTANCES - 1) / $NUM_INSTANCES ))

    for i in $(seq 0 $(($NUM_INSTANCES - 1))); do
        START=$(( i * PER_INSTANCE ))
        > /tmp/partition_${i}.txt
        for j in $(seq $START $(( START + PER_INSTANCE - 1 ))); do
            if [ $j -lt $TOTAL ]; then
                echo "s3://$BUCKET/$INPUT_PREFIX${SUBFOLDER_ARRAY[$j]}" >> /tmp/partition_${i}.txt
            fi
        done
        COUNT=$(grep -c . /tmp/partition_${i}.txt 2>/dev/null || echo 0)
        echo "  Partition $i: $COUNT subfolders"
        aws s3 --profile $AWS_PROFILE cp /tmp/partition_${i}.txt \
            s3://$BUCKET/$OUTPUT_PREFIX/assignments/partition_${i}.txt \
            --region $REGION
    done

    echo "✓ Assignments saved to s3://$BUCKET/$OUTPUT_PREFIX/assignments/"
fi

# ============================================================================
# STEP 2: Create Project Tarball and Upload to S3
# ============================================================================

echo ""
echo "Step 2: Creating and uploading project tarball to S3..."
echo "  (Repo is private — instances will download from S3 instead of git clone)"

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

echo "✓ Project uploaded to s3://$BUCKET/$OUTPUT_PREFIX/config/philter-project.tar.gz"

# Upload Philter config separately
if [ -f "configs/philter_one.json" ]; then
    aws s3 --profile $AWS_PROFILE cp configs/philter_one.json \
        s3://$BUCKET/$OUTPUT_PREFIX/config/philter_one.json --region $REGION
    echo "✓ Uploaded philter_one.json"
fi

# ============================================================================
# STEP 3: Setup SSH Key Pair
# ============================================================================

echo ""
echo "Step 3: Setting up SSH key pair..."

KEY_CHECK=$(aws ec2 --profile $AWS_PROFILE describe-key-pairs \
    --region $REGION \
    --key-names $KEY_NAME 2>&1) || true

if echo "$KEY_CHECK" | grep -q "InvalidKeyPair.NotFound"; then
    echo "Creating new key pair: $KEY_NAME"

    TEMP_KEY=$(mktemp /tmp/bdsp_key_XXXX.pem)
    aws ec2 --profile $AWS_PROFILE create-key-pair \
        --region $REGION \
        --key-name $KEY_NAME \
        --query 'KeyMaterial' \
        --output text > "$TEMP_KEY"

    mv -f "$TEMP_KEY" "$KEY_FILE"
    chmod 400 "$KEY_FILE"

    echo "✓ Key pair created: $KEY_NAME"
    echo "✓ PEM file saved: $KEY_FILE"
else
    echo "✓ Key pair already exists: $KEY_NAME"
    if [ -f "$KEY_FILE" ]; then
        echo "✓ PEM file found: $KEY_FILE"
    else
        echo "⚠ PEM file not found locally: $KEY_FILE"
        echo "  Re-create: aws ec2 delete-key-pair --key-name $KEY_NAME --profile $AWS_PROFILE --region $REGION"
        exit 1
    fi
fi

# ============================================================================
# STEP 4: Get AWS Credentials
# ============================================================================

echo ""
echo "Step 4: Retrieving AWS credentials from $AWS_PROFILE profile..."

AWS_ACCESS_KEY=$(aws configure get aws_access_key_id --profile $AWS_PROFILE 2>/dev/null) || true
AWS_SECRET_KEY=$(aws configure get aws_secret_access_key --profile $AWS_PROFILE 2>/dev/null) || true
AWS_SESSION_TOKEN=$(aws configure get aws_session_token --profile $AWS_PROFILE 2>/dev/null) || true

if [ -z "$AWS_ACCESS_KEY" ] || [ -z "$AWS_SECRET_KEY" ]; then
    echo "✗ Could not retrieve credentials from $AWS_PROFILE profile"
    echo "  Make sure ~/.aws/credentials has a [$AWS_PROFILE] section"
    exit 1
fi

echo "✓ Credentials retrieved (Access Key: ${AWS_ACCESS_KEY:0:10}...)"

# ============================================================================
# STEP 4b: Get Latest Amazon Linux 2 AMI
# ============================================================================

echo ""
echo "Step 4b: Looking up latest Amazon Linux 2 AMI..."
AMI_ID=$(aws ec2 --profile $AWS_PROFILE describe-images \
    --region $REGION \
    --owners amazon \
    --filters \
        'Name=name,Values=amzn2-ami-hvm-*-x86_64-gp2' \
        'Name=state,Values=available' \
    --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
    --output text)
echo "✓ AMI: $AMI_ID"

# ============================================================================
# STEP 4c: Network Configuration
# ============================================================================

echo ""
echo "Step 4c: Network configuration..."
echo "  VPC:             $VPC_ID"
echo "  Subnet:          $SUBNET_ID"
echo "  Security Groups: $SG_IDS_COMMA"

# ============================================================================
# STEP 5: Create User Data Script (runs on each EC2 instance at boot)
# ============================================================================

echo ""
echo "Step 5: Generating startup script..."

cat > ./user-data.sh <<'USERDATA_START'
#!/bin/bash
set -e

# Redirect all output to log
exec > >(tee /var/log/user-data.log) 2>&1

echo "=========================================="
echo "Instance Startup: $(date)"
echo "=========================================="

# -----------------------------------------------
# Install Python 3.9 and dependencies
# -----------------------------------------------
echo "Installing dependencies..."
yum update -y -q
amazon-linux-extras install python3.9 -y
yum install -y -q python39-pip

PYTHON=$(command -v python3.9 || command -v python3)
echo "Using Python: $PYTHON ($($PYTHON --version))"

$PYTHON -m pip install --upgrade pip -q
$PYTHON -m pip install -q pyarrow pandas boto3 s3fs nltk

# Download NLTK data (required by Philter POS tagging)
echo "Downloading NLTK data..."
$PYTHON -c "
import nltk
nltk.download('averaged_perceptron_tagger', quiet=True)
nltk.download('averaged_perceptron_tagger_eng', quiet=True)
nltk.download('punkt', quiet=True)
nltk.download('punkt_tab', quiet=True)
print('NLTK data downloaded')
"

echo "✓ Dependencies installed"

# -----------------------------------------------
# Configure AWS credentials
# -----------------------------------------------
echo "Configuring AWS credentials..."
mkdir -p /home/ec2-user/.aws /root/.aws
USERDATA_START

# Inject credentials (these variables are expanded at script generation time)
cat >> ./user-data.sh <<USERDATA_CREDS
cat > /home/ec2-user/.aws/credentials <<AWSCREDS
[default]
aws_access_key_id = ${AWS_ACCESS_KEY}
aws_secret_access_key = ${AWS_SECRET_KEY}
aws_session_token = ${AWS_SESSION_TOKEN}
AWSCREDS

cat > /home/ec2-user/.aws/config <<AWSCONFIG
[default]
region = ${REGION}
output = json
AWSCONFIG

# Root also needs credentials for user-data script phase
cp /home/ec2-user/.aws/credentials /root/.aws/credentials
cp /home/ec2-user/.aws/config /root/.aws/config
chown -R ec2-user:ec2-user /home/ec2-user/.aws
chmod 600 /home/ec2-user/.aws/credentials /root/.aws/credentials
echo "✓ AWS credentials configured"
USERDATA_CREDS

# Rest of user-data (no variable expansion needed)
cat >> ./user-data.sh <<USERDATA_REST
# -----------------------------------------------
# Download project from S3 (private repo)
# -----------------------------------------------
echo "Downloading project from S3..."
cd /home/ec2-user
aws s3 cp s3://${BUCKET}/${OUTPUT_PREFIX}/config/philter-project.tar.gz /tmp/philter-project.tar.gz
tar -xzf /tmp/philter-project.tar.gz
cd philter-plus-deidentification

# Install project requirements
PYTHON=\$(command -v python3.9 || command -v python3)
\$PYTHON -m pip install -q -r requirements.txt 2>/dev/null || true

echo "✓ Project extracted and ready"

# -----------------------------------------------
# Get partition assignment from instance tag
# -----------------------------------------------
echo "Getting partition assignment..."
TOKEN=\$(curl -s -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
INSTANCE_ID=\$(curl -s -H "X-aws-ec2-metadata-token: \$TOKEN" \
    http://169.254.169.254/latest/meta-data/instance-id)
PARTITION=\$(aws ec2 describe-tags \
    --region ${REGION} \
    --filters "Name=resource-id,Values=\$INSTANCE_ID" "Name=key,Values=Partition" \
    --query 'Tags[0].Value' --output text)

echo "Instance: \$INSTANCE_ID, Partition: \$PARTITION"

# Download subfolder assignment list
aws s3 cp s3://${BUCKET}/${OUTPUT_PREFIX}/assignments/partition_\${PARTITION}.txt /tmp/my_subfolders.txt

echo "Subfolders to process:"
cat /tmp/my_subfolders.txt

# -----------------------------------------------
# Process each assigned subfolder
# -----------------------------------------------
cd /home/ec2-user/philter-plus-deidentification
> /var/log/deidentify.log

(
    SUBFOLDER_NUM=0
    TOTAL_START=\$(date +%s)

    while IFS= read -r SUBFOLDER_PATH; do
        [ -z "\$SUBFOLDER_PATH" ] && continue

        FOLDER_NAME=\$(echo "\$SUBFOLDER_PATH" | sed 's|s3://||' | tr '/' '_' | sed 's/_\$//')

        echo "==============================" | tee -a /var/log/deidentify.log
        echo "Processing: \$SUBFOLDER_PATH"  | tee -a /var/log/deidentify.log
        echo "Output to:  s3://${BUCKET}/${OUTPUT_PREFIX}/output/\${FOLDER_NAME}/" | tee -a /var/log/deidentify.log
        echo "Started at: \$(date)"          | tee -a /var/log/deidentify.log
        echo "==============================" | tee -a /var/log/deidentify.log

        \$PYTHON process_parquet_aws.py \\
            --input-path "\$SUBFOLDER_PATH" \\
            --output-path "s3://${BUCKET}/${OUTPUT_PREFIX}/output/\${FOLDER_NAME}/" \\
            --workers 120 \\
            --batch-size 10000 \\
            --philter-config configs/philter_one.json \\
            >> /var/log/deidentify.log 2>&1

        SUBFOLDER_NUM=\$(( SUBFOLDER_NUM + 1 ))
        echo "Completed subfolder \$SUBFOLDER_NUM at \$(date)" | tee -a /var/log/deidentify.log

    done < /tmp/my_subfolders.txt

    TOTAL_END=\$(date +%s)
    TOTAL_ELAPSED=\$(( TOTAL_END - TOTAL_START ))
    TOTAL_HOURS=\$(echo "scale=1; \$TOTAL_ELAPSED / 3600" | bc)

    echo "==========================================" | tee -a /var/log/deidentify.log
    echo "ALL SUBFOLDERS COMPLETE at \$(date)"        | tee -a /var/log/deidentify.log
    echo "Partition \$PARTITION finished"              | tee -a /var/log/deidentify.log
    echo "Total subfolders: \$SUBFOLDER_NUM"          | tee -a /var/log/deidentify.log
    echo "Total time: \${TOTAL_HOURS} hours"          | tee -a /var/log/deidentify.log
    echo "==========================================" | tee -a /var/log/deidentify.log

    # Upload completion marker
    echo "Partition \$PARTITION completed at \$(date). Subfolders: \$SUBFOLDER_NUM. Time: \${TOTAL_HOURS}h" \
        | tee /var/log/processing-complete.log
    aws s3 cp /var/log/processing-complete.log \
        s3://${BUCKET}/${OUTPUT_PREFIX}/logs/partition_\${PARTITION}_complete.log

    # Upload final log
    aws s3 cp /var/log/deidentify.log \
        s3://${BUCKET}/${OUTPUT_PREFIX}/logs/partition_\${PARTITION}_deidentify.log

) &

PID=\$!
echo "Processing started with PID: \$PID"
echo \$PID > /var/run/deidentify.pid

echo "=========================================="
echo "Startup Complete: \$(date)"
echo "=========================================="
USERDATA_REST

echo "✓ User data script created"

# Base64 encode for AWS CLI
USER_DATA_B64=$(base64 < ./user-data.sh)

# ============================================================================
# STEP 6: Launch EC2 Instances
# ============================================================================

echo ""
echo "Step 6: Launching $NUM_INSTANCES × $INSTANCE_TYPE Spot instances..."

INSTANCE_IDS=()

for i in $(seq 0 $(($NUM_INSTANCES - 1))); do
    echo ""
    echo "Launching instance $i (partition $i)..."

    INSTANCE_ID=$(aws ec2 --profile $AWS_PROFILE run-instances \
        --region $REGION \
        --image-id $AMI_ID \
        --instance-type $INSTANCE_TYPE \
        --key-name $KEY_NAME \
        --metadata-options "HttpTokens=required,HttpPutResponseHopLimit=2,HttpEndpoint=enabled" \
        --network-interfaces "DeviceIndex=0,SubnetId=${SUBNET_ID},Groups=${SG_IDS_COMMA},AssociatePublicIpAddress=true" \
        --block-device-mappings "[{\"DeviceName\":\"/dev/xvda\",\"Ebs\":{\"VolumeSize\":${EBS_SIZE},\"VolumeType\":\"gp3\",\"Iops\":3000,\"Throughput\":125}}]" \
        --user-data "$USER_DATA_B64" \
        --tag-specifications \
            "ResourceType=instance,Tags=[
                {Key=Name,Value=Philter-Worker-${i}},
                {Key=Partition,Value=${i}},
                {Key=Project,Value=Philter-Deidentify}
            ]" \
        --instance-market-options \
            "MarketType=spot,SpotOptions={MaxPrice=${MAX_SPOT_PRICE},SpotInstanceType=one-time}" \
        --query 'Instances[0].InstanceId' \
        --output text)

    INSTANCE_IDS+=($INSTANCE_ID)
    echo "✓ Launched: $INSTANCE_ID (Partition $i)"
done

# ============================================================================
# STEP 7: Wait for Instances to Start
# ============================================================================

echo ""
echo "Step 7: Waiting for instances to start..."

aws ec2 --profile $AWS_PROFILE wait instance-running \
    --region $REGION \
    --instance-ids ${INSTANCE_IDS[@]}

echo "✓ All instances running"

# Get instance details
echo ""
echo "Instance Details:"
echo "=========================================="
aws ec2 --profile $AWS_PROFILE describe-instances \
    --region $REGION \
    --instance-ids ${INSTANCE_IDS[@]} \
    --query 'Reservations[].Instances[].[InstanceId,State.Name,PublicIpAddress,PrivateIpAddress,Tags[?Key==`Partition`].Value|[0]]' \
    --output table

# Save instance IDs
echo ${INSTANCE_IDS[@]} > /tmp/philter-instance-ids.txt

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
echo "  Instances: $NUM_INSTANCES × $INSTANCE_TYPE"
echo "  IDs:       ${INSTANCE_IDS[@]}"
echo ""
echo "Monitoring:"
echo ""
echo "  1. Quick status check:"
echo "     ./check_status.sh"
echo ""
echo "  2. Check completion logs:"
echo "     aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$OUTPUT_PREFIX/logs/"
echo ""
echo "  3. Check output files:"
echo "     aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$OUTPUT_PREFIX/output/ --recursive --summarize"
echo ""
echo "  4. SSH to instance:"
echo "     ssh -i $KEY_FILE ec2-user@<public-ip>"
echo "     tail -f /var/log/deidentify.log"
echo ""
echo "  5. Download results when done:"
echo "     aws s3 --profile $AWS_PROFILE sync s3://$BUCKET/$OUTPUT_PREFIX/output/ ./deidentified_output/"
echo ""
echo "=========================================="
echo "Instance IDs saved to: /tmp/philter-instance-ids.txt"
