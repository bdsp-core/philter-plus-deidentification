"""
AWS Parquet Processing Script for Hyper-Fast De-identification.

Designed for:
- Input: Parquet files from S3
- Output: De-identified Parquet files to S3
- Multi-core processing with all optimizations
- Checkpoint-based resume capability

Usage:
    python process_parquet_aws.py \
        --input-path s3://bucket/input/part1/ \
        --output-path s3://bucket/output/part1/ \
        --partition-id 1 \
        --workers 120
"""
import pyarrow.parquet as pq
import pyarrow as pa
import pandas as pd
from philter import Philter
import keyword_removal
import argparse
import logging
from datetime import datetime
from multiprocessing import Pool, cpu_count
import os
import json
import re
import boto3

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Global worker state
_philter_instance = None
_philter_config_path = None


def _init_worker(config_path):
    """Initialize Philter once per worker."""
    global _philter_instance, _philter_config_path
    _philter_config_path = config_path

    try:
        philter_config = {
            "filters": config_path,
            "phi_text": {},
            "filenames": [],
            "verbose": False,
            "run_eval": False
        }
        _philter_instance = Philter(philter_config)
    except Exception as e:
        logger.error(f"Worker {os.getpid()}: Philter init failed: {e}")
        raise


def _fast_transform(text, filename, philter):
    """
    Optimized transform using range-based replacement.
    5-10x faster than character-by-character iteration.
    """
    if not text:
        return ""

    if filename not in philter.include_map.map:
        return re.sub(r'[a-zA-Z0-9]', '*', text)

    preserve_ranges = []
    coord_map = philter.include_map.map[filename]

    for start in sorted(coord_map.keys()):
        stop = coord_map[start]
        preserve_ranges.append((start, stop))

    result = []
    last_end = 0

    for start, stop in preserve_ranges:
        if start > last_end:
            gap = text[last_end:start]
            result.append(re.sub(r'[a-zA-Z0-9]', '*', gap))

        result.append(text[start:stop])
        last_end = stop

    if last_end < len(text):
        gap = text[last_end:]
        result.append(re.sub(r'[a-zA-Z0-9]', '*', gap))

    return ''.join(result)


def _process_batch(batch_data):
    """
    Process a batch of records.

    Args:
        batch_data: List of (note_id, deid_name, text, shifted_year) tuples

    Returns:
        List of (note_id, deid_text, deid_name, shifted_year) tuples
    """
    global _philter_instance

    if _philter_instance is None:
        return []

    texts_dict = {}
    record_map = {}

    for note_id, deid_name, text, shifted_year in batch_data:
        if not text or len(str(text).strip()) == 0:
            continue

        # Apply keyword removal first (belt and suspenders approach)
        cleaned_text = keyword_removal.remove_keywords(str(text))
        texts_dict[deid_name] = cleaned_text
        record_map[deid_name] = (note_id, shifted_year)

    if not texts_dict:
        return []

    # Clear Philter state
    _philter_instance.include_map.map.clear()
    _philter_instance.exclude_map.map.clear()
    _philter_instance.data_all_files.clear()
    for phi_type in _philter_instance.phi_type_list:
        _philter_instance.phi_type_dict[phi_type][0].map.clear()

    results = []

    try:
        _philter_instance.texts = texts_dict
        _philter_instance.filenames = list(texts_dict.keys())
        _philter_instance.map_coordinates()

        for deid_name in texts_dict.keys():
            try:
                deid_text = _fast_transform(
                    texts_dict[deid_name],
                    deid_name,
                    _philter_instance
                )
                note_id, shifted_year = record_map[deid_name]
                results.append((note_id, deid_text, deid_name, shifted_year))
            except:
                # Fallback to original method
                try:
                    deid_text = _philter_instance.transform_text_asterisk(
                        texts_dict[deid_name],
                        deid_name
                    )
                    note_id, shifted_year = record_map[deid_name]
                    results.append((note_id, deid_text, deid_name, shifted_year))
                except:
                    pass
    except:
        pass

    return results


def save_checkpoint(checkpoint_path, processed_count, batch_num):
    """Save progress checkpoint to S3."""
    checkpoint = {
        'processed_count': processed_count,
        'batch_num': batch_num,
        'timestamp': datetime.now().isoformat()
    }

    if checkpoint_path.startswith('s3://'):
        # Save to S3
        s3 = boto3.client('s3')
        bucket, key = checkpoint_path.replace('s3://', '').split('/', 1)
        s3.put_object(
            Bucket=bucket,
            Key=f"{key}/checkpoint.json",
            Body=json.dumps(checkpoint, indent=2)
        )
    else:
        # Save locally
        with open(checkpoint_path, 'w') as f:
            json.dump(checkpoint, f, indent=2)


def load_checkpoint(checkpoint_path):
    """Load progress checkpoint from S3."""
    try:
        if checkpoint_path.startswith('s3://'):
            s3 = boto3.client('s3')
            bucket, key = checkpoint_path.replace('s3://', '').split('/', 1)
            obj = s3.get_object(Bucket=bucket, Key=f"{key}/checkpoint.json")
            return json.loads(obj['Body'].read())
        else:
            if os.path.exists(checkpoint_path):
                with open(checkpoint_path, 'r') as f:
                    return json.load(f)
    except:
        pass
    return None


def write_parquet_batch(results, output_path, batch_num):
    """Write results to Parquet file."""
    df = pd.DataFrame(results, columns=['NoteCSNID', 'NoteTXT', 'DeIDNoteID', 'ShiftedContactYear'])
    table = pa.Table.from_pandas(df)

    output_file = f"{output_path}/batch_{batch_num:06d}.parquet"

    if output_path.startswith('s3://'):
        # Write to S3
        import s3fs
        fs = s3fs.S3FileSystem()
        with fs.open(output_file, 'wb') as f:
            pq.write_table(table, f, compression='snappy')
    else:
        # Write locally
        os.makedirs(output_path, exist_ok=True)
        pq.write_table(table, output_file, compression='snappy')

    logger.warning(f"  Wrote batch {batch_num}: {len(results)} records to {output_file}")


def process_partition(input_path, output_path, partition_id, num_workers, batch_size, philter_config):
    """
    Process a partition of Parquet files.

    Args:
        input_path: S3 path or local path to input Parquet files
        output_path: S3 path or local path for output
        partition_id: ID of this partition
        num_workers: Number of parallel workers
        batch_size: Records per batch
        philter_config: Path to Philter config JSON
    """
    logger.warning("=" * 80)
    logger.warning(f"PARTITION {partition_id}: Processing")
    logger.warning("=" * 80)
    logger.warning(f"Input: {input_path}")
    logger.warning(f"Output: {output_path}")
    logger.warning(f"Workers: {num_workers}")
    logger.warning(f"Batch size: {batch_size}")
    logger.warning("=" * 80)

    # Load checkpoint
    checkpoint = load_checkpoint(output_path)
    start_batch = 0
    total_processed = 0

    if checkpoint:
        logger.warning(f"Checkpoint found: {checkpoint['processed_count']:,} records processed")
        logger.warning("Auto-resuming from checkpoint...")
        start_batch = checkpoint['batch_num']
        total_processed = checkpoint['processed_count']

    # Read Parquet files
    logger.warning("\nReading Parquet files...")
    start_time = datetime.now()

    if input_path.startswith('s3://'):
        import s3fs
        fs = s3fs.S3FileSystem()
        dataset = pq.ParquetDataset(input_path, filesystem=fs)
        table = dataset.read()
    else:
        dataset = pq.ParquetDataset(input_path)
        table = dataset.read()

    df = table.to_pandas()
    elapsed = (datetime.now() - start_time).total_seconds()

    logger.warning(f"✓ Loaded {len(df):,} records in {elapsed:.1f}s")
    logger.warning(f"  Columns: {list(df.columns)}")
    logger.warning(f"  Memory: {df.memory_usage(deep=True).sum() / 1024**3:.2f} GB")

    # Verify required columns
    required_cols = ['NoteCSNID', 'DeIDNoteID', 'NoteTXT', 'ShiftedContactYear']
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        logger.error(f"Missing columns: {missing}")
        return

    # Initialize worker pool
    logger.warning(f"\nInitializing {num_workers} workers...")
    init_start = datetime.now()
    pool = Pool(processes=num_workers, initializer=_init_worker, initargs=(philter_config,))
    init_elapsed = (datetime.now() - init_start).total_seconds()
    logger.warning(f"✓ Workers ready in {init_elapsed:.1f}s")

    # Process in batches
    logger.warning("\n" + "=" * 80)
    logger.warning("PROCESSING STARTED")
    logger.warning("=" * 80)

    process_start = datetime.now()
    batch_num = start_batch
    all_results = []

    for i in range(start_batch * batch_size, len(df), batch_size):
        batch_df = df.iloc[i:i+batch_size]

        # Prepare batch data
        batch_data = [
            (row['NoteCSNID'], row['DeIDNoteID'], row['NoteTXT'], row['ShiftedContactYear'])
            for _, row in batch_df.iterrows()
        ]

        # Split into sub-batches for parallel processing
        worker_batch_size = max(1, len(batch_data) // num_workers)
        worker_batches = []
        for j in range(0, len(batch_data), worker_batch_size):
            worker_batches.append(batch_data[j:j+worker_batch_size])

        # Process in parallel
        batch_results = pool.map(_process_batch, worker_batches)

        # Flatten results
        for result_list in batch_results:
            all_results.extend(result_list)

        total_processed += len(batch_data)
        batch_num += 1

        # Write output every 100K records
        if len(all_results) >= 100_000:
            write_parquet_batch(all_results, output_path, batch_num)
            all_results = []

            # Save checkpoint
            save_checkpoint(output_path, total_processed, batch_num)

            # Progress update
            elapsed = (datetime.now() - process_start).total_seconds()
            rate = total_processed / elapsed if elapsed > 0 else 0
            remaining = len(df) - total_processed
            eta_seconds = remaining / rate if rate > 0 else 0

            logger.warning(f"")
            logger.warning(f"Progress: {total_processed:,} / {len(df):,} ({total_processed/len(df)*100:.1f}%)")
            logger.warning(f"  Speed: {rate:.1f} rec/sec")
            logger.warning(f"  ETA: {eta_seconds/3600:.1f} hours")

    # Write final batch
    if all_results:
        write_parquet_batch(all_results, output_path, batch_num)

    # Cleanup
    pool.close()
    pool.join()

    # Final summary
    elapsed = (datetime.now() - process_start).total_seconds()

    logger.warning("\n" + "=" * 80)
    logger.warning("COMPLETED")
    logger.warning("=" * 80)
    logger.warning(f"Time: {elapsed/3600:.1f} hours")
    logger.warning(f"Processed: {total_processed:,} records")
    logger.warning(f"Speed: {total_processed/elapsed:.1f} rec/sec")
    logger.warning("=" * 80)


def main():
    parser = argparse.ArgumentParser(description='Process Parquet files with Philter de-identification')
    parser.add_argument('--input-path', required=True, help='S3 or local path to input Parquet files')
    parser.add_argument('--output-path', required=True, help='S3 or local path for output Parquet files')
    parser.add_argument('--partition-id', type=int, default=1, help='Partition ID')
    parser.add_argument('--workers', type=int, default=cpu_count(), help='Number of parallel workers')
    parser.add_argument('--batch-size', type=int, default=10000, help='Records per batch')
    parser.add_argument('--philter-config', default='configs/philter_one.json', help='Path to Philter config')

    args = parser.parse_args()

    # Validate paths
    if args.input_path.startswith('s3://'):
        logger.warning("Using S3 for input (ensure AWS credentials are configured)")
    if args.output_path.startswith('s3://'):
        logger.warning("Using S3 for output (ensure AWS credentials are configured)")

    process_partition(
        input_path=args.input_path,
        output_path=args.output_path,
        partition_id=args.partition_id,
        num_workers=args.workers,
        batch_size=args.batch_size,
        philter_config=args.philter_config
    )


if __name__ == "__main__":
    main()
