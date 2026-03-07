"""
Generate a stats CSV by comparing input and output parquet files.

For each record in the input, determines whether it was de-identified
in the output or not.

Output CSV columns:
  NoteCSNID       - clinical note identifier from input
  de_id_filename  - note filename key used during de-identification
  status          - "deidentified" or "not_deidentified"

Usage:
    # Single output partition
    python generate_stats.py \
        --input-path  s3://bucket/I0001_Notes/Notes_parquet_20/ \
        --output-paths s3://bucket/output/partition_7/ \
        --stats-file  ./stats.csv

    # Multiple output partitions for one input subfolder (comma-separated)
    python generate_stats.py \
        --input-path  s3://bucket/I0001_Notes/Notes_parquet_18_19/ \
        --output-paths s3://bucket/output/partition_3/,s3://bucket/output/partition_4/,s3://bucket/output/partition_5/,s3://bucket/output/partition_6/ \
        --stats-file  ./stats.csv

    # Append mode (for combining multiple subfolders into one file)
    python generate_stats.py \
        --input-path  s3://bucket/I0001_Notes/Notes_parquet_16_17/ \
        --output-paths s3://bucket/output/partition_1/,s3://bucket/output/partition_2/ \
        --stats-file  ./stats_all.csv \
        --append
"""

import pyarrow.parquet as pq
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


def build_output_set(output_paths):
    """
    Read all output parquet files across one or more output paths.
    Returns a set of de_id_filename values that appear in the output.
    """
    deid_keys = set()
    for output_path in output_paths:
        output_files = list_parquet_files(output_path)
        if not output_files:
            logger.warning(f"No output parquet files found in {output_path}")
            continue
        logger.info(f"  {output_path}: {len(output_files)} file(s)")
        for f in output_files:
            df = read_parquet_columns(f, ["de_id_filename"])
            deid_keys.update(df["de_id_filename"].dropna().astype(str).tolist())
            del df

    logger.info(f"Total unique de_id_filename in output: {len(deid_keys):,}")
    return deid_keys


def generate_stats(input_path, output_paths, stats_file, append=False, id_col='NoteCSNID'):
    """
    Compare input vs output and write stats CSV.
    Streams input files one at a time — memory-safe.
    Uses vectorized pandas ops instead of iterrows for speed.
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"Input:   {input_path}")
    logger.info(f"Outputs: {output_paths}")
    logger.info(f"{'='*60}")

    # Step 1: Build set of de-identified keys from all output paths
    logger.info("Building de-identified key set from output...")
    deid_keys = build_output_set(output_paths)

    # Step 2: Stream input files and write stats
    input_files = list_parquet_files(input_path)
    if not input_files:
        logger.error(f"No input parquet files found in {input_path}")
        return 0, 0, 0

    logger.info(f"\nProcessing {len(input_files)} input file(s)...")

    total_records = 0
    total_deidentified = 0
    total_not_deidentified = 0

    write_mode = "a" if append else "w"
    write_header = not append or not os.path.exists(stats_file)

    with open(stats_file, write_mode, newline="") as csvf:
        writer = csv.writer(csvf)
        if write_header:
            writer.writerow([id_col, "de_id_filename", "status"])

        for i, f in enumerate(input_files):
            logger.info(f"  [{i+1}/{len(input_files)}] {os.path.basename(f)}")
            df = read_parquet_columns(f, [id_col, "de_id_filename"])

            # Vectorized comparison — much faster than iterrows
            df["de_id_filename"] = df["de_id_filename"].fillna("").astype(str)
            df["status"] = df["de_id_filename"].apply(
                lambda x: "deidentified" if x in deid_keys else "not_deidentified"
            )

            writer.writerows(df[[id_col, "de_id_filename", "status"]].values.tolist())

            file_deid = (df["status"] == "deidentified").sum()
            file_not  = (df["status"] == "not_deidentified").sum()
            total_deidentified    += file_deid
            total_not_deidentified += file_not
            total_records         += len(df)

            logger.info(f"    {len(df):,} records — deidentified: {file_deid:,}, "
                        f"not_deidentified: {file_not:,}")
            del df

    logger.info(f"\n  Subfolder summary: {total_records:,} total, "
                f"{total_deidentified:,} deidentified ({100*total_deidentified/max(total_records,1):.1f}%), "
                f"{total_not_deidentified:,} not_deidentified")

    return total_records, total_deidentified, total_not_deidentified


def main():
    parser = argparse.ArgumentParser(description="Generate stats CSV from input/output parquet files")
    parser.add_argument("--input-path",   required=True,
                        help="S3 or local path to input parquet files")
    parser.add_argument("--output-paths", required=True,
                        help="Comma-separated S3 or local paths to output parquet files")
    parser.add_argument("--stats-file",   required=True,
                        help="Local path for the output stats CSV")
    parser.add_argument("--append",       action="store_true",
                        help="Append to stats-file instead of overwriting")
    parser.add_argument("--note-type", choices=['clinicalnotes', 'imagingreport'], default='clinicalnotes',
                        help="Type of notes: clinicalnotes (NoteCSNID) or imagingreport (OrderProcedureID)")
    args = parser.parse_args()

    id_col = 'OrderProcedureID' if args.note_type == 'imagingreport' else 'NoteCSNID'
    output_paths = [p.strip() for p in args.output_paths.split(",")]

    start = datetime.now()
    generate_stats(args.input_path, output_paths, args.stats_file, append=args.append, id_col=id_col)
    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"Done in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
