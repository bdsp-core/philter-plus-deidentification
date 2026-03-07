#!/usr/bin/env python3
"""
Repartition clinical notes parquet files from S3 into uniform ~500K-row files.

Streams one input file at a time — never holds more than ~2 files in memory.
Writes output files to a flat folder in S3.

Usage:
    python3 repartition_s3.py

Input:  s3://bdsp-site-mgb/I0001_ClinicalNotes_Missed/
Output: s3://bdsp-site-mgb/I0001_ClinicalNotes_Missed_200/
"""

import sys
import time
from datetime import datetime

import pyarrow as pa
import pyarrow.parquet as pq
import s3fs

# ============================================================================
# CONFIGURATION
# ============================================================================

INPUT_PATH   = "s3://bdsp-site-mgb/I0001_ClinicalNotes_Missed/"
OUTPUT_PATH  = "s3://bdsp-site-mgb/I0001_ClinicalNotes_Missed_200/"
ROWS_PER_FILE = 500_000   # target rows per output file

# ============================================================================

def log(msg):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


def main():
    fs = s3fs.S3FileSystem(anon=False)

    # ---- Discover input files ----
    bucket_key = INPUT_PATH.replace("s3://", "").rstrip("/")
    raw = fs.glob(f"{bucket_key}/**/*.parquet") + fs.glob(f"{bucket_key}/*.parquet")
    input_files = sorted(set(f"s3://{f}" for f in raw))

    if not input_files:
        log(f"ERROR: No parquet files found at {INPUT_PATH}")
        sys.exit(1)

    log(f"Found {len(input_files)} input files in {INPUT_PATH}")

    # Quick row count estimate from metadata (fast, no data read)
    log("Counting total rows from file metadata...")
    total_rows_estimate = 0
    for f in input_files:
        try:
            meta = pq.read_metadata(f, filesystem=fs)
            total_rows_estimate += meta.num_rows
        except Exception:
            pass  # some files may lack metadata
    expected_output_files = (total_rows_estimate + ROWS_PER_FILE - 1) // ROWS_PER_FILE
    log(f"Total rows (estimate): {total_rows_estimate:,}")
    log(f"Expected output files: {expected_output_files} (at {ROWS_PER_FILE:,} rows each)")
    log(f"Output path: {OUTPUT_PATH}")
    log("")

    # ---- Stream through input files, write output files ----
    buffer_tables = []
    buffer_rows   = 0
    out_file_num  = 0
    total_read    = 0
    start_time    = time.time()

    for file_idx, input_file in enumerate(input_files):
        file_start = time.time()
        log(f"[{file_idx+1}/{len(input_files)}] Reading: {input_file.split('/')[-1]}")

        try:
            table = pq.read_table(input_file, filesystem=fs)
        except Exception as e:
            log(f"  ERROR reading {input_file}: {e} — skipping")
            continue

        rows_in_file = len(table)
        total_read  += rows_in_file
        buffer_tables.append(table)
        buffer_rows  += rows_in_file

        elapsed_file = time.time() - file_start
        log(f"  {rows_in_file:,} rows read in {elapsed_file:.1f}s  |  buffer: {buffer_rows:,} rows")

        # Write output files whenever buffer exceeds ROWS_PER_FILE
        while buffer_rows >= ROWS_PER_FILE:
            combined = pa.concat_tables(buffer_tables)
            buffer_tables = []

            chunk    = combined.slice(0, ROWS_PER_FILE)
            leftover = combined.slice(ROWS_PER_FILE)

            out_file_num += 1
            out_path = f"{OUTPUT_PATH}part-{out_file_num:05d}.snappy.parquet"

            write_start = time.time()
            pq.write_table(chunk, out_path, compression='snappy', filesystem=fs)
            write_elapsed = time.time() - write_start

            log(f"  → Wrote output file {out_file_num:3d}: {len(chunk):,} rows "
                f"in {write_elapsed:.1f}s  [{out_path.split('/')[-1]}]")

            if len(leftover) > 0:
                buffer_tables = [leftover]
                buffer_rows   = len(leftover)
            else:
                buffer_rows = 0

        # Progress
        elapsed_total = time.time() - start_time
        rate = total_read / elapsed_total if elapsed_total > 0 else 0
        remaining = total_rows_estimate - total_read
        eta_sec   = remaining / rate if rate > 0 else 0
        log(f"  Progress: {total_read:,}/{total_rows_estimate:,} rows "
            f"({100*total_read/total_rows_estimate:.1f}%)  "
            f"rate: {rate/1000:.0f}K rows/s  ETA: {eta_sec/60:.0f} min")
        log("")

    # ---- Flush remaining buffer ----
    if buffer_tables and buffer_rows > 0:
        combined = pa.concat_tables(buffer_tables)
        out_file_num += 1
        out_path = f"{OUTPUT_PATH}part-{out_file_num:05d}.snappy.parquet"
        pq.write_table(combined, out_path, compression='snappy', filesystem=fs)
        log(f"  → Wrote final output file {out_file_num}: {len(combined):,} rows  [{out_path.split('/')[-1]}]")

    # ---- Summary ----
    elapsed_total = time.time() - start_time
    log("")
    log("=" * 60)
    log("REPARTITION COMPLETE")
    log("=" * 60)
    log(f"  Input files:        {len(input_files)}")
    log(f"  Output files:       {out_file_num}")
    log(f"  Total rows read:    {total_read:,}")
    log(f"  Rows per output:    ~{total_read // out_file_num if out_file_num else 0:,}")
    log(f"  Total time:         {elapsed_total/60:.1f} min")
    log(f"  Output path:        {OUTPUT_PATH}")
    log("=" * 60)


if __name__ == "__main__":
    main()
