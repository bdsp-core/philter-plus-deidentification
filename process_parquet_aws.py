"""
Parquet Processing Script for Hyper-Fast De-identification.

Designed for:
- Input: Parquet files from local filesystem or S3
- Output: De-identified Parquet files
- Multi-core processing with all optimizations
- Checkpoint-based resume capability (crash-safe)

Optimizations applied:
- Pattern data caching (avoids file re-reads per batch)
- Reference assignment for pattern restore (not deep copy)
- imap_unordered for dynamic work scheduling (no straggler blocking)
- maxtasksperchild=3 (workers restart to release accumulated memory)
- Vectorized extraction (100x faster than iterrows)
- File-by-file processing (constant memory, crash-safe)
- Per-file checkpoints with completed_files tracking
- Explicit gc.collect() after each batch

Usage:
    python process_parquet_aws.py \
        --input-path /path/to/input/ \
        --output-path /path/to/output/ \
        --partition-id 1 \
        --workers 20
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
import gc
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
_pattern_data_cache = None


def _init_worker(config_path):
    """Initialize Philter once per worker."""
    global _philter_instance, _philter_config_path, _pattern_data_cache
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

        # Cache pattern data references so we can restore after map_coordinates
        # deletes "data" keys (instead of re-reading files from disk every batch)
        import copy
        _pattern_data_cache = {}
        for i, pat in enumerate(_philter_instance.patterns):
            if "data" in pat:
                _pattern_data_cache[i] = copy.deepcopy(pat["data"])
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

    # Restore pattern data from cache (map_coordinates deletes "data" keys)
    # Reference assignment — map_coordinates only reads data then deletes the key,
    # it does not modify the data content itself, so sharing the reference is safe
    for i, data in _pattern_data_cache.items():
        _philter_instance.patterns[i]["data"] = data

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
            except Exception as e1:
                # Fallback to original method
                try:
                    deid_text = _philter_instance.transform_text_asterisk(
                        texts_dict[deid_name],
                        deid_name
                    )
                    note_id, shifted_year = record_map[deid_name]
                    results.append((note_id, deid_text, deid_name, shifted_year))
                except Exception as e2:
                    logger.warning(f"Worker {os.getpid()}: Failed record {deid_name}: fast={e1}, fallback={e2}")
    except Exception as e:
        logger.error(f"Worker {os.getpid()}: map_coordinates failed for {len(texts_dict)} texts: {e}")
        import traceback
        traceback.print_exc()

    # Explicitly clear large objects and force garbage collection to prevent memory growth
    texts_dict.clear()
    record_map.clear()
    _philter_instance.include_map.map.clear()
    _philter_instance.exclude_map.map.clear()
    _philter_instance.data_all_files.clear()
    _philter_instance.texts.clear()
    _philter_instance.filenames.clear()
    for phi_type in _philter_instance.phi_type_list:
        _philter_instance.phi_type_dict[phi_type][0].map.clear()
    gc.collect()

    return results


def save_checkpoint(checkpoint_path, processed_count, batch_num, completed_files=None):
    """Save progress checkpoint."""
    checkpoint = {
        'processed_count': processed_count,
        'batch_num': batch_num,
        'completed_files': completed_files or [],
        'timestamp': datetime.now().isoformat()
    }

    if checkpoint_path.startswith('s3://'):
        s3 = boto3.client('s3')
        bucket, key = checkpoint_path.replace('s3://', '').split('/', 1)
        s3.put_object(
            Bucket=bucket,
            Key=f"{key}/checkpoint.json",
            Body=json.dumps(checkpoint, indent=2)
        )
    else:
        os.makedirs(checkpoint_path, exist_ok=True)
        local_file = os.path.join(checkpoint_path, 'checkpoint.json')
        with open(local_file, 'w') as f:
            json.dump(checkpoint, f, indent=2)


def load_checkpoint(checkpoint_path):
    """Load progress checkpoint."""
    try:
        if checkpoint_path.startswith('s3://'):
            s3 = boto3.client('s3')
            bucket, key = checkpoint_path.replace('s3://', '').split('/', 1)
            obj = s3.get_object(Bucket=bucket, Key=f"{key}/checkpoint.json")
            return json.loads(obj['Body'].read())
        else:
            local_file = os.path.join(checkpoint_path, 'checkpoint.json')
            if os.path.exists(local_file):
                with open(local_file, 'r') as f:
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
        import s3fs
        fs = s3fs.S3FileSystem()
        with fs.open(output_file, 'wb') as f:
            pq.write_table(table, f, compression='snappy')
    else:
        os.makedirs(output_path, exist_ok=True)
        pq.write_table(table, output_file, compression='snappy')

    logger.warning(f"  Wrote batch {batch_num}: {len(results)} records to {output_file}")


def process_partition(input_path, output_path, partition_id, num_workers, batch_size, philter_config, file_start=0, file_end=None):
    """
    Process a partition of Parquet files.

    Args:
        input_path: S3 path or local path to input Parquet files
        output_path: S3 path or local path for output
        partition_id: ID of this partition
        num_workers: Number of parallel workers
        batch_size: Records per batch
        philter_config: Path to Philter config JSON
        file_start: Index of first file to process (0-based)
        file_end: Index of last file to process (exclusive, None=all)
    """
    logger.warning("=" * 80)
    logger.warning(f"PARTITION {partition_id}: Processing")
    logger.warning("=" * 80)
    logger.warning(f"Input: {input_path}")
    logger.warning(f"Output: {output_path}")
    logger.warning(f"Workers: {num_workers}")
    logger.warning(f"Batch size: {batch_size}")
    logger.warning("=" * 80)

    # Load checkpoint to find already-completed files
    checkpoint = load_checkpoint(output_path)
    completed_files = set()
    total_processed = 0
    batch_num = 0

    if checkpoint:
        completed_files = set(checkpoint.get('completed_files', []))
        total_processed = checkpoint.get('processed_count', 0)
        batch_num = checkpoint.get('batch_num', 0)
        if completed_files:
            logger.warning(f"Checkpoint found: {len(completed_files)} files already done, {total_processed:,} records processed")
            logger.warning("Resuming — skipping completed files...")

    # Discover Parquet files (one at a time to avoid OOM)
    logger.warning("\nDiscovering Parquet files...")

    if input_path.startswith('s3://'):
        import s3fs
        fs = s3fs.S3FileSystem()
        parquet_files = sorted([f"s3://{f}" for f in fs.glob(f"{input_path.rstrip('/')}/*.parquet")])
    else:
        import glob
        parquet_files = sorted(glob.glob(os.path.join(input_path, "*.parquet")))

    if not parquet_files:
        logger.error(f"No parquet files found in {input_path}")
        return

    # Apply file range selection (for splitting subfolders across instances)
    if file_start > 0 or file_end is not None:
        total_files = len(parquet_files)
        parquet_files = parquet_files[file_start:file_end]
        logger.warning(f"  File range: [{file_start}:{file_end}] — selected {len(parquet_files)} of {total_files} files")

    # Filter out already-completed files
    remaining_files = [f for f in parquet_files if os.path.basename(f) not in completed_files]
    logger.warning(f"  Found {len(parquet_files)} parquet files, {len(remaining_files)} remaining to process")

    if not remaining_files:
        logger.warning("All files already processed! Nothing to do.")
        return

    # Verify schema from first remaining file
    first_table = pq.read_table(remaining_files[0])
    cols = first_table.column_names
    required_cols = ['NoteCSNID', 'DeIDNoteID', 'NoteTXT', 'ShiftedContactYear']
    missing = [c for c in required_cols if c not in cols]
    if missing:
        logger.error(f"Missing columns: {missing}")
        logger.error(f"Available columns: {cols}")
        return
    logger.warning(f"  Columns: {cols}")
    del first_table

    # Initialize worker pool
    logger.warning(f"\nInitializing {num_workers} workers...")
    init_start = datetime.now()
    pool = Pool(processes=num_workers, initializer=_init_worker, initargs=(philter_config,),
                maxtasksperchild=3)
    init_elapsed = (datetime.now() - init_start).total_seconds()
    logger.warning(f"Workers ready in {init_elapsed:.1f}s")

    # Process in batches — one parquet file at a time
    logger.warning("\n" + "=" * 80)
    logger.warning("PROCESSING STARTED")
    logger.warning("=" * 80)

    process_start = datetime.now()
    all_results = []
    total_records = 0

    for file_idx, parquet_file in enumerate(remaining_files):
        file_start_time = datetime.now()
        file_basename = os.path.basename(parquet_file)
        logger.warning(f"\n--- File {file_idx+1}/{len(remaining_files)}: {file_basename} ---")

        table = pq.read_table(parquet_file)
        df = table.to_pandas()
        del table

        file_records = len(df)
        total_records += file_records
        logger.warning(f"  Loaded {file_records:,} records ({df.memory_usage(deep=True).sum() / 1024**3:.1f} GB)")

        # Vectorized extraction (100x faster than iterrows)
        file_data = list(zip(
            df['NoteCSNID'].values,
            df['DeIDNoteID'].values,
            df['NoteTXT'].values,
            df['ShiftedContactYear'].values
        ))
        del df  # Free dataframe memory early

        # Split entire file into small sub-batches for dynamic scheduling
        # Small sub-batches (200 records) prevent straggler workers from blocking others
        sub_batch_size = 200
        worker_batches = [file_data[j:j+sub_batch_size]
                          for j in range(0, len(file_data), sub_batch_size)]
        logger.warning(f"  Split into {len(worker_batches)} sub-batches of {sub_batch_size} records")

        # Process with dynamic scheduling — fast workers grab next chunk immediately
        file_result_count = 0
        sub_batches_done = 0
        progress_interval = max(1, len(worker_batches) // 10)  # Log ~10 times per file

        for result_list in pool.imap_unordered(_process_batch, worker_batches):
            all_results.extend(result_list)
            file_result_count += len(result_list)
            sub_batches_done += 1

            # Log progress periodically
            if sub_batches_done % progress_interval == 0 or sub_batches_done == len(worker_batches):
                elapsed_file = (datetime.now() - file_start_time).total_seconds()
                rate = (sub_batches_done * sub_batch_size) / elapsed_file if elapsed_file > 0 else 0
                pct = 100 * sub_batches_done / len(worker_batches)
                logger.warning(f"  Progress: {sub_batches_done}/{len(worker_batches)} sub-batches ({pct:.0f}%), "
                               f"{file_result_count:,} output, {rate:.1f} rec/sec")

        total_processed += file_records
        batch_num += 1
        del file_data

        # Write output after EACH file (not every 100K) — crash-safe
        if all_results:
            write_parquet_batch(all_results, output_path, batch_num)
            all_results = []

        # Mark this file as completed in checkpoint
        completed_files.add(file_basename)
        save_checkpoint(output_path, total_processed, batch_num, list(completed_files))

        file_elapsed = (datetime.now() - file_start_time).total_seconds()

        # Progress update
        elapsed = (datetime.now() - process_start).total_seconds()
        rate = total_processed / elapsed if elapsed > 0 else 0
        files_done = len(completed_files)
        files_total = len(parquet_files)

        logger.warning(f"  File done in {file_elapsed/60:.1f} min")
        logger.warning(f"  Progress: {files_done}/{files_total} files, {total_processed:,} records, {rate:.1f} rec/sec")

    # Write any remaining results
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
    parser.add_argument('--file-start', type=int, default=0, help='Index of first file to process (0-based, for splitting subfolders)')
    parser.add_argument('--file-end', type=int, default=None, help='Index of last file (exclusive, for splitting subfolders)')

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
        philter_config=args.philter_config,
        file_start=args.file_start,
        file_end=args.file_end
    )


if __name__ == "__main__":
    main()
