"""
Generate a stats CSV by comparing input and output parquet files.

For each record in the input, determines whether it was de-identified
in the output or not.

Output CSV columns:
  NoteCSNID       - clinical note identifier from input
  de_id_filename  - note filename key used during de-identification
  status          - "deidentified" or "not_deidentified"

Usage:
    # Local paths
    python generate_stats.py \
        --input-path ./input/ \
        --output-path ./output/ \
        --stats-file ./stats.csv

    # S3 paths
    python generate_stats.py \
        --input-path s3://bdsp-site-mgb/I0001_Notes/Notes_parquet_20/ \
        --output-path s3://bdsp-site-mgb/philter-deidentify/output/partition_7/ \
        --stats-file ./stats.csv
"""

import pyarrow.parquet as pq
import pyarrow as pa
import pandas as pd
import argparse
import csv
import os
import glob
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


def list_parquet_files(path):
    """List all parquet files in a local or S3 path."""
    if path.startswith("s3://"):
        import s3fs
        fs = s3fs.S3FileSystem()
        files = sorted([f"s3://{f}" for f in fs.glob(f"{path.rstrip('/')}/*.parquet")])
    else:
        files = sorted(glob.glob(os.path.join(path, "*.parquet")))
    return files


def read_parquet_columns(filepath, columns):
    """Read specific columns from a parquet file (local or S3)."""
    if filepath.startswith("s3://"):
        import s3fs
        fs = s3fs.S3FileSystem()
        with fs.open(filepath) as f:
            return pq.read_table(f, columns=columns).to_pandas()
    else:
        return pq.read_table(filepath, columns=columns).to_pandas()


def build_output_set(output_path):
    """
    Read all output parquet files and return the set of de_id_filename values
    that were successfully written to output.
    """
    output_files = list_parquet_files(output_path)
    if not output_files:
        logger.warning(f"No output parquet files found in {output_path}")
        return set()

    logger.info(f"Reading {len(output_files)} output file(s) to build de-identified set...")
    deid_keys = set()

    for i, f in enumerate(output_files):
        df = read_parquet_columns(f, ["de_id_filename"])
        deid_keys.update(df["de_id_filename"].dropna().astype(str).tolist())
        logger.info(f"  [{i+1}/{len(output_files)}] {os.path.basename(f)}: "
                    f"{len(df):,} records — cumulative set size: {len(deid_keys):,}")
        del df

    logger.info(f"Total unique de_id_filename in output: {len(deid_keys):,}")
    return deid_keys


def generate_stats(input_path, output_path, stats_file):
    """
    Compare input vs output and write stats CSV.
    Streams input files one at a time to stay memory-safe.
    """
    # Step 1: Build set of de-identified keys from output
    deid_keys = build_output_set(output_path)

    # Step 2: Stream input files and write stats
    input_files = list_parquet_files(input_path)
    if not input_files:
        logger.error(f"No input parquet files found in {input_path}")
        return

    logger.info(f"\nProcessing {len(input_files)} input file(s)...")

    total_records = 0
    total_deidentified = 0
    total_not_deidentified = 0

    with open(stats_file, "w", newline="") as csvf:
        writer = csv.writer(csvf)
        writer.writerow(["NoteCSNID", "de_id_filename", "status"])

        for i, f in enumerate(input_files):
            logger.info(f"  [{i+1}/{len(input_files)}] {os.path.basename(f)}")
            df = read_parquet_columns(f, ["NoteCSNID", "de_id_filename"])

            rows = []
            for _, row in df.iterrows():
                note_csn_id = row["NoteCSNID"]
                de_id_fn = str(row["de_id_filename"]) if pd.notna(row["de_id_filename"]) else ""
                status = "deidentified" if de_id_fn in deid_keys else "not_deidentified"
                rows.append((note_csn_id, de_id_fn, status))

            writer.writerows(rows)

            file_deid = sum(1 for _, _, s in rows if s == "deidentified")
            file_not = len(rows) - file_deid
            total_deidentified += file_deid
            total_not_deidentified += file_not
            total_records += len(rows)

            logger.info(f"    {len(rows):,} records — deidentified: {file_deid:,}, "
                        f"not_deidentified: {file_not:,}")
            del df

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("STATS SUMMARY")
    logger.info("=" * 60)
    logger.info(f"Total records:        {total_records:,}")
    logger.info(f"Deidentified:         {total_deidentified:,} "
                f"({100*total_deidentified/total_records:.1f}%)")
    logger.info(f"Not deidentified:     {total_not_deidentified:,} "
                f"({100*total_not_deidentified/total_records:.1f}%)")
    logger.info(f"Stats file written:   {stats_file}")
    logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Generate stats CSV from input/output parquet files")
    parser.add_argument("--input-path",  required=True, help="S3 or local path to input parquet files")
    parser.add_argument("--output-path", required=True, help="S3 or local path to output parquet files")
    parser.add_argument("--stats-file",  required=True, help="Local path for the output stats CSV")
    args = parser.parse_args()

    start = datetime.now()
    generate_stats(args.input_path, args.output_path, args.stats_file)
    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"Done in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
