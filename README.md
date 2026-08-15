# philter-plus-deidentification

De-identification for BDSP clinical notes. **Surrogate mode**: PHI is replaced with realistic fakes —
a name becomes a different name, a date shifts by the patient's canonical offset — rather than blacked
out. A residual leak is therefore not self-announcing: a real name left among thousands of plausible
fakes is not identifiable *as* real.

## One branch

**`main` is the only branch.** History before 2026-08-15 is preserved in the tag
`pre-consolidation-2026-08-15`. The `martinos` branch holds Martinos-cluster SLURM config (last
touched 2026-02) and is a different execution environment, not a second copy of the pipeline.

This matters here specifically: eponym put-back rules that had been written once appeared to be
missing because the work sat on a second branch. With a team using this, a second branch is how the
wrong code gets run.

## Before you change anything

```bash
python loom/deid_bench.py    # in db_phi_aurora — 35 synthetic cases, no PHI, runs in seconds
```

Every defect a human found by reading real notes is a case in that bench: `Senna`→`Duggin`,
`AlkPhos`→`Tsui`, `q8hr`→`q8woldeyohannes`, `Oral`→`Olle`, `5mg`→`25mg`, `Thomas splint`→`Bell splint`.
They are cheap to reintroduce and expensive to notice.

## The three name mechanisms

| whose name | mechanism | why |
|---|---|---|
| the **patient** | `known_names`, supplied per note via `--names-path` | NER misses names it does not recognise; measured 8.91% leak without it, ~0.1% with it |
| **providers** | `filters/blacklists/provider_roster.json`, deliberately NOT POS-gated | the surname blacklist only fires on an `NNP` tag, so ALL-CAPS signature blocks survive; 45% of roster names are absent from the 161k census list |
| everyone else | NER + census lists | no ground truth available |

`--names-path` is not optional for a real run. Without it philter silently reverts to NER-only names.

## Deployment

Do not hand-build the fleet tarball — use `db_phi_aurora/loom/deploy_fleet.sh`, which refuses to
publish unless this repo is on `main`, clean and pushed, and stamps the commit into the artifact.


---

# Philter De-identification Pipeline

De-identifies clinical notes and imaging reports stored as Parquet files in AWS S3, using the **Philter** NLP engine. Built to process **800+ million medical records** across multiple EC2 instances in parallel — reading directly from S3 and writing back to S3 with no local disk staging.

---

## ⚠️ What this pipeline GUARANTEES (read before running or modifying)

Full detail: **[DEID_METHODOLOGY.md](DEID_METHODOLOGY.md)** — especially *"The whitelist-restore guard"*.
This section exists because that document was missed once and a run silently lost every guarantee below.

| guarantee | mechanism | how to VERIFY it happened |
|---|---|---|
| PHI replaced by realistic **fakes**, not asterisks | `transform_text_surrogate` (`--surrogate`, default on) | output must contain almost no `***`. Any run with >2% is the FALLBACK, not surrogates |
| **Eponyms and clinical terms preserved** (`Parkinson`, `Prader-Willi`, biomarkers, genomic coords) | whitelist-restore guard **inside `transform_text_surrogate`** | those terms appear unchanged in output |
| dates shifted by the patient's **canonical offset** | `--shift-col` / `default_date_shift` | in-text dates move by the same offset as structured data |
| same real name → same fake name everywhere | HMAC-keyed surrogates on `de_id_filename` | one patient's name is consistent across their notes |

### THE FAILURE MODE THAT COSTS YOU A WHOLE RUN

`transform_text_surrogate` is wrapped in a `try/except` that falls back to `transform_text_asterisk`
**and still records the note as "deidentified"**. So ANY error in the surrogate path — a missing corpus, an
unreadable whitelist — silently converts the entire run to asterisk redaction with **no restore guard**,
i.e. no eponym preservation, no clinical-term protection. It still produces plausible output, correct row
counts, and PHI-pattern removal, so row-count and PHI checks all PASS.

This happened: a missing NLTK `names` corpus turned a 62M-note run into asterisk redaction, undetected for
five hours. Guards now in place — do not remove them:
- `_preflight_surrogate()` runs before any data is touched and **exits 3** if surrogates cannot work.
- surrogate→asterisk fallbacks are **counted and logged with the real exception** (`_fallback_count`).
- the Loom QA gate has a `surrogate_not_asterisk` check.

### RUNTIME DATA DEPENDENCIES (not in requirements.txt — they are NOT pip packages)

`requirements.txt` lists `nltk`, but NLTK **corpora** are downloaded data. Missing `names` breaks
surrogates specifically. Install all of these:

```bash
python -m nltk.downloader punkt punkt_tab averaged_perceptron_tagger \
    averaged_perceptron_tagger_eng maxent_ne_chunker maxent_ne_chunker_tab words names
pip install s3fs fsspec          # also missing from requirements.txt; needed for S3 I/O
```

NLTK 3.9 RENAMED resources: `averaged_perceptron_tagger_eng` and `punkt_tab` replace the older names.
Installing only the old names fails at runtime with `map_coordinates failed` — and philter then writes
**zero rows while still reporting a rec/sec figure**.


## Table of Contents

- [Project Status](#project-status)
- [How It Works](#how-it-works)
- [Input / Output Schema](#input--output-schema)
- [S3 Layout](#s3-layout)
- [Prerequisites](#prerequisites)
- [Step-by-Step Manual Execution Guide](#step-by-step-manual-execution-guide)
  - [1. Create EC2 Instances](#1-create-ec2-instances)
  - [2. Configure IAM Role](#2-configure-iam-role)
  - [3. Deploy and Run](#3-deploy-and-run)
  - [4. Monitor Progress](#4-monitor-progress)
  - [5. Verify and Generate Stats](#5-verify-and-generate-stats)
- [Deploy Scripts Reference](#deploy-scripts-reference)
- [Architecture: Key Design Decisions](#architecture-key-design-decisions)
- [Monitoring Reference](#monitoring-reference)
- [Cost Estimates](#cost-estimates)
- [File Reference](#file-reference)
- [Local Setup](#local-setup)

---

## Project Status

| Dataset | Records | Status | Output Path |
|---------|---------|--------|-------------|
| Clinical Notes (original) | ~512M | **Done** (~74.6M missed due to SSO expiry) | `philter-deidentify/output/` |
| Imaging Reports | ~47.6M | **Complete** | `philter-deidentify/imaging_output/partition_{0-19}/` |
| Missed Clinical Notes | ~108.8M | **Complete** | `philter-deidentify/missed_notes_output/partition_{0-19}/` |
| BI Clinical Notes | ~197.8M | **In progress** | `philter-deidentify/bi_clinical_notes_output/partition_{0-19}/` |

---

## How It Works

The de-identification pipeline applies two stages to each note:

1. **Keyword Removal** (`keyword_removal.py`) — Regex-based removal of ~250 site-specific identifiers (hospital names, addresses, MRNs, abbreviations). Replaces matches with `*****`.
2. **Philter NLP** (`philter.py`) — 330 regex patterns + NLTK POS tagging to detect and redact PHI (patient names, dates, phone numbers, SSNs, locations, etc.). Config from `configs/philter_one.json`.

Processing flow:
```
S3 Parquet Input
  → Read in batches
  → keyword_removal (regex PHI scrub)
  → Philter NLP (330 patterns + NER)
  → Write de-identified Parquet back to S3
```

Each EC2 instance runs **60 parallel workers** via `multiprocessing.Pool` with `imap_unordered`. A **watchdog script** wraps the Python process and auto-restarts it on crash. Checkpoints are saved to S3 after each file for crash-safe resume.

---

## Input / Output Schema

### Clinical Notes (`--note-type clinicalnotes`)

**Input columns (from S3):**

| Column | Type | Description |
|--------|------|-------------|
| `NoteCSNID` | string | Note CSN identifier |
| `BDSPPatientID` | int32 | BDSP patient identifier |
| `bdsp_encounter_id` | int64 | BDSP encounter identifier |
| `ShiftedContactDate` | string | Date-shifted contact date |
| `NoteTXT` | string | **Clinical note text (to be de-identified)** |
| `de_id_filename` | string | Filename key used by Philter |

**Output columns:**

| Column | Type | Description |
|--------|------|-------------|
| `BDSPPatientID` | int32 | BDSP patient identifier |
| `bdsp_encounter_id` | int64 | BDSP encounter identifier |
| `ShiftedContactDate` | string | Date-shifted contact date |
| `NoteTXT` | string | **De-identified clinical note text** |
| `de_id_filename` | string | Filename key |

### Imaging Reports (`--note-type imagingreport`)

**Input columns (from S3):**

| Column | Type | Description |
|--------|------|-------------|
| `OrderProcedureID` | decimal128 | Order procedure identifier |
| `BDSPPatientID` | int32 | BDSP patient identifier |
| `bdsp_encounter_id` | int64 | BDSP encounter identifier |
| `ShiftedContactDate` | string | Date-shifted contact date |
| `ReportTXT` | string | **Imaging report text (to be de-identified)** |
| `de_id_filename` | string | Filename key used by Philter |

**Output columns:**

| Column | Type | Description |
|--------|------|-------------|
| `BDSPPatientID` | int32 | BDSP patient identifier |
| `bdsp_encounter_id` | int64 | BDSP encounter identifier |
| `ShiftedContactDate` | string | Date-shifted contact date |
| `ReportTXT` | string | **De-identified imaging report text** |
| `de_id_filename` | string | Filename key |

**Status CSV columns:** `OrderProcedureID`, `de_id_filename`, `status`

### BI Clinical Notes (`--note-type bi_clinicalnotes`)

**Input columns (from S3):**

| Column | Type | Description |
|--------|------|-------------|
| `CLINICALNOTETEXTKEY` | string | Clinical note text key (ID) |
| `TYPE` | string | Note type |
| `SHIFTED_CREATIONINSTANT` | timestamp[ns] | Date-shifted creation timestamp |
| `TEXT` | string | **Clinical note text (to be de-identified)** |
| `COUNT` | decimal128(38,0) | Record count |
| `DeidentifiedName` | string | Filename key used by Philter |

**Output columns:**

| Column | Type | Description |
|--------|------|-------------|
| `DeidentifiedName` | string | Filename key |
| `TEXT` | string | **De-identified clinical note text** |
| `TYPE` | string | Note type |
| `CREATIONINSTANT` | timestamp[ns] | Creation timestamp (renamed from `SHIFTED_CREATIONINSTANT`) |
| `COUNT` | decimal128(38,0) | Record count |

**Status CSV columns:** `CLINICALNOTETEXTKEY`, `DeidentifiedName`, `status`

---

## S3 Layout

```
s3://bdsp-site-mgb/
├── I0001_Notes/                                    ← INPUT: original clinical notes (8 subfolders)
│   ├── Notes_parquet_15_and_before/
│   ├── Notes_parquet_16_17/
│   ├── Notes_parquet_18_19/
│   ├── Notes_parquet_20/
│   ├── Notes_parquet_21/
│   ├── Notes_parquet_22/
│   ├── Notes_parquet_23/
│   └── Notes_parquet_24/
├── I0001_ImagingReports/                           ← INPUT: imaging reports (96 files flat)
├── I0001_ClinicalNotes_Missed/                     ← INPUT: missed clinical notes (200 files flat)
├── bi_clinical_notes/                              ← INPUT: BI clinical notes (20 files flat, ~197.8M records)
└── philter-deidentify/
    ├── config/philter_one.json                     ← Philter config (uploaded by deploy scripts)
    ├── output/                                     ← Clinical notes output (original run)
    ├── imaging_output/
    │   ├── partition_0/                            ← Imaging report output (20 partitions)
    │   │   ├── batch_000001.parquet
    │   │   └── checkpoint.json
    │   ├── partition_1/ ... partition_19/
    │   └── logs/partition_X_complete.log           ← Completion markers
    ├── missed_notes_output/
    │   ├── partition_0/                            ← Missed notes output (20 partitions)
    │   │   ├── batch_000001.parquet
    │   │   └── checkpoint.json
    │   ├── partition_1/ ... partition_19/
    │   └── logs/partition_X_complete.log
    └── bi_clinical_notes_output/
        ├── partition_0/                            ← BI clinical notes output (20 partitions)
        │   ├── batch_000001.parquet
        │   └── checkpoint.json
        ├── partition_1/ ... partition_19/
        └── logs/partition_X_complete.log
```

---

## Prerequisites

### AWS Setup

1. **AWS CLI** installed and configured locally with the `bidmc` profile:
   ```bash
   aws configure --profile bidmc
   # Enter: Access Key ID, Secret Access Key, region=us-east-1, output=json
   ```

2. **`bidmc` profile** must have permissions to:
   - `ec2:DescribeInstances` (to get instance IPs)
   - `s3:GetObject` / `s3:PutObject` on `s3://bdsp-site-mgb/`

3. **SSH key** (`philter.pem`) with `chmod 400`:
   ```bash
   chmod 400 /Users/anjanarayapureddy/Desktop/Philter/philter.pem
   ```

4. **Security group** on instances must allow **SSH (port 22)** from your IP:
   ```bash
   MY_IP=$(curl -s https://checkip.amazonaws.com)
   aws ec2 authorize-security-group-ingress \
       --profile bidmc --region us-east-1 \
       --group-id <SG_ID> \
       --protocol tcp --port 22 --cidr ${MY_IP}/32
   ```

### Local Dependencies (for stats/verification only)

```bash
pip install boto3 pandas pyarrow s3fs
```

---

## Step-by-Step Manual Execution Guide

This section explains how to run the pipeline from scratch without any automation tooling.

### 1. Create EC2 Instances

In the **AWS Console** (or via AWS CLI), create N instances with these settings:

| Setting | Value |
|---------|-------|
| AMI | Amazon Linux 2 (x86_64, `amzn2-ami-hvm-*`) |
| Instance type | `c6i.16xlarge` (64 vCPU, 128 GB RAM) |
| Pricing | **Spot** (~$0.68/hr, significant savings) |
| Storage | 50 GB gp3 (no local data needed — S3-direct) |
| IAM Instance Profile | `AmazonSSMRoleForInstancesQuickSetup` ← **critical** |
| Key pair | Your PEM file |
| Auto-assign public IP | Enabled |
| Security group | Must allow SSH (port 22) from your IP |

**Via AWS CLI (example for 20 instances):**
```bash
aws ec2 run-instances \
    --profile bidmc --region us-east-1 \
    --image-id ami-xxxxxxxxxxxxxxxxx \
    --count 20 \
    --instance-type c6i.16xlarge \
    --key-name your-key-name \
    --iam-instance-profile Name=AmazonSSMRoleForInstancesQuickSetup \
    --instance-market-options '{"MarketType":"spot"}' \
    --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":50,"VolumeType":"gp3"}}]' \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=philter-worker}]'
```

Wait for all instances to reach `running` state:
```bash
aws ec2 describe-instances \
    --profile bidmc --region us-east-1 \
    --filters "Name=tag:Name,Values=philter-worker" "Name=instance-state-name,Values=running" \
    --query 'Reservations[*].Instances[*].[InstanceId,PublicIpAddress]' \
    --output text
```

### 2. Configure IAM Role

The IAM instance profile `AmazonSSMRoleForInstancesQuickSetup` must be attached to all instances. This grants boto3 automatic, non-expiring S3 access via the instance metadata service (IMDS). **No credentials file is needed or wanted on the instance.**

> **Why this matters**: Previous runs used SSO credentials written to `~/.aws/credentials` on each instance. These expired after 12 hours, silently causing all S3 writes to fail mid-run. The IAM role has no expiry.

If the role isn't attached at launch, attach it via the console: *EC2 → Instance → Actions → Security → Modify IAM Role*.

### 3. Deploy and Run

Each deploy script handles everything automatically: packages the code, uploads to S3, SSH's into each instance sequentially, installs dependencies, and starts a watchdog process.

**For a new dataset run, use the appropriate script:**

```bash
# BI clinical notes
./deploy_bi_clinical_notes.sh i-0aaa... i-0bbb... i-0ccc...   # pass all instance IDs

# Missed clinical notes
./deploy_missed_notes.sh i-0aaa... i-0bbb... i-0ccc...

# Imaging reports
./deploy_imaging.sh i-0aaa... i-0bbb...

# Original clinical notes
./deploy_aws.sh i-0aaa... i-0bbb...
```

**What the deploy script does per instance (in order):**

1. Packages the project into a tarball and uploads it to S3 (done once, shared by all instances)
2. SSHs into the instance
3. Removes any stale AWS credentials (`rm -f ~/.aws/credentials`) so the IAM role takes effect
4. Downloads and extracts the project tarball from S3
5. Installs Python dependencies (`pyarrow`, `pandas`, `boto3`, `s3fs`, `nltk`)
6. Writes a `start_watchdog.sh` script that wraps the Python process in a restart loop
7. Starts the watchdog via `setsid` as a detached background process (survives SSH disconnect)

**The watchdog loop (how crash recovery works):**

```bash
while true; do
    python3.9 process_parquet_aws.py \
        --input-path  "s3://bdsp-site-mgb/I0001_ClinicalNotes_Missed/" \
        --output-path "s3://bdsp-site-mgb/philter-deidentify/missed_notes_output/partition_N/" \
        --partition-id N \
        --workers 60 \
        --file-start FILE_START \
        --file-end   FILE_END \
        --note-type  clinicalnotes \
        --philter-config configs/philter_one.json
    EXIT=$?
    [ $EXIT -eq 0 ] && break           # clean exit = done
    echo "$(date): crashed (exit $EXIT), restarting in 10s..."
    sleep 10
done
```

If the process crashes (OOM, spot interruption, network error), the watchdog restarts it automatically. On restart, `process_parquet_aws.py` reads `checkpoint.json` from S3 and skips already-completed files.

**To run manually on a single instance (without the deploy script):**

```bash
# SSH in
ssh -i /path/to/philter.pem ec2-user@<instance-ip>

# Install deps (if not already installed)
sudo yum install -y python39 python39-pip git
pip3.9 install pyarrow pandas boto3 s3fs nltk
python3.9 -c "import nltk; nltk.download('averaged_perceptron_tagger'); nltk.download('averaged_perceptron_tagger_eng'); nltk.download('punkt'); nltk.download('punkt_tab')"

# Clear stale credentials (IAM role must be used instead)
rm -f ~/.aws/credentials

# Clone the repo
git clone -b AWS_Integration https://github.com/your-org/philter-plus-deidentification.git
cd philter-plus-deidentification

# Run directly (no watchdog — you'll need to restart manually on crash)
python3.9 process_parquet_aws.py \
    --input-path  "s3://bdsp-site-mgb/I0001_ClinicalNotes_Missed/" \
    --output-path "s3://bdsp-site-mgb/philter-deidentify/missed_notes_output/partition_0/" \
    --partition-id 0 \
    --workers 60 \
    --file-start 0 \
    --file-end 10 \
    --note-type clinicalnotes \
    --philter-config configs/philter_one.json

# OR: run with watchdog for auto-restart (setsid creates new session, survives SSH disconnect)
setsid bash start_watchdog.sh > ~/deidentify.log 2>&1 &
sleep 3
```

**`process_parquet_aws.py` argument reference:**

| Argument | Description | Example |
|----------|-------------|---------|
| `--input-path` | S3 prefix to read Parquet files from | `s3://bdsp-site-mgb/I0001_ClinicalNotes_Missed/` |
| `--output-path` | S3 prefix to write output Parquet files to | `s3://bdsp-site-mgb/.../partition_0/` |
| `--partition-id` | Integer ID for this partition (used in logs/completion markers) | `0` |
| `--workers` | Number of parallel multiprocessing workers | `60` |
| `--batch-size` | Records per sub-batch sent to workers | `10000` |
| `--file-start` | Index of first file to process (0-based, inclusive) | `0` |
| `--file-end` | Index of last file to process (exclusive) | `10` |
| `--note-type` | `clinicalnotes` (NoteTXT/NoteCSNID), `imagingreport` (ReportTXT/OrderProcedureID), or `bi_clinicalnotes` (TEXT/CLINICALNOTETEXTKEY) | `clinicalnotes` |
| `--philter-config` | Path to Philter JSON config | `configs/philter_one.json` |

### 4. Monitor Progress

**Check if watchdog is running (via SSH):**
```bash
KEY="/Users/anjanarayapureddy/Desktop/Philter/philter.pem"
IP="<instance-ip>"

ssh -o StrictHostKeyChecking=no -i $KEY ec2-user@$IP \
    'ps aux | grep start_watchdog | grep -v grep | wc -l'
# Should print 1
```

**Tail the log for live progress:**
```bash
ssh -i $KEY ec2-user@$IP 'tail -f ~/deidentify.log'
```

Expected log output:
```
--- File 3/10: part-00003.parquet ---
  Loaded 543,812 records
  Split into 55 sub-batches of 10000 records
  Progress: 27/55 sub-batches (49%), 269,443 output, 123.4 rec/sec
  Progress: 55/55 sub-batches (100%), 542,907 output, 121.8 rec/sec
  Wrote batch 3: 542907 records
  File done in 74.5 min
  Progress: 3/10 files, 1,621,432 records, 122.1 rec/sec
```

**Check all 20 instances at once (speed + watchdog count):**
```bash
KEY="/Users/anjanarayapureddy/Desktop/Philter/philter.pem"
declare -A IPS=(
    [0]="<ip0>" [1]="<ip1>" [2]="<ip2>" [3]="<ip3>" [4]="<ip4>"
    # ... add all IPs
)
for i in "${!IPS[@]}"; do
    IP="${IPS[$i]}"
    RESULT=$(ssh -o StrictHostKeyChecking=no -i $KEY ec2-user@$IP \
        "WC=\$(ps aux | grep start_watchdog | grep -v grep | wc -l); \
         RATE=\$(grep 'rec/sec' ~/deidentify.log 2>/dev/null | tail -1 | grep -oP '[0-9]+\.[0-9]+ rec/sec'); \
         echo 'P${i}: watchdogs='\$WC' | '\$RATE" 2>/dev/null)
    echo "$RESULT"
done
```

**Count output files in S3 per partition:**
```bash
for i in $(seq 0 19); do
    COUNT=$(aws s3 --profile bidmc ls \
        s3://bdsp-site-mgb/philter-deidentify/missed_notes_output/partition_${i}/ 2>/dev/null \
        | grep -c parquet || echo 0)
    echo "Partition $i: $COUNT files"
done
```

**Check completion logs (one per partition when done):**
```bash
aws s3 --profile bidmc ls s3://bdsp-site-mgb/philter-deidentify/missed_notes_output/logs/
# When all 20 partitions appear here, the run is complete
```

**Check checkpoint progress for one partition:**
```bash
aws s3 --profile bidmc cp \
    s3://bdsp-site-mgb/philter-deidentify/missed_notes_output/partition_0/checkpoint.json - \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Files done: {len(d[\"completed_files\"])}, Records: {d[\"processed_count\"]:,}')"
```

### 5. Verify and Generate Stats

After all completion logs appear, run `generate_stats.py` locally to verify coverage:

```bash
# For missed clinical notes
python3 generate_stats.py \
    --input-path s3://bdsp-site-mgb/I0001_ClinicalNotes_Missed/ \
    --output-paths s3://bdsp-site-mgb/philter-deidentify/missed_notes_output/partition_0/,partition_1/,...,partition_19/ \
    --stats-file ./missed_notes_stats.csv \
    --note-type clinicalnotes \
    --profile bidmc

# For imaging reports
python3 generate_stats.py \
    --input-path s3://bdsp-site-mgb/I0001_ImagingReports/ \
    --output-paths s3://bdsp-site-mgb/philter-deidentify/imaging_output/partition_0/,...,partition_19/ \
    --stats-file ./imaging_stats.csv \
    --note-type imagingreport \
    --profile bidmc
```

Expected: ≥99% records de-identified, output `ReportTXT`/`NoteTXT` contains `*` characters.

---

## Deploy Scripts Reference

### `deploy_bi_clinical_notes.sh` — BI Clinical Notes

Deploys to N instances for processing `bi_clinical_notes/` (20 files × ~8.67M rows = ~197.8M records).

```bash
./deploy_bi_clinical_notes.sh i-0aaa... i-0bbb... [more instance IDs...]
```

Key config inside the script:
```bash
BUCKET="bdsp-site-mgb"
INPUT_PREFIX="bi_clinical_notes/"
OUTPUT_PREFIX="philter-deidentify/bi_clinical_notes_output"
NUM_WORKERS=60
TOTAL_FILES=20
NOTE_TYPE="bi_clinicalnotes"
```

File range split: 1 file per instance (20 files → 20 instances). Each instance processes `--file-start i --file-end i+1`.

---

### `deploy_missed_notes.sh` — Missed Clinical Notes

Deploys to N instances for processing `I0001_ClinicalNotes_Missed/` (200 files × ~544K rows = ~108.8M records).

```bash
./deploy_missed_notes.sh i-0aaa... i-0bbb... [more instance IDs...]
```

Key config inside the script:
```bash
BUCKET="bdsp-site-mgb"
INPUT_PREFIX="I0001_ClinicalNotes_Missed/"
OUTPUT_PREFIX="philter-deidentify/missed_notes_output"
NUM_WORKERS=60
TOTAL_FILES=200
NOTE_TYPE="clinicalnotes"
```

File range split: `200 files / N instances`, one range per instance. With 20 instances = 10 files each.

---

### `deploy_imaging.sh` — Imaging Reports

Deploys to N instances for processing `I0001_ImagingReports/` (96 files × ~496K rows = ~47.6M records).

```bash
./deploy_imaging.sh i-0aaa... i-0bbb... [more instance IDs...]
```

Key config:
```bash
BUCKET="bdsp-site-mgb"
INPUT_PREFIX="I0001_ImagingReports/"
OUTPUT_PREFIX="philter-deidentify/imaging_output"
NUM_WORKERS=60
TOTAL_FILES=96
NOTE_TYPE="imagingreport"
```

---

### `deploy_aws.sh` — Original Clinical Notes

Deploys to N instances for processing `I0001_Notes/` (8 subfolders, ~512M records).

```bash
./deploy_aws.sh i-0aaa... i-0bbb... [more instance IDs...]
```

Key config:
```bash
BUCKET="bdsp-site-mgb"
INPUT_PREFIX="I0001_Notes/"
OUTPUT_PREFIX="philter-deidentify/output"
NUM_WORKERS=60
NOTE_TYPE="clinicalnotes"
```

This script handles subfolder assignment (each instance processes one or more subfolders).

---

## Architecture: Key Design Decisions

### IAM Instance Role (no credential expiry)

All EC2 instances use the `AmazonSSMRoleForInstancesQuickSetup` IAM instance profile. boto3 automatically picks up credentials from the instance metadata service — no access key file needed, no expiry after 12 hours.

> Previous runs wrote SSO credentials to `~/.aws/credentials` on each instance. These expired silently after 12 hours, causing ~74.6M records to go un-de-identified in the original clinical notes run.

Deploy scripts always run `rm -f ~/.aws/credentials` on each instance to ensure stale credentials don't block IAM role fallback.

### S3-Direct Processing (no local disk staging)

All Parquet files are read directly from S3 using `pyarrow.dataset` and written back to S3 using `pyarrow.parquet.write_table`. No local disk copy is needed, so instance storage can be minimal (50 GB vs 200 GB previously).

### Watchdog Auto-Restart

Every instance runs a watchdog shell loop that restarts the Python process on any crash (OOM, spot interruption, network error). The Python process reads `checkpoint.json` from S3 on startup and skips already-completed files.

```
start_watchdog.sh (background, detached via setsid — survives SSH disconnect)
  └── process_parquet_aws.py (restarted on crash)
        └── checkpoint.json (S3, tracks completed files)
```

### Sequential SSH Deploy (no parallel background jobs)

Deploy scripts SSH into instances one at a time (no `&` backgrounding of deploy steps). This prevents duplicate process launches that were observed when multiple SSH sessions ran in parallel against the same instance.

### Nested Heredoc Variable Escaping

Deploy scripts write watchdog scripts to EC2 instances using heredocs. EC2-runtime variables (e.g., `$PYTHON`, `$(date)`, `$EXIT_CODE`) must be escaped as `\$` so they are not expanded by the local shell at write time. Deploy-time variables (bucket name, partition ID, file range) should NOT be escaped — they should expand at write time on the local machine.

### File-Range Splitting

The `--file-start` and `--file-end` arguments allow multiple instances to process non-overlapping ranges of files from the same flat S3 prefix. The deploy script calculates each instance's range:

```
FILES_PER_INSTANCE = ceil(TOTAL_FILES / NUM_INSTANCES)
Instance N processes files [N * FILES_PER, (N+1) * FILES_PER)
```

---

## Monitoring Reference

### Check all instances — watchdog running + speed
```bash
KEY="/Users/anjanarayapureddy/Desktop/Philter/philter.pem"
for IP in <ip0> <ip1> <ip2> ...; do
    ssh -o StrictHostKeyChecking=no -i $KEY ec2-user@$IP \
        "WC=\$(ps aux | grep start_watchdog | grep -v grep | wc -l); \
         RATE=\$(grep 'rec/sec' ~/deidentify.log | tail -1 | grep -oP '[0-9]+\.[0-9]+ rec/sec'); \
         echo 'watchdogs='\$WC' | '\$RATE" 2>/dev/null
done
```

### Count S3 output files by partition
```bash
OUTPUT="missed_notes_output"   # or imaging_output
for i in $(seq 0 19); do
    COUNT=$(aws s3 --profile bidmc ls \
        s3://bdsp-site-mgb/philter-deidentify/${OUTPUT}/partition_${i}/ 2>/dev/null \
        | grep -c parquet || echo 0)
    echo "Partition $i: $COUNT parquet files"
done
```

### Check completion (one log file per partition when done)
```bash
aws s3 --profile bidmc ls s3://bdsp-site-mgb/philter-deidentify/missed_notes_output/logs/
```

### Read live log from one instance
```bash
ssh -i /path/to/philter.pem ec2-user@<ip> 'tail -50 ~/deidentify.log'
```

### Check memory usage on an instance
```bash
ssh -i /path/to/philter.pem ec2-user@<ip> 'free -g'
# At 60 workers: ~40 GB used out of 128 GB (stable, no growth)
```

### Kill and restart if watchdog is stuck
```bash
ssh -i /path/to/philter.pem ec2-user@<ip> << 'EOF'
# Kill old processes
pkill -f start_watchdog.sh
pkill -f process_parquet_aws.py
sleep 3
# Restart (setsid ensures process survives SSH disconnect)
setsid bash ~/start_watchdog.sh > ~/deidentify.log 2>&1 &
sleep 3
echo "Restarted"
EOF
```

---

## Cost Estimates

### Current Architecture (20 × c6i.16xlarge Spot)

| Resource | Rate | Cost |
|----------|------|------|
| 20 × c6i.16xlarge Spot | ~$0.68/hr each | ~$0.68 × 20 × runtime |
| EBS (50 GB × 20 instances) | $0.08/GB-month | ~$2/day |
| S3 requests + transfer | — | ~$10-20 |

Estimated runtimes at ~90–120 rec/sec per instance:

| Dataset | Records | Instances | Est. Runtime | Est. Cost (on-demand) |
|---------|---------|-----------|--------------|-----------|
| Imaging Reports | 47.6M | 20 | ~1.7 hours | **~$92** |
| Missed Clinical Notes | 108.8M | 20 | ~12 hours | **~$653** |
| BI Clinical Notes | 197.8M | 20 | ~27 hours | **~$1,469** |

### Previous Architecture (for reference)

The original clinical notes run used SSO credentials written to instances (11 × c6i.32xlarge). Due to credential expiry and no watchdog, ~74.6M records were not processed despite running for ~$600.

### Worker Count Guidelines

| Instance Type | vCPUs | RAM | Safe Workers | Memory at Max Workers |
|---------------|-------|-----|--------------|----------------------|
| c6i.16xlarge | 64 | 128 GB | 60 | ~40 GB stable |
| c6i.4xlarge | 16 | 32 GB | 40 | ~25 GB stable |

> Never exceed 1 worker per vCPU × 1.25. OOM was observed at 120 workers on c6i.32xlarge (248 GB RAM).

---

## File Reference

| File | Description |
|------|-------------|
| `deploy_bi_clinical_notes.sh` | Deploy BI clinical notes run to N instances (20 files, 1/instance) |
| `deploy_missed_notes.sh` | Deploy missed clinical notes run to N instances |
| `deploy_imaging.sh` | Deploy imaging reports run to N instances |
| `deploy_aws.sh` | Deploy original clinical notes run to N instances |
| `process_parquet_aws.py` | Main de-id script — reads from S3, de-identifies, writes to S3. Accepts `--note-type clinicalnotes\|imagingreport\|bi_clinicalnotes`, `--file-start`/`--file-end` for range splitting |
| `generate_stats.py` | Post-run verification — compares input record count vs output. Accepts `--note-type` |
| `extract_not_deidentified.py` | Extracts records not present in output (for re-run). Used after original clinical notes run |
| `run_extract.sh` | Shell wrapper to run `extract_not_deidentified.py` |
| `repartition_s3.py` | Stream-repartition large Parquet files in S3 to uniform ~500K-row chunks (not needed for current flat-folder runs) |
| `check_deployment_ready.sh` | Pre-flight checks — verifies AWS credentials, S3 access, config file |
| `cleanup_aws.sh` | Terminates instances and cleans up S3 output prefix |
| `read_parquet.py` | Utility — read and print any Parquet file (local or S3) |
| `philter.py` | Philter NLP de-identification engine |
| `keyword_removal.py` | Site-specific keyword removal (hospital names, MRNs, etc.) |
| `configs/philter_one.json` | Philter configuration (330 filter rules) |

---

## Local Setup

For running stats, verification, or the extract script locally:

```bash
# Create conda environment
conda create -n philter python=3.9 -y
conda activate philter

# Install dependencies
pip install pyarrow pandas boto3 s3fs nltk

# Download NLTK data (required by Philter)
python -c "
import nltk
nltk.download('averaged_perceptron_tagger')
nltk.download('averaged_perceptron_tagger_eng')
nltk.download('punkt')
nltk.download('punkt_tab')
"

# Configure AWS profile
aws configure --profile bidmc

# Read a Parquet file from S3
python read_parquet.py s3://bdsp-site-mgb/philter-deidentify/imaging_output/partition_0/ \
    --profile bidmc --rows 20
```
