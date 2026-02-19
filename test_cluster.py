"""
Cluster test script — validates the full pipeline on a small sample.

Reads first parquet file from scratch, prints schema,
runs de-identification on first 100 rows, writes output.

Usage:
    python3 test_cluster.py --scratch-dir /scratch/$USER
"""
import argparse
import os
import time
from datetime import datetime
import pyarrow.parquet as pq
import pyarrow as pa
import pandas as pd
from multiprocessing import Pool

# Add project root to path so philter and keyword_removal are importable
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from philter import Philter
import keyword_removal

SUBFOLDERS = [
    "Notes_parquet_15_and_before",
    "Notes_parquet_16_17",
    "Notes_parquet_18_19",
    "Notes_parquet_20",
    "Notes_parquet_21",
    "Notes_parquet_22",
    "Notes_parquet_23",
    "Notes_parquet_24",
]

REQUIRED_COLS = ['NoteCSNID', 'DeIDNoteID', 'NoteTXT', 'ShiftedContactYear']


def find_first_parquet(input_base):
    """Find first parquet file in any subfolder."""
    for sf in SUBFOLDERS:
        sf_path = os.path.join(input_base, sf)
        if not os.path.isdir(sf_path):
            continue
        for fname in sorted(os.listdir(sf_path)):
            if fname.endswith('.parquet'):
                return os.path.join(sf_path, fname), sf
    return None, None


def print_schema(filepath):
    """Print parquet file schema and row counts."""
    pf = pq.ParquetFile(filepath)
    schema = pf.schema_arrow
    meta = pf.metadata

    print("=" * 60)
    print(f"File: {os.path.basename(filepath)}")
    print("=" * 60)
    print("\nSCHEMA:")
    print("-" * 40)
    for field in schema:
        print(f"  {field.name}: {field.type}")
    print(f"\nFILE INFO:")
    print("-" * 40)
    print(f"  Row groups:    {meta.num_row_groups}")
    print(f"  Total rows:    {meta.num_rows:,}")
    print(f"  Total columns: {meta.num_columns}")
    print()


def run_mini_deid(df_sample, philter_config, workers=4):
    """Run de-identification on a small sample dataframe."""
    from process_parquet_aws import _init_worker, _process_batch

    print(f"Running de-identification on {len(df_sample)} rows with {workers} workers...")

    batch_data = [
        (row['NoteCSNID'], row['DeIDNoteID'], row['NoteTXT'], row['ShiftedContactYear'])
        for _, row in df_sample.iterrows()
    ]

    worker_batch_size = max(1, len(batch_data) // workers)
    worker_batches = [
        batch_data[i:i+worker_batch_size]
        for i in range(0, len(batch_data), worker_batch_size)
    ]

    t0 = time.time()
    with Pool(processes=workers, initializer=_init_worker, initargs=(philter_config,)) as pool:
        results = pool.map(_process_batch, worker_batches)
    elapsed = time.time() - t0

    flat = [r for batch in results for r in batch]
    rate = len(flat) / elapsed if elapsed > 0 else 0

    print(f"  Done: {len(flat)} records in {elapsed:.1f}s ({rate:.1f} rec/sec)")
    return flat, elapsed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--scratch-dir', default=f"/scratch/{os.environ.get('USER', 'user')}")
    parser.add_argument('--sample-size', type=int, default=100)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--philter-config', default='configs/philter_one.json')
    args = parser.parse_args()

    input_base = os.path.join(args.scratch_dir, 'I0001_Notes')
    output_dir = os.path.join(args.scratch_dir, 'philter-deidentify', 'test_output')

    print("=" * 60)
    print("Philter Cluster Test")
    print("=" * 60)
    print(f"Date:         {datetime.now()}")
    print(f"Scratch:      {args.scratch_dir}")
    print(f"Input base:   {input_base}")
    print(f"Output dir:   {output_dir}")
    print(f"Sample size:  {args.sample_size} rows")
    print(f"Workers:      {args.workers}")
    print(f"Philter cfg:  {args.philter_config}")
    print()

    # -------------------------------------------------------------------------
    # Check 1: Data exists
    # -------------------------------------------------------------------------
    print("CHECK 1: Input data...")
    if not os.path.isdir(input_base):
        print(f"  FAIL: {input_base} does not exist")
        sys.exit(1)

    parquet_file, subfolder = find_first_parquet(input_base)
    if not parquet_file:
        print(f"  FAIL: No parquet files found under {input_base}")
        sys.exit(1)
    print(f"  OK: Found parquet file in {subfolder}")
    print(f"      {parquet_file}")
    print()

    # -------------------------------------------------------------------------
    # Check 2: Schema
    # -------------------------------------------------------------------------
    print("CHECK 2: Parquet schema...")
    print_schema(parquet_file)

    # -------------------------------------------------------------------------
    # Check 3: Required columns
    # -------------------------------------------------------------------------
    print("CHECK 3: Required columns...")
    pf = pq.ParquetFile(parquet_file)
    actual_cols = [f.name for f in pf.schema_arrow]
    missing = [c for c in REQUIRED_COLS if c not in actual_cols]
    if missing:
        print(f"  FAIL: Missing columns: {missing}")
        sys.exit(1)
    print(f"  OK: All required columns present: {REQUIRED_COLS}")
    print()

    # -------------------------------------------------------------------------
    # Check 4: Read sample
    # -------------------------------------------------------------------------
    print(f"CHECK 4: Reading {args.sample_size} rows...")
    t0 = time.time()
    df = pq.read_table(parquet_file, columns=REQUIRED_COLS).to_pandas()
    df_sample = df.head(args.sample_size)
    elapsed = time.time() - t0
    print(f"  OK: Read {len(df_sample)} rows in {elapsed:.2f}s")
    print(f"  Sample NoteTXT length: {df_sample['NoteTXT'].str.len().describe().to_dict()}")
    print()

    # -------------------------------------------------------------------------
    # Check 5: Philter config
    # -------------------------------------------------------------------------
    print("CHECK 5: Philter config...")
    if not os.path.isfile(args.philter_config):
        print(f"  FAIL: {args.philter_config} not found")
        sys.exit(1)
    print(f"  OK: {args.philter_config}")
    print()

    # -------------------------------------------------------------------------
    # Check 6: De-identification
    # -------------------------------------------------------------------------
    print("CHECK 6: De-identification...")
    results, elapsed = run_mini_deid(df_sample, args.philter_config, args.workers)
    if not results:
        print("  FAIL: No records returned from de-identification")
        sys.exit(1)

    # Show a before/after sample
    sample_in = df_sample.iloc[0]['NoteTXT'][:200] if len(df_sample) > 0 else ""
    sample_out = results[0][1][:200] if results else ""
    print()
    print("  Sample input  (first 200 chars):")
    print(f"    {repr(sample_in)}")
    print("  Sample output (first 200 chars):")
    print(f"    {repr(sample_out)}")
    print()

    # -------------------------------------------------------------------------
    # Check 7: Write output
    # -------------------------------------------------------------------------
    print("CHECK 7: Writing output...")
    os.makedirs(output_dir, exist_ok=True)
    out_file = os.path.join(output_dir, 'test_batch.parquet')
    df_out = pd.DataFrame(results, columns=['NoteCSNID', 'NoteTXT', 'DeIDNoteID', 'ShiftedContactYear'])
    table = pa.Table.from_pandas(df_out)
    pq.write_table(table, out_file, compression='snappy')
    size_kb = os.path.getsize(out_file) / 1024
    print(f"  OK: Written {len(df_out)} rows → {out_file} ({size_kb:.1f} KB)")
    print()

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    projected_rate = len(results) / elapsed if elapsed > 0 else 0
    total_records = 512_000_000
    projected_hours = total_records / (projected_rate * 8) / 3600 if projected_rate > 0 else 0

    print("=" * 60)
    print("ALL CHECKS PASSED")
    print("=" * 60)
    print(f"  De-id rate (this node, {args.workers} workers): {projected_rate:.1f} rec/sec")
    print(f"  Projected full run (8 nodes in parallel):       {projected_hours:.1f} hours")
    print()
    print("Ready to run full job:")
    print("  ./submit_slurm.sh")
    print("=" * 60)


if __name__ == '__main__':
    main()
