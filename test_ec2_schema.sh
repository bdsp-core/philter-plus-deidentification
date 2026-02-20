#!/bin/bash
#
# TEST SCRIPT - Full end-to-end de-identification test on existing EC2 instance.
#
# Steps:
#   1. SSH into existing EC2 instance
#   2. Install dependencies, upload project via SCP
#   3. Read 500 rows from first Parquet file in S3
#   4. Run full Philter de-identification pipeline
#   5. Write output Parquet to S3
#   6. Print summary and sample output
#
# Output: s3://bdsp-site-mgb/philter-deidentify/test_output/
#

set -e

BUCKET="bdsp-site-mgb"
INPUT_PREFIX="I0001_Notes/Notes_parquet_15_and_before/"
REGION="us-east-1"
AWS_PROFILE="bidmc"
OUTPUT_PREFIX="philter-deidentify"
INSTANCE_HOST="ec2-100-25-98-114.compute-1.amazonaws.com"
KEY_FILE="/Users/anjanarayapureddy/Desktop/Philter/testec2pem.pem"
TEST_ROWS=500
TEST_WORKERS=2
TEST_BATCH_SIZE=100

echo "=========================================="
echo "EC2 De-identification Test (end-to-end)"
echo "=========================================="
echo ""
echo "Will:"
echo "  1. SSH into $INSTANCE_HOST"
echo "  2. Install dependencies & upload project via SCP"
echo "  3. Read $TEST_ROWS rows from s3://$BUCKET/$INPUT_PREFIX"
echo "  4. Run full Philter de-identification ($TEST_WORKERS workers)"
echo "  5. Write output to s3://$BUCKET/$OUTPUT_PREFIX/test_output/"
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
# Run full de-identification test on existing instance via SSH
# ============================================================================

# ============================================================================
# Create tarball of project and SCP to EC2 (repo is private, can't git clone)
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
    "$(basename "$SCRIPT_DIR")"

TARBALL_SIZE=$(du -h "$TARBALL" | cut -f1)
echo "✓ Tarball created: $TARBALL ($TARBALL_SIZE)"

echo ""
echo "Uploading project to EC2 via SCP..."
scp -i "$KEY_FILE" \
    -o StrictHostKeyChecking=no \
    -o ConnectTimeout=30 \
    "$TARBALL" ec2-user@$INSTANCE_HOST:/tmp/philter-project.tar.gz

echo "✓ Project uploaded to EC2"

echo ""
echo "Connecting to $INSTANCE_HOST ..."

ssh -i "$KEY_FILE" \
    -o StrictHostKeyChecking=no \
    -o ConnectTimeout=30 \
    -o ServerAliveInterval=60 \
    ec2-user@$INSTANCE_HOST bash <<REMOTE
set -e

echo "=========================================="
echo "De-identification Test Started: \$(date)"
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

# Use whichever python3 is available
PYTHON=\$(command -v python3.9 || command -v python3)
echo "Using Python: \$PYTHON (\$(\$PYTHON --version))"

# Install pip packages
\$PYTHON -m pip install --quiet --upgrade pip 2>/dev/null || true
\$PYTHON -m pip install --quiet pyarrow pandas boto3 s3fs nltk 2>/dev/null || \
    \$PYTHON -m pip install pyarrow pandas boto3 s3fs nltk

# Download NLTK data required by Philter (POS tagging for NER)
echo "Downloading NLTK data..."
\$PYTHON -c "import nltk; nltk.download('averaged_perceptron_tagger', quiet=True); nltk.download('averaged_perceptron_tagger_eng', quiet=True); nltk.download('punkt', quiet=True); nltk.download('punkt_tab', quiet=True); print('✓ NLTK data downloaded')"

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
# Step 4: Prepare test data (500 rows from S3)
# -----------------------------------------------
echo ""
echo "Step 4: Preparing test data (${TEST_ROWS} rows)..."

rm -rf /tmp/test_input /tmp/test_output
mkdir -p /tmp/test_input /tmp/test_output

\$PYTHON <<PYEOF
import pyarrow.parquet as pq
import pyarrow as pa
import s3fs
import os

print("Connecting to S3...")
fs = s3fs.S3FileSystem()

# Find first parquet file
prefix = "${BUCKET}/${INPUT_PREFIX}"
files = fs.ls(prefix)
parquet_files = [f for f in files if f.endswith('.parquet')]

if not parquet_files:
    # Try recursive listing
    all_files = fs.glob(f"{prefix}**/*.parquet")
    parquet_files = all_files

if not parquet_files:
    print("ERROR: No parquet files found!")
    exit(1)

first_file = f"s3://{parquet_files[0]}"
print(f"Reading from: {first_file}")

# Read first row group only (memory-safe)
pf = pq.ParquetFile(fs.open(parquet_files[0]))
print(f"  Schema: {[f.name for f in pf.schema_arrow]}")
print(f"  Total rows in file: {pf.metadata.num_rows:,}")
print(f"  Row groups: {pf.metadata.num_row_groups}")

# Read first row group and take ${TEST_ROWS} rows
table = pf.read_row_group(0)
df = table.to_pandas().head(${TEST_ROWS})
print(f"  Selected {len(df)} rows for test")

# Verify required columns exist
required = ['BDSPPatientID', 'bdsp_encounter_id', 'ShiftedContactDate', 'NoteTXT', 'de_id_filename']
missing = [c for c in required if c not in df.columns]
if missing:
    print(f"ERROR: Missing columns: {missing}")
    print(f"Available columns: {list(df.columns)}")
    exit(1)

# Show sample
print(f"\nSample input:")
print(f"  BDSPPatientID: {df['BDSPPatientID'].iloc[0]}")
print(f"  bdsp_encounter_id: {df['bdsp_encounter_id'].iloc[0]}")
print(f"  de_id_filename: {df['de_id_filename'].iloc[0]}")
print(f"  NoteTXT length: {len(str(df['NoteTXT'].iloc[0]))} chars")
print(f"  NoteTXT preview: {str(df['NoteTXT'].iloc[0])[:150]}...")

# Save as local parquet
output_path = "/tmp/test_input/test_sample.parquet"
table = pa.Table.from_pandas(df)
pq.write_table(table, output_path)
print(f"\n✓ Test data saved to {output_path} ({len(df)} rows)")
PYEOF

echo "✓ Test data prepared"

# -----------------------------------------------
# Step 5: Run Philter de-identification
# -----------------------------------------------
echo ""
echo "Step 5: Running Philter de-identification..."
echo "  Workers: ${TEST_WORKERS}"
echo "  Batch size: ${TEST_BATCH_SIZE}"
echo "  Config: configs/philter_one.json"
echo ""

cd /home/ec2-user/philter-plus-deidentification

START_TIME=\$(date +%s)

\$PYTHON process_parquet_aws.py \
    --input-path /tmp/test_input/ \
    --output-path /tmp/test_output/ \
    --workers 1 \
    --batch-size ${TEST_BATCH_SIZE} \
    --philter-config configs/philter_one.json \
    2>&1 | tee /tmp/philter_test.log

END_TIME=\$(date +%s)
ELAPSED=\$(( END_TIME - START_TIME ))

echo ""
echo "✓ De-identification complete in \${ELAPSED}s"

# -----------------------------------------------
# Step 6: Verify output and show sample
# -----------------------------------------------
echo ""
echo "Step 6: Verifying output..."

\$PYTHON <<PYEOF
import pyarrow.parquet as pq
import os

output_dir = "/tmp/test_output"
files = [f for f in os.listdir(output_dir) if f.endswith('.parquet')]

if not files:
    print("ERROR: No output parquet files found!")
    print("Contents of output dir:", os.listdir(output_dir))
    # Show log for debugging
    log_path = "/tmp/philter_test.log"
    if os.path.exists(log_path):
        print("\n--- Last 50 lines of processing log ---")
        with open(log_path) as f:
            lines = f.readlines()
            for line in lines[-50:]:
                print(line.rstrip())
    exit(1)

print(f"Output files: {files}")

total_rows = 0
for f in files:
    path = os.path.join(output_dir, f)
    table = pq.read_table(path)
    df = table.to_pandas()
    total_rows += len(df)

    print(f"\n  File: {f}")
    print(f"  Rows: {len(df)}")
    print(f"  Columns: {list(df.columns)}")

    # Show sample de-identified output
    if len(df) > 0:
        row = df.iloc[0]
        print(f"\n  Sample output (first row):")
        print(f"    BDSPPatientID: {row.get('BDSPPatientID', 'N/A')}")
        print(f"    bdsp_encounter_id: {row.get('bdsp_encounter_id', 'N/A')}")
        print(f"    ShiftedContactDate: {row.get('ShiftedContactDate', 'N/A')}")
        print(f"    de_id_filename: {row.get('de_id_filename', 'N/A')}")
        deid_text = str(row.get('NoteTXT', ''))
        print(f"    NoteTXT length: {len(deid_text)} chars")
        print(f"    NoteTXT preview: {deid_text[:200]}...")

print(f"\n✓ Total output rows: {total_rows}")
PYEOF

# -----------------------------------------------
# Step 7: Upload output to S3
# -----------------------------------------------
echo ""
echo "Step 7: Uploading output to S3..."

aws s3 sync /tmp/test_output/ s3://${BUCKET}/${OUTPUT_PREFIX}/test_output/ --quiet
echo "✓ Output uploaded to s3://${BUCKET}/${OUTPUT_PREFIX}/test_output/"

# -----------------------------------------------
# Summary
# -----------------------------------------------
echo ""
echo "=========================================="
echo "TEST COMPLETE: \$(date)"
echo "=========================================="
echo "  Input:  ${TEST_ROWS} rows from s3://${BUCKET}/${INPUT_PREFIX}"
echo "  Output: s3://${BUCKET}/${OUTPUT_PREFIX}/test_output/"
echo "  Time:   \${ELAPSED}s"
echo "=========================================="
REMOTE

echo ""
echo "========================================================"
echo "✓ End-to-end de-identification test complete"
echo ""
echo "To download output:"
echo "  aws s3 sync s3://$BUCKET/$OUTPUT_PREFIX/test_output/ ./test_output/ --profile $AWS_PROFILE"
echo ""
echo "To inspect output:"
echo "  python3 -c \"import pyarrow.parquet as pq; print(pq.read_table('./test_output/').to_pandas().head())\""
echo ""
echo "To clean up test output:"
echo "  aws s3 rm s3://$BUCKET/$OUTPUT_PREFIX/test_output/ --recursive --profile $AWS_PROFILE"
echo "========================================================"
