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

Each EC2 instance runs 30 parallel workers using Python's `multiprocessing.Pool` with `imap_unordered` for dynamic scheduling. Checkpoints are saved after each file for crash-safe resume on interruption.

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
| **Instance Type** | `t3.micro` (test) or `c6i.16xlarge` (production) |
| **Key Pair** | Your PEM key |
| **VPC** | `vpc-0b19ba4d16f0f4695` |
| **Subnet** | `subnet-032f4ed8e15acf550` (public, us-east-1a) |
| **Security Group** | `sg-0350d41bfbbc1f0b6` (launch-wizard-20) |
| **Auto-assign Public IP** | Enabled |
| **Storage** | 200 GB gp3 (production), 8 GB (test) |

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

## Testing on EC2 (Completed Feb 24, 2026)

Comprehensive testing was performed on a single **c6i.16xlarge** instance (64 vCPU, 128 GB RAM) in `us-east-1a` over 6+ hours. All tests passed.

### Test A: Correctness (Single File)

Processed 1 repartitioned file (~500K records) to validate de-identification quality.

| Metric | Result |
|--------|--------|
| Input records | 500,000 |
| Output records | 498,925 (99.8% retention) |
| Output columns | `BDSPPatientID`, `bdsp_encounter_id`, `ShiftedContactDate`, `NoteTXT`, `de_id_filename` |
| Star replacement ratio | 6.3% of characters replaced with `*` |
| Speed | 86.7 rec/sec (first file, includes warmup) |
| Memory | 19-20 GB (stable) |

### Test B: Crash Recovery

Started processing 2 files, killed the process after file 1 completed, restarted.

| Metric | Result |
|--------|--------|
| Checkpoint detection | "Checkpoint found: 1 files already done" |
| File 1 reprocessed? | No (correctly skipped) |
| File 1 output intact? | Yes |
| File 2 reprocessed? | Yes (re-started from beginning, as expected) |

### Test C: Memory Stability & Sustained Speed (6+ hours)

Ran the full `Notes_parquet_18_19` subfolder (191 repartitioned files, ~93M records) for 6+ hours continuously.

| Metric | Result |
|--------|--------|
| Duration | 6+ hours (still running at test end) |
| Files completed | 5 of 191 (~2.5M records) |
| Speed (file 1, warmup) | 86.7 rec/sec |
| Speed (files 2-5, sustained) | **130-145 rec/sec** |
| Memory at start | 15 GB |
| Memory at 6 hours | 32-34 GB (stable, no growth) |
| OOM crashes | **None** |
| Worker processes alive | 31 (1 main + 30 workers) throughout |

### Key Optimizations Applied During Testing

| Problem | Root Cause | Fix | Impact |
|---------|-----------|-----|--------|
| OOM crashes (120 workers) | Too many workers + deep copy memory growth | Reduced to 30 workers + reference assignment | Memory: 200+ GB → 32 GB stable |
| Slow speed (12.5 rec/sec) | `pool.map()` straggler blocking | `pool.imap_unordered()` with 200-record sub-batches | Speed: 12.5 → 130+ rec/sec |
| Memory growth over time | `copy.deepcopy(data)` per batch | Reference assignment (safe: map_coordinates only reads then deletes key) | Memory stable over 6+ hours |
| Workers running out of work | Batch loop with only 50 sub-batches | Process entire file at once (2500 sub-batches for 500K records) | All 30 workers stay busy |
| Slow data extraction | `df.iterrows()` | Vectorized `list(zip(df[col].values, ...))` | 100x faster extraction |

### Running a Test

```bash
# SSH into a c6i.16xlarge instance
ssh -i /path/to/key.pem ec2-user@<ip>

# Run on a single file
python3.9 process_parquet_aws.py \
    --input-path /home/ec2-user/test_input/ \
    --output-path /home/ec2-user/test_output/ \
    --workers 30 \
    --philter-config configs/philter_one.json
```

---

## Production Deployment (11 × c6i.16xlarge Spot)

### Important: AWS Session Token Expires in 12 Hours

The `bidmc` AWS credentials use a session token that expires after 12 hours. Since processing takes ~5.5 days, data must be copied to EC2 local disk **before** credentials expire. The pipeline:

```
Phase 1 (within 12hrs):  S3 → EC2 local disk (download + repartition)
Phase 2 (~5.5 days):     De-identify on local disk (no AWS access needed)
Phase 3 (after done):    Refresh credentials → upload EC2 local disk → S3
```

### Instance Configuration

| Setting | Value |
|---------|-------|
| Instance type | **c6i.16xlarge** (64 vCPU, 128 GB RAM) |
| Pricing | **Spot** (~$1.10/hr, 60% cheaper than on-demand) |
| EBS | **200 GB gp3** per instance |
| Workers | 30 |
| Sub-batch size | 200 records |
| maxtasksperchild | 3 (periodic worker restart to control memory) |
| Availability Zone | `us-east-1a` |
| Subnet | `subnet-032f4ed8e15acf550` (CIDR: 10.224.10.0/28, 11 usable IPs) |

### Repartitioning

Original S3 parquet files can be very large (up to 7.5M records per file). These must be split into ~500K record chunks to prevent OOM during processing. Each instance runs a repartition step after downloading from S3:

```bash
# Repartitions large parquet files into ~500K record chunks
python3.9 repartition.py /home/ec2-user/input/<subfolder>/ --max-rows 500000
```

### Partition Assignments (11 instances)

The 3 largest subfolders are split across 2 instances each using `--file-start`/`--file-end`. Smaller subfolders get 1 instance each.

| # | Subfolder | File Range | ~Records | ~Days |
|---|-----------|------------|----------|-------|
| 1 | `Notes_parquet_18_19` | first half | ~47M | ~4 |
| 2 | `Notes_parquet_18_19` | second half | ~46M | ~4 |
| 3 | `Notes_parquet_24` | first half | ~46M | ~4 |
| 4 | `Notes_parquet_24` | second half | ~45M | ~4 |
| 5 | `Notes_parquet_23` first half + `Notes_parquet_15_and_before` | all | ~52M | ~4.5 |
| 6 | `Notes_parquet_23` | second half | ~35M | ~3 |
| 7 | `Notes_parquet_16_17` | first half | ~32M | ~3 |
| 8 | `Notes_parquet_16_17` | second half | ~32M | ~3 |
| 9 | `Notes_parquet_22` | all | ~62M | ~5.5 |
| 10 | `Notes_parquet_21` | all | ~60M | ~5 |
| 11 | `Notes_parquet_20` | all | ~55M | ~5 |

### Running with File Range Splitting

For instances that process only part of a subfolder:

```bash
# Instance 1: first half of 18_19 (files 0-95)
python3.9 process_parquet_aws.py \
    --input-path /home/ec2-user/input/Notes_parquet_18_19/ \
    --output-path /home/ec2-user/output/Notes_parquet_18_19_a/ \
    --workers 30 --philter-config configs/philter_one.json \
    --file-start 0 --file-end 96

# Instance 2: second half of 18_19 (files 96+)
python3.9 process_parquet_aws.py \
    --input-path /home/ec2-user/input/Notes_parquet_18_19/ \
    --output-path /home/ec2-user/output/Notes_parquet_18_19_b/ \
    --workers 30 --philter-config configs/philter_one.json \
    --file-start 96
```

For instances that process a full subfolder (no splitting needed):

```bash
python3.9 process_parquet_aws.py \
    --input-path /home/ec2-user/input/Notes_parquet_22/ \
    --output-path /home/ec2-user/output/Notes_parquet_22/ \
    --workers 30 --philter-config configs/philter_one.json
```

### Checkpoint & Crash Recovery

- Checkpoint saved after **each file** (not every N records)
- On restart, completed files are **skipped automatically**
- Only the current in-progress file is re-processed (~500K records, ~60 min)
- Spot interruptions are handled gracefully — relaunch and resume
- Output parquet files survive instance stop/restart (stored on EBS)

### Data on EC2 Instance

```
/home/ec2-user/
├── input/                                   ← Copied from S3, repartitioned
│   └── Notes_parquet_18_19/
│       ├── chunk_000000.parquet             (~500K records each)
│       ├── chunk_000001.parquet
│       └── ... (191 files for 18_19)
├── output/                                  ← De-identified results
│   └── Notes_parquet_18_19/
│       ├── batch_000001.parquet
│       ├── batch_000002.parquet
│       └── checkpoint.json                  ← Tracks completed files
└── philter-plus-deidentification/           ← Project code
```

### After Processing Completes (~5.5 days)

1. **Refresh AWS credentials** in `~/.aws/credentials` on your local machine
2. **Update credentials on each EC2 instance:**

```bash
scp -i /path/to/key.pem ~/.aws/credentials ec2-user@<worker-ip>:~/.aws/credentials
```

3. **Upload results from each instance to S3:**

```bash
ssh -i /path/to/key.pem ec2-user@<worker-ip> \
    'aws s3 sync /home/ec2-user/output/ s3://bdsp-site-mgb/philter-deidentify/output/'
```

4. **Download all results locally:**

```bash
aws s3 --profile bidmc sync s3://bdsp-site-mgb/philter-deidentify/output/ ./deidentified_output/
```

---

## Monitoring

### SSH into a worker for live progress

```bash
ssh -i /path/to/key.pem ec2-user@<worker-ip>

# Watch the log in real time
tail -f /home/ec2-user/processing.log
```

You'll see output like:
```
--- File 12/191: chunk_000011.parquet ---
  Loaded 500,000 records (2.1 GB)
  Split into 2500 sub-batches of 200 records
  Progress: 1250/2500 sub-batches (50%), 249,531 output, 143.6 rec/sec
  Progress: 2500/2500 sub-batches (100%), 498,945 output, 138.7 rec/sec
  Wrote batch 12: 498945 records to /home/ec2-user/output/.../batch_000012.parquet
  File done in 60.2 min
  Progress: 12/191 files, 6,000,000 records, 135.2 rec/sec
```

### Quick status check (one-liner)

```bash
ssh -i /path/to/key.pem ec2-user@<worker-ip> \
    'ps aux | grep process_parquet | grep -v grep | wc -l && echo "procs" && \
     cat /home/ec2-user/output/*/checkpoint.json 2>/dev/null | python3.9 -c "import sys,json; d=json.load(sys.stdin); print(f\"Files: {len(d[\"completed_files\"])}, Records: {d[\"processed_count\"]:,}\")" && \
     free -g | grep Mem'
```

### Check memory and worker health

```bash
ssh -i /path/to/key.pem ec2-user@<worker-ip> \
    'free -g | grep Mem && ps aux | grep process_parquet | grep -v grep | wc -l && echo "processes"'
```

### Check local output files on instance

```bash
ssh -i /path/to/key.pem ec2-user@<worker-ip> 'du -sh /home/ec2-user/output/* && ls /home/ec2-user/output/*/*.parquet | wc -l && echo "output files"'
```

### Check disk space

```bash
ssh -i /path/to/key.pem ec2-user@<worker-ip> 'df -h /home/ec2-user'
```

---

## Cost Estimate

### Production (11 × c6i.16xlarge Spot)

| Resource | Cost |
|----------|------|
| 11 spot instances × ~45 instance-days total | ~$1,200 |
| EBS storage (200 GB × 11 × 5.5 days) | ~$32 |
| S3 data transfer | ~$20-30 |
| **Total** | **~$1,250** |

Estimated runtime: **~5.5 days** (bottleneck: largest unsplit subfolder at 62M records)

Most instances finish in 3-4 days. Only instances 9-11 (full medium subfolders) run the full 5-5.5 days.

### Spot vs On-Demand

| | Spot | On-Demand |
|---|------|-----------|
| c6i.16xlarge hourly rate | ~$1.10/hr | $2.72/hr |
| 11 instances × 5.5 days | **~$1,200** | **~$3,200** |
| Interruption risk | ~5% (recovered via checkpoint) | None |

### Testing Cost

Single c6i.16xlarge on-demand for 24 hours: ~$65-70 (compute) + EBS

---

## File Reference

| File | Description |
|------|-------------|
| `deploy_production.sh` | **Production deployment** — SCPs project to 4 EC2 instances, starts processing |
| `test_ec2_schema.sh` | **Test script** — runs 500-row end-to-end test on 1 EC2 instance |
| `check_status.sh` | **Monitoring** — checks completion logs, checkpoints, output files |
| `check_deployment_ready.sh` | **Pre-flight checks** — verifies credentials, S3 access, config file |
| `process_parquet_aws.py` | Runs on EC2 — reads local Parquet, de-identifies with 30 workers, writes output. Supports `--file-start`/`--file-end` for splitting subfolders across instances |
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
│   └── Notes_parquet_24/
└── philter-deidentify/                       ← OUTPUT
    ├── assignments/partition_0..10.txt
    ├── config/philter_one.json
    ├── output/                               ← De-identified Parquet files
    └── logs/partition_X_complete.log          ← Completion markers
```
