#!/bin/bash
#
# TEST SCRIPT - Launch a tiny EC2 instance, read 1 parquet file schema, terminate.
#
# Cost: < $0.001 (t3.micro for ~5 minutes)
# Free tier: FREE if your account is under 750 hrs/month t2.micro usage
#
# Output: Schema saved to ./parquet_schema.txt
#

set -e

BUCKET="bdsp-site-mgb"
INPUT_PREFIX="I0001_Notes/Notes_parquet_15_and_before/"  # Smallest subfolder
REGION="us-east-1"
INSTANCE_TYPE="t3.micro"          # Cheapest general purpose (~$0.01/hr)
AWS_PROFILE="bidmc"
KEY_NAME="bdsp-ec2"
KEY_FILE="./bdsp-ec2.pem"
OUTPUT_PREFIX="philter-deidentify"
VPC_ID="vpc-0b19ba4d16f0f4695"          # bdsp-webapp-vpc
SUBNET_ID="subnet-032f4ed8e15acf550"    # bdsp-webapp-subnet-public1-us-east-1a
SG_IDS_COMMA="sg-0f0200d3a98d50585"    # launch-wizard-17

echo "=========================================="
echo "EC2 Schema Test (t3.micro, < \$0.001)"
echo "=========================================="
echo ""
echo "Will:"
echo "  1. Launch 1x t3.micro instance"
echo "  2. Read schema from s3://$BUCKET/$INPUT_PREFIX"
echo "  3. Save schema to s3://$BUCKET/$OUTPUT_PREFIX/schema_test.txt"
echo "  4. Terminate instance automatically"
echo ""
read -p "Continue? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then echo "Aborted."; exit 1; fi

# ============================================================================
# Get bidmc credentials
# ============================================================================

AWS_ACCESS_KEY=$(aws configure get aws_access_key_id --profile $AWS_PROFILE)
AWS_SECRET_KEY=$(aws configure get aws_secret_access_key --profile $AWS_PROFILE)

if [ -z "$AWS_ACCESS_KEY" ] || [ -z "$AWS_SECRET_KEY" ]; then
    echo "✗ Could not retrieve credentials from $AWS_PROFILE profile"
    exit 1
fi
echo "✓ Credentials retrieved"

# ============================================================================
# Get latest Amazon Linux 2 AMI (dynamic - never outdated)
# ============================================================================

echo "Looking up latest Amazon Linux 2 AMI..."
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
# Network Configuration
# ============================================================================

echo "Network configuration..."
echo "✓ VPC:             $VPC_ID"
echo "✓ Subnet:          $SUBNET_ID"
echo "✓ Security Groups: $SG_IDS_COMMA"

# ============================================================================
# Check/create key pair
# ============================================================================

KEY_CHECK=$(aws ec2 --profile $AWS_PROFILE describe-key-pairs \
    --region $REGION --key-names $KEY_NAME 2>&1) || true

if echo "$KEY_CHECK" | grep -q "InvalidKeyPair.NotFound"; then
    echo "Creating key pair: $KEY_NAME"

    # Write to temp file first, then fix permissions, then move to final location
    TEMP_KEY=$(mktemp /tmp/bdsp_key_XXXX.pem)
    aws ec2 --profile $AWS_PROFILE create-key-pair \
        --region $REGION --key-name $KEY_NAME \
        --query 'KeyMaterial' --output text > "$TEMP_KEY"

    # Move to final location
    mv -f "$TEMP_KEY" "$KEY_FILE"

    # Fix Windows permissions: strip inheritance, grant only current user
    WIN_KEY_FILE="$(cygpath -w $KEY_FILE)"
    icacls "$WIN_KEY_FILE" /inheritance:r > /dev/null
    icacls "$WIN_KEY_FILE" /remove "BUILTIN\Users" > /dev/null 2>&1 || true
    icacls "$WIN_KEY_FILE" /remove "BUILTIN\Administrators" > /dev/null 2>&1 || true
    icacls "$WIN_KEY_FILE" /remove "NT AUTHORITY\SYSTEM" > /dev/null 2>&1 || true
    icacls "$WIN_KEY_FILE" /grant:r "$(whoami):R" > /dev/null

    echo "✓ Key pair created, PEM saved: $KEY_FILE"
else
    echo "✓ Key pair exists: $KEY_NAME"
fi

# ============================================================================
# Write test user-data script
# ============================================================================

cat > ./test-user-data.sh <<USERDATA
#!/bin/bash
exec > >(tee /var/log/user-data.log) 2>&1
set -e

echo "=== Schema Test Instance ==="
echo "Started: \$(date)"

# Install minimal dependencies
yum install -y python3 python3-pip git 2>/dev/null || true

pip3 install --quiet pyarrow boto3 s3fs 2>/dev/null

# Configure AWS credentials
mkdir -p /root/.aws
cat > /root/.aws/credentials <<CREDS
[default]
aws_access_key_id = ${AWS_ACCESS_KEY}
aws_secret_access_key = ${AWS_SECRET_KEY}
CREDS
cat > /root/.aws/config <<CONF
[default]
region = ${REGION}
output = json
CONF
chmod 600 /root/.aws/credentials

echo "✓ Credentials configured"

# Find first parquet file
echo "Finding first parquet file in s3://${BUCKET}/${INPUT_PREFIX} ..."
FIRST_FILE=\$(aws s3 ls s3://${BUCKET}/${INPUT_PREFIX} --recursive \
    | grep '\.parquet$' | head -1 | awk '{print \$4}')

if [ -z "\$FIRST_FILE" ]; then
    echo "ERROR: No parquet files found"
    exit 1
fi

FIRST_FILE_PATH="s3://${BUCKET}/\$FIRST_FILE"
echo "Found: \$FIRST_FILE_PATH"

# Read schema using Python
python3 - <<PYEOF
import pyarrow.parquet as pq
import s3fs
import json

path = "\$FIRST_FILE_PATH"
print(f"Reading schema from: {path}")

fs = s3fs.S3FileSystem()
pf = pq.ParquetFile(fs.open(path))

schema = pf.schema_arrow
metadata = pf.metadata

output = []
output.append("=" * 60)
output.append(f"File: {path}")
output.append("=" * 60)
output.append("")
output.append("SCHEMA:")
output.append("-" * 40)
for field in schema:
    output.append(f"  {field.name}: {field.type}")
output.append("")
output.append("FILE INFO:")
output.append("-" * 40)
output.append(f"  Row groups:    {metadata.num_row_groups}")
output.append(f"  Total rows:    {metadata.num_rows:,}")
output.append(f"  Total columns: {metadata.num_columns}")
output.append("")
output.append("ROW GROUP SIZES:")
output.append("-" * 40)
for i in range(metadata.num_row_groups):
    rg = metadata.row_group(i)
    output.append(f"  Group {i}: {rg.num_rows:,} rows, {rg.total_byte_size / 1024**2:.1f} MB compressed")

text = "\n".join(output)
print(text)

# Save locally
with open("/tmp/parquet_schema.txt", "w") as f:
    f.write(text)

print("")
print("Schema written to /tmp/parquet_schema.txt")
PYEOF

# Upload schema to S3
aws s3 cp /tmp/parquet_schema.txt s3://${BUCKET}/${OUTPUT_PREFIX}/schema_test.txt
echo "✓ Schema uploaded to s3://${BUCKET}/${OUTPUT_PREFIX}/schema_test.txt"

echo ""
echo "=== Test Complete: \$(date) ==="

# Shut down the instance after completing (saves cost)
shutdown -h now
USERDATA

echo "✓ Test script created"

# Base64 encode for Windows-compatible passing to AWS CLI
USER_DATA_B64=$(base64 -w 0 ./test-user-data.sh)

# ============================================================================
# Launch t3.micro instance
# ============================================================================

echo ""
echo "Launching t3.micro test instance..."

INSTANCE_ID=$(aws ec2 --profile $AWS_PROFILE run-instances \
    --region $REGION \
    --image-id $AMI_ID \
    --instance-type $INSTANCE_TYPE \
    --key-name $KEY_NAME \
    --network-interfaces "DeviceIndex=0,SubnetId=${SUBNET_ID},Groups=${SG_IDS_COMMA},AssociatePublicIpAddress=true" \
    --user-data "$USER_DATA_B64" \
    --tag-specifications \
        "ResourceType=instance,Tags=[
            {Key=Name,Value=Philter-Schema-Test},
            {Key=Project,Value=Philter-Deidentify}
        ]" \
    --query 'Instances[0].InstanceId' \
    --output text)

echo "✓ Instance launched: $INSTANCE_ID"
echo ""
echo "Waiting for instance to start..."
aws ec2 --profile $AWS_PROFILE wait instance-running \
    --region $REGION --instance-ids $INSTANCE_ID

PUBLIC_IP=$(aws ec2 --profile $AWS_PROFILE describe-instances \
    --region $REGION --instance-ids $INSTANCE_ID \
    --query 'Reservations[0].Instances[0].PublicIpAddress' \
    --output text)

echo "✓ Instance running"
echo "  Instance ID: $INSTANCE_ID"
echo "  Public IP:   $PUBLIC_IP"
echo ""
echo "The instance will:"
echo "  1. Install Python + pyarrow (~2-3 min)"
echo "  2. Read parquet schema (~30 sec)"
echo "  3. Upload schema to S3"
echo "  4. Shut itself down automatically"
echo ""
echo "========================================================"
echo "To monitor (optional SSH):"
echo "  ssh -i $KEY_FILE ec2-user@$PUBLIC_IP"
echo "  sudo tail -f /var/log/user-data.log"
echo ""
echo "To wait for schema result:"
echo "  Watch for: s3://$BUCKET/$OUTPUT_PREFIX/schema_test.txt"
echo "  Check with:"
echo "  aws s3 ls s3://$BUCKET/$OUTPUT_PREFIX/schema_test.txt --profile $AWS_PROFILE"
echo ""
echo "To download schema when ready:"
echo "  aws s3 cp s3://$BUCKET/$OUTPUT_PREFIX/schema_test.txt ./parquet_schema.txt --profile $AWS_PROFILE"
echo "  cat ./parquet_schema.txt"
echo ""
echo "Instance auto-terminates when done. Or terminate manually:"
echo "  aws ec2 terminate-instances --instance-ids $INSTANCE_ID --region $REGION --profile $AWS_PROFILE"
echo "========================================================"

# Save instance ID
echo $INSTANCE_ID > /tmp/test-instance-id.txt
echo ""
echo "Instance ID saved to: /tmp/test-instance-id.txt"
