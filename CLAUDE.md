# Project Context for Claude

## What This Project Does

This project de-identifies clinical notes (free text) stored as Parquet files in AWS S3, using the **Philter** NLP de-identification engine. The goal is to process approximately **512 million medical records** and produce de-identified output back to S3.

---

## Current Goal

Run a full AWS deployment that:
- Reads from `s3://bdsp-site-mgb/I0001_Notes/` (8 subfolders, ~512M records)
- De-identifies each note using Philter (`configs/philter_one.json`)
- Writes de-identified Parquet output to `s3://bdsp-site-mgb/philter-deidentify/output/`
- Completes in ~2.7 days using 8 × c6i.32xlarge Spot EC2 instances

---

## AWS Environment

- **AWS Profile:** `bidmc` (configured locally via `aws configure --profile bidmc`)
- **Region:** `us-east-1`
- **S3 Bucket:** `bdsp-site-mgb` (pre-existing, do not create or delete)
- **No IAM roles** — instances use `bidmc` user credentials written via user-data
- **Reference EC2 instance:** `i-00f062d5c9c4797c5` — network config (subnet, security groups) is copied from this instance at deploy time
- **SSH Key:** `philter-key` / `philter-key.pem` (saved locally, never committed to git)
- **OS:** Windows 11, scripts run via Git Bash

---

## S3 Layout

```
s3://bdsp-site-mgb/
├── I0001_Notes/                          ← INPUT (do not modify)
│   ├── Notes_parquet_15_and_before/      (~17M records)
│   ├── Notes_parquet_16_17/              (~64M records)
│   ├── Notes_parquet_18_19/              (~93M records, bottleneck)
│   ├── Notes_parquet_20/                 (~55M records)
│   ├── Notes_parquet_21/                 (~60M records)
│   ├── Notes_parquet_22/                 (~62M records)
│   ├── Notes_parquet_23/                 (~70M records)
│   └── Notes_parquet_24/                 (~91M records)
└── philter-deidentify/                   ← OUTPUT (created by scripts)
    ├── assignments/partition_0..7.txt    ← Subfolder assignments per instance
    ├── config/philter_one.json           ← Philter config uploaded at deploy
    ├── output/                           ← De-identified Parquet files
    ├── logs/                             ← Completion markers per partition
    └── schema_test.txt                   ← Schema inspection result (from test)
```

---

## Key Files

| File | Purpose |
|------|---------|
| `deploy_aws.sh` | **Main deployment script** — launches 8 EC2 Spot instances |
| `test_ec2_schema.sh` | **Test script** — launches 1 t3.micro to validate S3 access & schema |
| `cleanup_aws.sh` | Terminates instances, deletes `philter-deidentify/` prefix in S3 |
| `check_deployment_ready.sh` | Pre-flight checks (credentials, S3/EC2 access, config file) |
| `process_parquet_aws.py` | Runs on EC2 — reads Parquet from S3, de-identifies, writes output |
| `philter.py` | Philter NLP de-identification engine |
| `keyword_removal.py` | Pre-processing step applied before Philter |
| `configs/philter_one.json` | Philter configuration (filter rules) |

---

## How Deployment Works

### `deploy_aws.sh` steps:
1. Verifies access to `s3://bdsp-site-mgb`
2. Lists 8 subfolders under `I0001_Notes/` and writes assignment files to S3
3. Uploads `configs/philter_one.json` to S3
4. Creates SSH key pair `philter-key` (saves `philter-key.pem` locally)
5. Retrieves `bidmc` AWS credentials from local AWS config
6. Looks up latest Amazon Linux 2 AMI dynamically (`ec2:describe-images`)
7. Copies subnet + security groups from reference instance `i-00f062d5c9c4797c5`
8. Launches 8 × c6i.32xlarge Spot instances with user-data that:
   - Installs Python 3.9, git, pyarrow, pandas, boto3, s3fs
   - Clones this repo (`db_integrated` branch)
   - Writes `bidmc` credentials to `/home/ec2-user/.aws/`
   - Downloads the subfolder assignment for this instance
   - Runs `process_parquet_aws.py` for each assigned subfolder
   - Uploads a completion marker to S3 when done

### `test_ec2_schema.sh` steps:
- Same network setup as deploy (copies from reference instance)
- Launches 1 × t3.micro
- Reads schema from first `.parquet` file in `Notes_parquet_15_and_before/`
- Uploads schema to `s3://bdsp-site-mgb/philter-deidentify/schema_test.txt`
- Shuts itself down automatically

---

## Parquet Schema (Expected)

Columns used by `process_parquet_aws.py`:
- `NoteCSNID` — note identifier
- `DeIDNoteID` — de-identified note name (used as filename key in Philter)
- `NoteTXT` — the free text clinical note to de-identify
- `ShiftedContactYear` — year (passed through to output unchanged)

Output columns: `NoteCSNID`, `NoteTXT` (de-identified), `DeIDNoteID`, `ShiftedContactYear`

---

## Important Technical Decisions

### No IAM Roles
The `bidmc` AWS profile does not have `iam:CreateRole` permissions. Instead, `bidmc` user credentials are retrieved locally and written to each EC2 instance via user-data. This is acceptable for temporary Spot instances.

### Windows Git Bash Compatibility
- User-data scripts are written to `./user-data.sh` and base64-encoded before passing to `--user-data` (Windows AWS CLI cannot use `file:///tmp/` paths)
- Scripts use `base64 -w 0` to avoid line wrapping

### Network Config Copied from Reference Instance
Instead of hardcoding VPC/subnet/security group IDs, both scripts look up the config from `i-00f062d5c9c4797c5` at runtime:
```bash
SUBNET_ID=$(aws ec2 describe-instances --instance-ids i-00f062d5c9c4797c5 \
    --query 'Reservations[0].Instances[0].SubnetId' --output text)
SG_IDS=$(aws ec2 describe-instances --instance-ids i-00f062d5c9c4797c5 \
    --query 'Reservations[0].Instances[0].SecurityGroups[*].GroupId' --output text)
```
Uses `--network-interfaces` (not `--subnet-id` + `--security-group-ids`) so that `AssociatePublicIpAddress=true` works correctly.

### Dynamic AMI Lookup
AMI ID is looked up at runtime (SSM access is blocked for `bidmc`):
```bash
aws ec2 describe-images --owners amazon \
    --filters 'Name=name,Values=amzn2-ami-hvm-*-x86_64-gp2' 'Name=state,Values=available' \
    --query 'sort_by(Images, &CreationDate)[-1].ImageId'
```

### set -e + key pair check
`describe-key-pairs` returns non-zero when key not found. All key pair checks use `|| true` to prevent `set -e` from killing the script prematurely.

---

## Current Status (as of session end)

- [x] `deploy_aws.sh` fully written and debugged
- [x] `test_ec2_schema.sh` fully written and debugged
- [x] `process_parquet_aws.py` written (removes blocking `input()` call for unattended EC2)
- [x] Network config copies from reference instance at runtime
- [x] Public IP assigned via `--network-interfaces AssociatePublicIpAddress=true`
- [ ] `test_ec2_schema.sh` — needs final confirmation that schema uploads to S3 successfully
- [ ] `process_parquet_aws.py` committed to `db_integrated` branch (was untracked)
- [ ] Full deployment (`deploy_aws.sh`) not yet run

---

## Next Steps

1. Confirm `test_ec2_schema.sh` succeeds (schema file appears at `s3://bdsp-site-mgb/philter-deidentify/schema_test.txt`)
2. Download and verify schema: `aws s3 cp s3://bdsp-site-mgb/philter-deidentify/schema_test.txt ./parquet_schema.txt --profile bidmc`
3. Run full deployment: `./deploy_aws.sh`
4. Monitor: `aws s3 ls s3://bdsp-site-mgb/philter-deidentify/logs/ --profile bidmc`
5. Download results: `aws s3 sync s3://bdsp-site-mgb/philter-deidentify/output/ ./deidentified_output/ --profile bidmc`
6. Cleanup: `./cleanup_aws.sh`

---

## Running the Scripts

All scripts must be run from the project root in **Git Bash** (not PowerShell or CMD):

```bash
# Pre-flight check
./check_deployment_ready.sh

# Test (cheap, ~$0.001)
./test_ec2_schema.sh

# Full deployment (~$600, ~2.7 days)
./deploy_aws.sh

# Cleanup
./cleanup_aws.sh
```

---

## Cost Estimate

| Resource | Cost |
|----------|------|
| 8 × c6i.32xlarge Spot (~$2.50/hr each) | ~$480 |
| S3 storage + transfer | ~$20–50 |
| **Total** | **~$500–600** |
