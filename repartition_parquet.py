#!/usr/bin/env python3
"""
Repartition large Parquet files into smaller chunks (~500K rows each).
Reads files using iter_batches to avoid loading entire file into memory.
Deletes original file after successful split to save disk space.
"""

import os
import sys
import glob
import time
import pyarrow as pa
import pyarrow.parquet as pq

CHUNK_SIZE = 500_000  # rows per output file
MIN_SIZE_TO_SPLIT = 3 * 1024 * 1024 * 1024  # 3 GB - skip files smaller than this


def repartition_file(filepath):
    """Split a single large parquet file into ~500K-row chunks."""
    file_size = os.path.getsize(filepath)
    file_size_gb = file_size / (1024**3)

    if file_size < MIN_SIZE_TO_SPLIT:
        print(f"  SKIP {os.path.basename(filepath)} ({file_size_gb:.1f} GB) - below {MIN_SIZE_TO_SPLIT/(1024**3):.0f} GB threshold")
        return 0

    print(f"  SPLITTING {os.path.basename(filepath)} ({file_size_gb:.1f} GB)...")
    start = time.time()

    parent_dir = os.path.dirname(filepath)
    basename = os.path.splitext(os.path.basename(filepath))[0]

    pf = pq.ParquetFile(filepath)
    schema = pf.schema_arrow
    total_rows = pf.metadata.num_rows
    print(f"    Total rows: {total_rows:,}")

    chunk_num = 0
    current_rows = []
    current_count = 0
    files_written = 0

    for batch in pf.iter_batches(batch_size=100_000):
        table_batch = pa.Table.from_batches([batch], schema=schema)
        current_rows.append(table_batch)
        current_count += len(batch)

        if current_count >= CHUNK_SIZE:
            # Write chunk
            combined = pa.concat_tables(current_rows)
            out_path = os.path.join(parent_dir, f"{basename}_chunk{chunk_num:04d}.parquet")
            pq.write_table(combined, out_path, compression='snappy')
            out_size = os.path.getsize(out_path) / (1024**3)
            print(f"    Wrote chunk {chunk_num}: {current_count:,} rows ({out_size:.2f} GB)")

            files_written += 1
            chunk_num += 1
            current_rows = []
            current_count = 0

            # Free memory
            del combined

    # Write remaining rows
    if current_rows:
        combined = pa.concat_tables(current_rows)
        out_path = os.path.join(parent_dir, f"{basename}_chunk{chunk_num:04d}.parquet")
        pq.write_table(combined, out_path, compression='snappy')
        out_size = os.path.getsize(out_path) / (1024**3)
        print(f"    Wrote chunk {chunk_num}: {current_count:,} rows ({out_size:.2f} GB)")
        files_written += 1
        del combined

    elapsed = time.time() - start
    print(f"    Done: {files_written} chunks in {elapsed:.0f}s")

    # Delete original file
    os.remove(filepath)
    print(f"    Deleted original: {os.path.basename(filepath)}")

    return files_written


def main():
    input_base = "/home/ec2-user/input"

    if not os.path.exists(input_base):
        print(f"ERROR: {input_base} does not exist")
        sys.exit(1)

    # Find all subdirectories
    subdirs = sorted(glob.glob(os.path.join(input_base, "*/")))

    total_split = 0
    total_skipped = 0
    overall_start = time.time()

    for subdir in subdirs:
        folder_name = os.path.basename(subdir.rstrip('/'))
        parquet_files = sorted(glob.glob(os.path.join(subdir, "*.parquet")))

        print(f"\n{'='*60}")
        print(f"Folder: {folder_name} ({len(parquet_files)} files)")
        print(f"{'='*60}")

        # Process files one at a time (sort by size descending - biggest first)
        parquet_files.sort(key=lambda f: os.path.getsize(f), reverse=True)

        for filepath in parquet_files:
            # Skip files that are already chunks (from a previous run)
            if '_chunk' in os.path.basename(filepath):
                print(f"  SKIP {os.path.basename(filepath)} (already a chunk)")
                total_skipped += 1
                continue

            chunks = repartition_file(filepath)
            if chunks > 0:
                total_split += 1
            else:
                total_skipped += 1

    overall_elapsed = time.time() - overall_start

    # Print final summary
    print(f"\n{'='*60}")
    print(f"REPARTITION COMPLETE")
    print(f"{'='*60}")
    print(f"Files split: {total_split}")
    print(f"Files skipped (small): {total_skipped}")
    print(f"Total time: {overall_elapsed:.0f}s ({overall_elapsed/60:.1f} min)")

    # Show new file counts
    for subdir in subdirs:
        folder_name = os.path.basename(subdir.rstrip('/'))
        files = glob.glob(os.path.join(subdir, "*.parquet"))
        total_size = sum(os.path.getsize(f) for f in files) / (1024**3)
        print(f"  {folder_name}: {len(files)} files, {total_size:.1f} GB total")


if __name__ == "__main__":
    main()
