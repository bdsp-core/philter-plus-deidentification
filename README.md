# Philter De-identification Pipeline

De-identifies clinical notes (free text) stored as Parquet files in AWS S3 using the **Philter** NLP engine. Built to process **500+ million medical records** across multiple EC2 instances in parallel.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Input / Output Schema](#input--output-schema)
- [Prerequisites](#prerequisites)
- [Local Setup](#local-setup)
- [Testing on EC2](#testing-on-ec2)
- [Production Deployment](#production-deployment)
- [Monitoring](#monitoring)
- [Cost Estimate](#cost-estimate)
- [File Reference](#file-reference)

---

## How It Works

The de-identification pipeline has two stages applied to each clinical note:

1. **Keyword Removal** (`keyword_removal.py`) — Regex-based removal of ~250 site-specific identifiers (hospital names, addresses, MRNs, abbreviations). Replaces matches with `*****`.
2. **Philter NLP** (`philter.py`) — 330 regex patterns + NLTK POS tagging (Named Entity Recognition) to detect and redact PHI such as patient names, dates, phone numbers, SSNs, locations, etc. Uses config from `configs/philter_one.json`.

Processing flow:
```
S3 Parquet Input → Read batches → keyword_removal → Philter NLP → Write de-identified Parquet to S3
```

Each EC2 instance runs 120 parallel workers using Python's `multiprocessing.Pool`. Checkpoints are saved to S3 after each batch for automatic resume on interruption.

---

## Input / Output Schema

### Input (Parquet files in S3)

| Column | Type | Description |
|--------|------|-------------|
| `NoteCSNID` | string | Note CSN identifier |
| `BDSPPatientID` | int32 | BDSP patient identifier |
| `PMRNID` | string | PMR number |
| `PatientEncounterID` | string | Encounter identifier |
| `ShiftedDays` | int32 | Date shift offset |
| `bdsp_encounter_id` | int64 | BDSP encounter identifier |
| `sum` | int64 | Sum field |
| `ShiftedContactDate` | string | Date-shifted contact date |
| `ShiftedContactYear` | int32 | Date-shifted year |
| `NoteTXT` | string | **Clinical note text (to be de-identified)** |
| `de_id_filename` | string | Filename key used by Philter |

### Output (Parquet files written to S3)

| Column | Type | Description |
|--------|------|-------------|
| `BDSPPatientID` | int32 | BDSP patient identifier |
| `bdsp_encounter_id` | int64 | BDSP encounter identifier |
| `ShiftedContactDate` | string | Date-shifted contact date |
| `NoteTXT` | string | **De-identified clinical note text** |
| `de_id_filename` | string | Filename key |

---

## Prerequisites

Before running any scripts, you need:

### 1. AWS Credentials

A configured AWS profile (`bidmc`) in `~/.aws/credentials`:

```ini
[bidmc]
aws_access_key_id = YOUR_KEY
aws_secret_access_key = YOUR_SECRET
aws_session_token = YOUR_TOKEN
```

### 2. EC2 Instances (must be running before deployment)

The scripts **do not create** EC2 instances. You must create them manually in the AWS Console:

| Setting | Value |
|---------|-------|
| **AMI** | Amazon Linux 2 (x86_64) |
| **Instance Type** | `t3.micro` (test) or `c6i.32xlarge` (production) |
| **Key Pair** | Your PEM key |
| **VPC** | `vpc-0b19ba4d16f0f4695` |
| **Subnet** | `subnet-032f4ed8e15acf550` (public, us-east-1a) |
| **Security Group** | `sg-0350d41bfbbc1f0b6` (launch-wizard-20) |
| **Auto-assign Public IP** | Enabled |
| **Storage** | 100 GB gp3 (production), 8 GB (test) |

### 3. PEM File

- PEM file with `chmod 400` permissions
- Security group must have **SSH (port 22)** open for your IP

```bash
# Set correct permissions
chmod 400 /path/to/your-key.pem

# Add your IP to the security group
MY_IP=$(curl -s https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress \
    --profile bidmc --region us-east-1 \
    --group-id sg-0350d41bfbbc1f0b6 \
    --protocol tcp --port 22 --cidr ${MY_IP}/32
```

### 4. S3 Access

The `bidmc` profile must have read/write access to:
- `s3://bdsp-site-mgb/I0001_Notes/` (input - read only)
- `s3://bdsp-site-mgb/philter-deidentify/` (output - read/write)

---

## Local Setup

```bash
# Create conda environment
conda create -n philter python=3.9 -y
conda activate philter

# Install dependencies
pip install -r requirements.txt
pip install pyarrow pandas boto3 s3fs nltk

# Download NLTK data (required by Philter)
python -c "
import nltk
nltk.download('averaged_perceptron_tagger')
nltk.download('averaged_perceptron_tagger_eng')
nltk.download('punkt')
nltk.download('punkt_tab')
"
```

### Read a Parquet file locally

```bash
# Local file
python read_parquet.py /path/to/file.parquet

# S3 file
python read_parquet.py s3://bdsp-site-mgb/philter-deidentify/output/ --profile bidmc --rows 20
```

---

## Testing on EC2

The test script (`test_ec2_schema.sh`) runs a small end-to-end test on a single EC2 instance to validate the full pipeline before production.

### Setup

1. Create **1 × t3.micro** EC2 instance in AWS Console
2. Update the script with your instance details:

```bash
# Edit test_ec2_schema.sh
INSTANCE_HOST="ec2-XX-XX-XX-XX.compute-1.amazonaws.com"
KEY_FILE="/path/to/your-test-key.pem"
```

### Run

```bash
chmod +x test_ec2_schema.sh
./test_ec2_schema.sh
```

### What it does

1. Creates a tarball of the project (repo is private, can't git clone on EC2)
2. SCPs tarball to the EC2 instance
3. SSHes in and installs Python 3.9, pip packages, NLTK data
4. Reads 500 rows from the first Parquet file in S3
5. Runs full Philter de-identification (keyword_removal + Philter NLP)
6. Writes output Parquet to S3 at `s3://bdsp-site-mgb/philter-deidentify/test_output/`
7. Prints sample de-identified output for verification

### Verify test output

```bash
# Download and inspect
aws s3 sync s3://bdsp-site-mgb/philter-deidentify/test_output/ ./test_output/ --profile bidmc
python read_parquet.py ./test_output/

# Clean up test output
aws s3 rm s3://bdsp-site-mgb/philter-deidentify/test_output/ --recursive --profile bidmc
```

---

## Production Deployment

### Setup

1. Create **4 × c6i.32xlarge** EC2 instances in AWS Console (Spot recommended)
2. Update `deploy_production.sh` with your instance IPs and PEM path:

```bash
# Edit deploy_production.sh
KEY_FILE="/path/to/your-production-key.pem"
WORKER_IPS=(
    "1.2.3.4"       # Worker-0
    "5.6.7.8"       # Worker-1
    "9.10.11.12"    # Worker-2
    "13.14.15.16"   # Worker-3
)
```

3. Ensure SSH (port 22) is open for your current IP in the security group

### Run

```bash
chmod +x deploy_production.sh
./deploy_production.sh
```

### What it does

For each of the 4 instances, the script:

1. **SCPs** the project tarball to the instance
2. **SSHes in** and runs setup:
   - Installs Python 3.9, pip, pyarrow, pandas, boto3, s3fs, nltk
   - Downloads NLTK data (averaged_perceptron_tagger, punkt)
   - Extracts project and installs requirements.txt
   - Writes AWS credentials to `~/.aws/credentials`
   - Downloads partition assignment (which subfolders this instance processes)
3. **Starts processing in background** via `nohup`
4. **Disconnects SSH** and moves to the next instance

Processing runs unattended. You can close your laptop — it continues on EC2.

### Partition Assignments

9 input subfolders are split across 4 instances:

| Worker | Subfolders | Approx Records |
|--------|-----------|----------------|
| Worker-0 | `Notes_parquet_15_and_before/`, `Notes_parquet_16_17/` | ~81M |
| Worker-1 | `Notes_parquet_18_19/`, `Notes_parquet_20/` | ~148M |
| Worker-2 | `Notes_parquet_21/`, `Notes_parquet_22/`, `Notes_parquet_25_26/` | ~122M+ |
| Worker-3 | `Notes_parquet_23/`, `Notes_parquet_24/` | ~161M |

---

## Monitoring

### Quick status check

```bash
./check_status.sh
```

Shows: instance status, completed partitions, checkpoint progress, output file count.

### Check completion logs

```bash
# See which partitions are done (when all 4 exist, the job is done)
aws s3 --profile bidmc ls s3://bdsp-site-mgb/philter-deidentify/logs/
```

### SSH into a worker for live progress

```bash
ssh -i /path/to/key.pem ec2-user@<worker-ip>
tail -f /var/log/deidentify.log
```

You'll see output like:
```
Progress: 5,000,000 / 62,500,000 (8.0%)
  Speed: 550.3 rec/sec
  ETA: 29.1 hours
```

### Check output files

```bash
aws s3 --profile bidmc ls s3://bdsp-site-mgb/philter-deidentify/output/ --recursive --summarize
```

### Download results

```bash
aws s3 --profile bidmc sync s3://bdsp-site-mgb/philter-deidentify/output/ ./deidentified_output/
```

---

## Cost Estimate

### Production (4 × c6i.32xlarge)

| Resource | Spot Cost | On-Demand Cost |
|----------|-----------|----------------|
| 4 instances × ~70 hours | ~$450 | ~$1,520 |
| EBS Storage (100GB × 4) | ~$32 | ~$32 |
| **Total** | **~$300-500** | **~$1,550** |

Estimated runtime: **2.5-3 days**

### Test (1 × t3.micro)

Negligible cost (~$0.01)

---

## File Reference

| File | Description |
|------|-------------|
| `deploy_production.sh` | **Production deployment** — SCPs project to 4 EC2 instances, starts processing |
| `test_ec2_schema.sh` | **Test script** — runs 500-row end-to-end test on 1 EC2 instance |
| `check_status.sh` | **Monitoring** — checks completion logs, checkpoints, output files |
| `check_deployment_ready.sh` | **Pre-flight checks** — verifies credentials, S3 access, config file |
| `process_parquet_aws.py` | Runs on EC2 — reads Parquet from S3, de-identifies, writes output |
| `read_parquet.py` | Utility — read and print any Parquet file (local or S3) |
| `philter.py` | Philter NLP de-identification engine |
| `keyword_removal.py` | Site-specific keyword removal (hospital names, MRNs, etc.) |
| `configs/philter_one.json` | Philter configuration (330 filter rules) |
| `deploy_aws.sh` | Automated deployment (creates instances via AWS CLI) |
| `cleanup_aws.sh` | Terminates instances and cleans up S3 |
| `requirements.txt` | Python package dependencies |

---

## S3 Layout

```
s3://bdsp-site-mgb/
├── I0001_Notes/                              ← INPUT (do not modify)
│   ├── Notes_parquet_15_and_before/
│   ├── Notes_parquet_16_17/
│   ├── Notes_parquet_18_19/
│   ├── Notes_parquet_20/
│   ├── Notes_parquet_21/
│   ├── Notes_parquet_22/
│   ├── Notes_parquet_23/
│   ├── Notes_parquet_24/
│   └── Notes_parquet_25_26/
└── philter-deidentify/                       ← OUTPUT
    ├── assignments/partition_0..3.txt
    ├── config/philter_one.json
    ├── output/                               ← De-identified Parquet files
    └── logs/partition_X_complete.log          ← Completion markers
```
