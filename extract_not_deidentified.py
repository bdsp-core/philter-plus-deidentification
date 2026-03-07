"""
Extract records that were NOT de-identified, by comparing input parquet files
against output parquet files. Writes filtered records to a new S3 location
as repartitioned parquet files (~500K rows each), ready for re-processing.

Usage:
    python extract_not_deidentified.py \
        --input-path  s3://bucket/I0001_Notes/Notes_parquet_21/ \
        --output-paths s3://bucket/output/partition_8/,s3://bucket/output/partition_9/ \
        --rerun-path  s3://bucket/rerun_input/Notes_parquet_21/

    # Dry-run (count only, no writes):
    python extract_not_deidentified.py \
        --input-path  s3://bucket/I0001_Notes/Notes_parquet_21/ \
        --output-paths s3://bucket/output/partition_8/,s3://bucket/output/partition_9/ \
        --rerun-path  s3://bucket/rerun_input/Notes_parquet_21/ \
        --dry-run
"""

import pyarrow.parquet as pq
import pyarrow as pa
import pandas as pd
import argparse
import glob
import os
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

ROWS_PER_FILE = 500_000


def get_fs(path):
    if path.startswith("s3://"):
        import s3fs
        return s3fs.S3FileSystem(), True
    return None, False


def list_parquet_files(path):
    if path.startswith("s3://"):
        import s3fs
        fs = s3fs.S3FileSystem()
        files = sorted([f"s3://{f}" for f in fs.glob(f"{path.rstrip('/')}/*.parquet")])
    else:
        files = sorted(glob.glob(os.path.join(path, "*.parquet")))
    return files


def read_parquet_columns(filepath, columns):
    if filepath.startswith("s3://"):
        import s3fs
        fs = s3fs.S3FileSystem()
        with fs.open(filepath) as f:
            return pq.read_table(f, columns=columns).to_pandas()
    else:
        return pq.read_table(filepath, columns=columns).to_pandas()


def iter_parquet_batches(filepath, batch_size=100_000):
    """Iterate over a parquet file in batches to avoid loading it all into memory."""
    if filepath.startswith("s3://"):
        import s3fs
        fs = s3fs.S3FileSystem()
        with fs.open(filepath) as f:
            pf = pq.ParquetFile(f)
            for batch in pf.iter_batches(batch_size=batch_size):
                yield batch.to_pandas()
    else:
        pf = pq.ParquetFile(filepath)
        for batch in pf.iter_batches(batch_size=batch_size):
            yield batch.to_pandas()


def write_parquet(df, filepath):
    table = pa.Table.from_pandas(df, preserve_index=False)
    if filepath.startswith("s3://"):
        import s3fs
        fs = s3fs.S3FileSystem()
        with fs.open(filepath, "wb") as f:
            pq.write_table(table, f, compression="snappy")
    else:
        pq.write_table(table, filepath, compression="snappy")


def build_deidentified_set(output_paths):
    """Build set of de_id_filename values that are already in the output."""
    deid_keys = set()
    for output_path in output_paths:
        files = list_parquet_files(output_path)
        if not files:
            logger.warning(f"No output files found in {output_path}")
            continue
        logger.info(f"  {output_path}: {len(files)} file(s)")
        for f in files:
            df = read_parquet_columns(f, ["de_id_filename"])
            deid_keys.update(df["de_id_filename"].dropna().astype(str).tolist())
            del df
    logger.info(f"  Total already-deidentified keys: {len(deid_keys):,}")
    return deid_keys


def extract_not_deidentified(input_path, output_paths, rerun_path, dry_run=False):
    logger.info(f"\n{'='*60}")
    logger.info(f"Input:      {input_path}")
    logger.info(f"Outputs:    {output_paths}")
    logger.info(f"Rerun path: {rerun_path}")
    logger.info(f"Dry run:    {dry_run}")
    logger.info(f"{'='*60}")

    # Step 1: Build set of already-deidentified keys
    logger.info("Building already-deidentified key set from output...")
    deid_keys = build_deidentified_set(output_paths)

    # Step 2: Read input, filter to not-deidentified, write in batches
    input_files = list_parquet_files(input_path)
    if not input_files:
        logger.error(f"No input files found in {input_path}")
        return 0

    logger.info(f"\nProcessing {len(input_files)} input file(s)...")

    buffer = []
    buffer_rows = 0
    file_counter = 0
    total_not_deid = 0
    total_records = 0

    for i, f in enumerate(input_files):
        logger.info(f"  [{i+1}/{len(input_files)}] {os.path.basename(f)}")
        file_not_deid = 0

        try:
            # Read in batches to avoid loading full file into memory
            for df in iter_parquet_batches(f):
                total_records += len(df)

                df["de_id_filename"] = df["de_id_filename"].fillna("").astype(str)
                mask = ~df["de_id_filename"].isin(deid_keys)
                not_deid_df = df[mask].copy()
                del df

                count = len(not_deid_df)
                file_not_deid += count
                total_not_deid += count

                if count == 0 or dry_run:
                    del not_deid_df
                    continue

                buffer.append(not_deid_df)
                buffer_rows += count

                # Write when buffer reaches ROWS_PER_FILE
                while buffer_rows >= ROWS_PER_FILE:
                    combined = pd.concat(buffer, ignore_index=True)
                    buffer = []
                    buffer_rows = 0

                    chunk = combined.iloc[:ROWS_PER_FILE]
                    remainder = combined.iloc[ROWS_PER_FILE:]

                    file_counter += 1
                    out_path = f"{rerun_path.rstrip('/')}/part_{file_counter:05d}.parquet"
                    write_parquet(chunk, out_path)
                    logger.info(f"    Wrote {out_path} ({len(chunk):,} rows)")

                    if len(remainder) > 0:
                        buffer.append(remainder)
                        buffer_rows = len(remainder)

                    del combined, chunk, remainder

                del not_deid_df

        except Exception as e:
            logger.error(f"    ERROR reading {os.path.basename(f)}: {e} — skipping file")

        logger.info(f"    {file_not_deid:,} not-deidentified records")

    # Flush remaining buffer
    if buffer and not dry_run:
        combined = pd.concat(buffer, ignore_index=True)
        file_counter += 1
        out_path = f"{rerun_path.rstrip('/')}/part_{file_counter:05d}.parquet"
        write_parquet(combined, out_path)
        logger.info(f"  Wrote final {out_path} ({len(combined):,} rows)")
        del combined

    pct = 100 * total_not_deid / max(total_records, 1)
    logger.info(f"\n  Summary: {total_records:,} input records, "
                f"{total_not_deid:,} not-deidentified ({pct:.1f}%), "
                f"{file_counter} output file(s) written")

    return total_not_deid


def main():
    parser = argparse.ArgumentParser(
        description="Extract not-deidentified records for re-processing")
    parser.add_argument("--input-path",   required=True,
                        help="S3 or local path to input parquet files")
    parser.add_argument("--output-paths", required=True,
                        help="Comma-separated S3 or local paths to de-identified output")
    parser.add_argument("--rerun-path",   required=True,
                        help="S3 or local path to write filtered records")
    parser.add_argument("--dry-run",      action="store_true",
                        help="Count records only, do not write output")
    args = parser.parse_args()

    output_paths = [p.strip() for p in args.output_paths.split(",")]

    start = datetime.now()
    extract_not_deidentified(
        args.input_path, output_paths, args.rerun_path, dry_run=args.dry_run)
    elapsed = (datetime.now() - start).total_seconds()
    logger.info(f"Done in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
