#!/bin/bash
#
# TEST SCRIPT - Use existing EC2 instance to read 1 parquet file schema and upload to S3.
#
# Output: Schema saved to s3://bdsp-site-mgb/philter-deidentify/schema_test.txt
#

set -e

BUCKET="bdsp-site-mgb"
INPUT_PREFIX="I0001_Notes/Notes_parquet_15_and_before/"
REGION="us-east-1"
AWS_PROFILE="bidmc"
OUTPUT_PREFIX="philter-deidentify"
INSTANCE_HOST="ec2-100-25-98-114.compute-1.amazonaws.com"
KEY_FILE="/c/Users/bdsp/Downloads/testec2pem.pem"

echo "=========================================="
echo "EC2 Schema Test (existing instance)"
echo "=========================================="
echo ""
echo "Will:"
echo "  1. SSH into $INSTANCE_HOST"
echo "  2. Read schema from s3://$BUCKET/$INPUT_PREFIX"
echo "  3. Save schema to s3://$BUCKET/$OUTPUT_PREFIX/schema_test.txt"
echo ""
read -p "Continue? (y/n) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then echo "Aborted."; exit 1; fi

# ============================================================================
# Get bidmc credentials (SSO - includes session token)
# ============================================================================

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
# Run schema test on existing instance via SSH
# ============================================================================

echo ""
echo "Connecting to $INSTANCE_HOST ..."

ssh -i "$KEY_FILE" \
    -o StrictHostKeyChecking=no \
    -o ConnectTimeout=10 \
    ec2-user@$INSTANCE_HOST bash <<REMOTE
set -e

echo "=== Schema Test ==="
echo "Started: \$(date)"

# Install minimal dependencies if not already present
echo "Installing dependencies..."
sudo yum install -y python python-pip 2>/dev/null | tail -1 || true
pip3 install --quiet pyarrow boto3 s3fs 2>/dev/null || pip3 install pyarrow boto3 s3fs

# Configure AWS credentials
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
echo "✓ Credentials configured"

# Find first parquet file
echo "Finding first parquet file in s3://${BUCKET}/${INPUT_PREFIX} ..."
FIRST_FILE=\$(aws s3 ls s3://${BUCKET}/${INPUT_PREFIX} --recursive \
    | grep '\.parquet\$' | head -1 | awk '{print \$4}')

if [ -z "\$FIRST_FILE" ]; then
    echo "ERROR: No parquet files found"
    exit 1
fi

FIRST_FILE_PATH="s3://${BUCKET}/\$FIRST_FILE"
echo "Found: \$FIRST_FILE_PATH"

# Read schema using Python
python - <<PYEOF
import pyarrow.parquet as pq
import s3fs

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
REMOTE

echo ""
echo "========================================================"
echo "✓ Schema test complete"
echo ""
echo "To download schema:"
echo "  aws s3 cp s3://$BUCKET/$OUTPUT_PREFIX/schema_test.txt ./parquet_schema.txt --profile $AWS_PROFILE"
echo "========================================================"
