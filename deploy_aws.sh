#!/bin/bash
#
# ONE-COMMAND DEPLOYMENT for AWS Parquet Processing
#
# Usage:
#   ./deploy_aws.sh
#
# This script will:
# 1. Create S3 bucket
# 2. Upload config files
# 3. Create/use SSH key pair (saved as philter-key.pem)
# 4. Launch EC2 instances with auto-configuration
# 5. Start processing automatically
#

set -e

# ============================================================================
# CONFIGURATION - EDIT THESE
# ============================================================================

BUCKET="bdsp-site-mgb"                    # S3 bucket (input and output in same bucket)
INPUT_PREFIX="I0001_Notes/"               # Input subfolder prefix
OUTPUT_PREFIX="philter-deidentify"        # Output/logs/config prefix inside same bucket
NUM_INSTANCES=8  # 8 instances = 1 per subfolder (optimal for this dataset)
INSTANCE_TYPE="c6i.32xlarge"
REGION="us-east-1"
MAX_SPOT_PRICE="5.50"  # On-demand price for c6i.32xlarge
AWS_PROFILE="bidmc"  # AWS profile to use
KEY_NAME="bdsp-ec2"               # EC2 key pair name
KEY_FILE="./bdsp-ec2.pem"         # PEM file saved locally (same folder as this script)
VPC_ID="vpc-0b19ba4d16f0f4695"          # bdsp-webapp-vpc
SUBNET_ID="subnet-032f4ed8e15acf550"    # bdsp-webapp-subnet-public1-us-east-1a
SG_IDS_COMMA="sg-0f0200d3a98d50585"    # launch-wizard-17

# ============================================================================
# SETUP
# ============================================================================

echo "=========================================="
echo "AWS Philter Deployment Script"
echo "=========================================="
echo ""
echo "Configuration:"
echo "  Input:  s3://$BUCKET/$INPUT_PREFIX"
echo "  Output: s3://$BUCKET/$OUTPUT_PREFIX/output/"
echo "  Instances: $NUM_INSTANCES × $INSTANCE_TYPE"
echo "  Region: $REGION"
echo "  Spot Max Price: \$$MAX_SPOT_PRICE/hour"
echo ""
read -p "Continue? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 1
fi

# ============================================================================
# STEP 1: Verify S3 Bucket Access (no bucket created)
# ============================================================================

echo ""
echo "Step 1: Verifying S3 bucket access..."
if aws s3 --profile $AWS_PROFILE ls s3://$BUCKET --region $REGION > /dev/null 2>&1; then
    echo "✓ Bucket accessible: s3://$BUCKET"
    echo "  Output will go to: s3://$BUCKET/$OUTPUT_PREFIX/"
else
    echo "✗ Cannot access s3://$BUCKET"
    exit 1
fi

# ============================================================================
# STEP 1b: List and Partition Input Subfolders
# ============================================================================

echo ""
echo "Step 1b: Listing subfolders in s3://$BUCKET/$INPUT_PREFIX ..."

# List all "subdirectories" (common prefixes) under the input prefix
SUBFOLDERS=$(aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$INPUT_PREFIX \
    --region $REGION \
    | grep "PRE " \
    | awk '{print $2}')

if [ -z "$SUBFOLDERS" ]; then
    echo "No subfolders found under s3://$BUCKET/$INPUT_PREFIX"
    echo "Treating as a flat folder. Assigning entire path to all instances."
    for i in $(seq 0 $(($NUM_INSTANCES - 1))); do
        echo "s3://$BUCKET/$INPUT_PREFIX" > /tmp/partition_${i}.txt
        aws s3 --profile $AWS_PROFILE cp /tmp/partition_${i}.txt \
            s3://$BUCKET/$OUTPUT_PREFIX/assignments/partition_${i}.txt
    done
else
    # Convert to array - each entry is just "Notes_parquet_XX/", prepend full S3 path
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
# STEP 2: Upload Config Files
# ============================================================================

echo ""
echo "Step 2: Uploading Philter config..."
if [ -f "configs/philter_one.json" ]; then
    aws s3 --profile $AWS_PROFILE cp configs/philter_one.json \
        s3://$BUCKET/$OUTPUT_PREFIX/config/philter_one.json --region $REGION
    echo "✓ Uploaded philter_one.json"
else
    echo "⚠ Warning: configs/philter_one.json not found!"
    echo "  Please upload manually: aws s3 --profile $AWS_PROFILE cp configs/philter_one.json s3://$BUCKET/$OUTPUT_PREFIX/config/"
fi

# ============================================================================
# STEP 3: Create SSH Key Pair
# ============================================================================

echo ""
echo "Step 3: Setting up SSH key pair..."

# Check if key pair already exists in AWS
KEY_CHECK=$(aws ec2 --profile $AWS_PROFILE describe-key-pairs \
    --region $REGION \
    --key-names $KEY_NAME 2>&1) || true

if echo "$KEY_CHECK" | grep -q "InvalidKeyPair.NotFound"; then
    echo "Creating new key pair: $KEY_NAME"

    # Write to temp file first, then fix permissions, then move to final location
    TEMP_KEY=$(mktemp /tmp/bdsp_key_XXXX.pem)
    aws ec2 --profile $AWS_PROFILE create-key-pair \
        --region $REGION \
        --key-name $KEY_NAME \
        --query 'KeyMaterial' \
        --output text > "$TEMP_KEY"

    # Move to final location
    mv -f "$TEMP_KEY" "$KEY_FILE"

    # Fix Windows permissions: strip inheritance, grant only current user
    WIN_KEY_FILE="$(cygpath -w $KEY_FILE)"
    icacls "$WIN_KEY_FILE" /inheritance:r > /dev/null
    icacls "$WIN_KEY_FILE" /remove "BUILTIN\Users" > /dev/null 2>&1 || true
    icacls "$WIN_KEY_FILE" /remove "BUILTIN\Administrators" > /dev/null 2>&1 || true
    icacls "$WIN_KEY_FILE" /remove "NT AUTHORITY\SYSTEM" > /dev/null 2>&1 || true
    icacls "$WIN_KEY_FILE" /grant:r "$(whoami):R" > /dev/null

    echo "✓ Key pair created: $KEY_NAME"
    echo "✓ PEM file saved:   $KEY_FILE"
    echo ""
    echo "  IMPORTANT: Keep this file safe!"
    echo "  You need it to SSH into instances for debugging:"
    echo "  ssh -i $KEY_FILE ec2-user@<instance-ip>"
else
    echo "✓ Key pair already exists: $KEY_NAME"
    if [ -f "$KEY_FILE" ]; then
        echo "✓ PEM file found: $KEY_FILE"
    else
        echo "⚠ PEM file not found locally: $KEY_FILE"
        echo "  If you need SSH access, re-create the key pair:"
        echo "  aws ec2 delete-key-pair --key-name $KEY_NAME --profile $AWS_PROFILE --region $REGION"
        echo "  Then re-run this script."
    fi
fi

# ============================================================================
# STEP 4: Get AWS Credentials (using bidmc profile)
# ============================================================================

echo ""
echo "Step 4: Configuring AWS credentials for instances..."
echo "⚠ Using bidmc user credentials (not creating IAM roles)"

# Get AWS credentials from the bidmc profile
AWS_ACCESS_KEY=$(aws configure get aws_access_key_id --profile $AWS_PROFILE)
AWS_SECRET_KEY=$(aws configure get aws_secret_access_key --profile $AWS_PROFILE)

if [ -z "$AWS_ACCESS_KEY" ] || [ -z "$AWS_SECRET_KEY" ]; then
    echo "✗ Could not retrieve credentials from bidmc profile"
    echo "  Make sure you have configured: aws configure --profile bidmc"
    exit 1
fi

echo "✓ Retrieved credentials from bidmc profile"
echo "  Access Key: ${AWS_ACCESS_KEY:0:10}..."
echo ""
echo "Note: Instances will use these user credentials for S3 access"

# ============================================================================
# STEP 4b: Get latest Amazon Linux 2 AMI (dynamic - never outdated)
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
echo "✓ VPC:             $VPC_ID"
echo "✓ Subnet:          $SUBNET_ID"
echo "✓ Security Groups: $SG_IDS_COMMA"

# ============================================================================
# STEP 5: Create User Data Script
# ============================================================================

echo ""
echo "Step 5: Generating startup script..."

cat > ./user-data.sh <<USERDATA
#!/bin/bash
set -e

# Redirect output to log
exec > >(tee /var/log/user-data.log)
exec 2>&1

echo "=========================================="
echo "Instance Startup: \$(date)"
echo "=========================================="

# Install dependencies
echo "Installing dependencies..."
yum update -y
amazon-linux-extras install python3.9 -y
yum install git -y

# Clone repository
echo "Cloning repository..."
cd /home/ec2-user
git clone https://github.com/bdsp-core/philter-plus-deidentification.git
cd philter-plus-deidentification
git checkout db_integrated

# Install Python packages
echo "Installing Python packages..."
pip3.9 install --upgrade pip
pip3.9 install -r requirements.txt
pip3.9 install pyarrow pandas boto3 s3fs

# Configure AWS credentials (using bidmc user credentials)
echo "Configuring AWS credentials..."
mkdir -p /home/ec2-user/.aws

cat > /home/ec2-user/.aws/credentials <<AWSCREDS
[default]
aws_access_key_id = ${AWS_ACCESS_KEY}
aws_secret_access_key = ${AWS_SECRET_KEY}
AWSCREDS

cat > /home/ec2-user/.aws/config <<AWSCONFIG
[default]
region = ${REGION}
output = json
AWSCONFIG

chown -R ec2-user:ec2-user /home/ec2-user/.aws
chmod 600 /home/ec2-user/.aws/credentials
chmod 600 /home/ec2-user/.aws/config

echo "✓ AWS credentials configured"

# Download Philter config from S3
echo "Downloading Philter config..."
mkdir -p configs
aws s3 cp s3://${BUCKET}/${OUTPUT_PREFIX}/config/philter_one.json configs/philter_one.json

# Get partition ID from instance tag
echo "Getting partition assignment..."
INSTANCE_ID=\$(ec2-metadata --instance-id | cut -d " " -f 2)
PARTITION=\$(aws ec2 describe-tags \
    --region ${REGION} \
    --filters "Name=resource-id,Values=\$INSTANCE_ID" "Name=key,Values=Partition" \
    --query 'Tags[0].Value' --output text)

echo "Assigned to partition: \$PARTITION"

# Download the list of subfolders assigned to this instance
echo "Downloading subfolder assignment list..."
aws s3 cp s3://${BUCKET}/${OUTPUT_PREFIX}/assignments/partition_\${PARTITION}.txt /tmp/my_subfolders.txt

echo "Subfolders to process:"
cat /tmp/my_subfolders.txt

# Process each assigned subfolder
cd /home/ec2-user/philter-plus-deidentification

> /var/log/deidentify.log  # Create empty log

(
    SUBFOLDER_NUM=0
    while IFS= read -r SUBFOLDER_PATH; do
        [ -z "\$SUBFOLDER_PATH" ] && continue

        # Derive a clean folder name for the output path
        FOLDER_NAME=\$(echo "\$SUBFOLDER_PATH" | sed 's|s3://||' | tr '/' '_' | sed 's/_\$//')

        echo "==============================" | tee -a /var/log/deidentify.log
        echo "Processing: \$SUBFOLDER_PATH"  | tee -a /var/log/deidentify.log
        echo "Output to:  s3://${BUCKET}/${OUTPUT_PREFIX}/output/\${FOLDER_NAME}/" | tee -a /var/log/deidentify.log
        echo "==============================" | tee -a /var/log/deidentify.log

        python3.9 process_parquet_aws.py \
            --input-path "\$SUBFOLDER_PATH" \
            --output-path "s3://${BUCKET}/${OUTPUT_PREFIX}/output/\${FOLDER_NAME}/" \
            --workers 120 \
            --philter-config configs/philter_one.json \
            >> /var/log/deidentify.log 2>&1

        SUBFOLDER_NUM=\$(( SUBFOLDER_NUM + 1 ))
        echo "Completed subfolder \$SUBFOLDER_NUM" | tee -a /var/log/deidentify.log

    done < /tmp/my_subfolders.txt

    echo "=========================================="  | tee -a /var/log/deidentify.log
    echo "ALL SUBFOLDERS COMPLETE at \$(date)"         | tee -a /var/log/deidentify.log
    echo "Partition \$PARTITION finished"               | tee -a /var/log/deidentify.log
    echo "=========================================="  | tee -a /var/log/deidentify.log

    # Upload completion marker
    echo "Processing completed at \$(date). Subfolders done: \$SUBFOLDER_NUM" \
        | tee /var/log/processing-complete.log
    aws s3 cp /var/log/processing-complete.log \
        s3://${BUCKET}/${OUTPUT_PREFIX}/logs/partition_\${PARTITION}_complete.log

) &

PID=\$!
echo "Processing started with PID: \$PID"
echo \$PID > /var/run/deidentify.pid

echo "=========================================="
echo "Startup Complete: \$(date)"
echo "Instance ready for processing"
echo "=========================================="
USERDATA

echo "✓ User data script created"

# Base64 encode for Windows-compatible passing to AWS CLI
USER_DATA_B64=$(base64 -w 0 ./user-data.sh)

# ============================================================================
# STEP 5: Launch EC2 Instances
# ============================================================================

echo ""
echo "Step 6: Launching $NUM_INSTANCES instances..."

INSTANCE_IDS=()

for i in $(seq 0 $(($NUM_INSTANCES - 1))); do
    echo ""
    echo "Launching instance $i (partition $i)..."

    INSTANCE_ID=$(aws ec2 --profile $AWS_PROFILE run-instances \
        --region $REGION \
        --image-id $AMI_ID \
        --instance-type $INSTANCE_TYPE \
        --key-name $KEY_NAME \
        --network-interfaces "DeviceIndex=0,SubnetId=${SUBNET_ID},Groups=${SG_IDS_COMMA},AssociatePublicIpAddress=true" \
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
# STEP 6: Wait for Instances to Start
# ============================================================================

echo ""
echo "Step 7: Waiting for instances to start..."

aws ec2 --profile $AWS_PROFILE wait instance-running --region $REGION --instance-ids ${INSTANCE_IDS[@]}

echo "✓ All instances running"

# Get instance details
echo ""
echo "Instance Details:"
aws ec2 --profile $AWS_PROFILE describe-instances \
    --region $REGION \
    --instance-ids ${INSTANCE_IDS[@]} \
    --query 'Reservations[].Instances[].[InstanceId,State.Name,PublicIpAddress,PrivateIpAddress,Tags[?Key==`Partition`].Value|[0]]' \
    --output table

# ============================================================================
# SUMMARY
# ============================================================================

echo ""
echo "=========================================="
echo "✓ DEPLOYMENT COMPLETE"
echo "=========================================="
echo ""
echo "Input:  s3://$BUCKET/$INPUT_PREFIX"
echo "Output: s3://$BUCKET/$OUTPUT_PREFIX/output/"
echo "Instances Launched: $NUM_INSTANCES"
echo ""
echo "Next Steps:"
echo ""
echo "1. Instances are already processing - no data upload needed!"
echo "   Input is read directly from: s3://$BUCKET/$INPUT_PREFIX"
echo ""
echo "2. Monitor progress:"
echo "   aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$OUTPUT_PREFIX/output/ --recursive --human-readable"
echo ""
echo "3. Check completion logs:"
echo "   aws s3 --profile $AWS_PROFILE ls s3://$BUCKET/$OUTPUT_PREFIX/logs/"
echo ""
echo "4. SSH to instance for debugging (optional):"
echo "   ssh -i $KEY_FILE ec2-user@<instance-ip>"
echo "   tail -f /var/log/deidentify.log"
echo "   tail -f /var/log/user-data.log"
echo ""
echo "   PEM key file location: $KEY_FILE"
echo ""
echo "5. Download results when done:"
echo "   aws s3 --profile $AWS_PROFILE sync s3://$BUCKET/$OUTPUT_PREFIX/output/ ./deidentified_output/"
echo ""
echo "6. Cleanup when done:"
echo "   ./cleanup_aws.sh --delete-s3"
echo ""
echo "=========================================="
echo ""

# Save instance IDs for later
echo ${INSTANCE_IDS[@]} > /tmp/philter-instance-ids.txt
echo "Instance IDs saved to: /tmp/philter-instance-ids.txt"
