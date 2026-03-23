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
        --workers 30
"""
import pyarrow.parquet as pq
import pyarrow as pa
import pandas as pd
from philter import Philter
import keyword_removal
import argparse
import logging
import csv
from datetime import datetime
from multiprocessing import Pool, cpu_count
import os
import json
import re
import gc
import signal
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
        batch_data: List of (note_csn_id, bdsp_patient_id, bdsp_encounter_id,
                    shifted_contact_date, text, de_id_filename) tuples

    Returns:
        Tuple of:
          - List of (bdsp_patient_id, bdsp_encounter_id, shifted_contact_date,
                     deid_text, de_id_filename) tuples
          - List of (NoteCSNID, de_id_filename, status) tuples
    """
    global _philter_instance

    if _philter_instance is None:
        return [], []

    texts_dict = {}
    record_map = {}
    status_records = []

    for note_csn_id, bdsp_patient_id, bdsp_encounter_id, shifted_contact_date, text, de_id_filename in batch_data:
        if not text or len(str(text).strip()) == 0:
            status_records.append((note_csn_id, str(de_id_filename), "skipped"))
            continue

        deid_key = str(de_id_filename)
        cleaned_text = keyword_removal.remove_keywords(str(text))
        texts_dict[deid_key] = cleaned_text
        record_map[deid_key] = (note_csn_id, bdsp_patient_id, bdsp_encounter_id, shifted_contact_date)

    if not texts_dict:
        return [], status_records

    # Clear ALL Philter state (including all_coords and coord2pattern which leaked memory)
    _philter_instance.include_map.map.clear()
    _philter_instance.include_map.all_coords.clear()
    _philter_instance.include_map.coord2pattern.clear()
    _philter_instance.exclude_map.map.clear()
    _philter_instance.exclude_map.all_coords.clear()
    _philter_instance.exclude_map.coord2pattern.clear()
    _philter_instance.data_all_files.clear()
    for phi_type in _philter_instance.phi_type_list:
        _philter_instance.phi_type_dict[phi_type][0].map.clear()
        _philter_instance.phi_type_dict[phi_type][0].all_coords.clear()
        _philter_instance.phi_type_dict[phi_type][0].coord2pattern.clear()

    # Restore pattern data from cache (map_coordinates deletes "data" keys)
    # Reference assignment — map_coordinates only reads data then deletes the key,
    # it does not modify the data content itself, so sharing the reference is safe
    for i, data in _pattern_data_cache.items():
        _philter_instance.patterns[i]["data"] = data

    results = []

    # Timeout handler — prevents worker hang on pathological notes (regex backtracking)
    def _alarm_handler(signum, frame):
        raise TimeoutError("Batch timed out")

    try:
        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(60)  # 60 second timeout per batch (normal batch takes ~2s)

        _philter_instance.texts = texts_dict
        _philter_instance.filenames = list(texts_dict.keys())
        _philter_instance.map_coordinates()

        signal.alarm(0)  # cancel alarm — map_coordinates succeeded

        for deid_key in texts_dict.keys():
            try:
                deid_text = _fast_transform(
                    texts_dict[deid_key],
                    deid_key,
                    _philter_instance
                )
                note_csn_id, bdsp_patient_id, bdsp_encounter_id, shifted_contact_date = record_map[deid_key]
                results.append((bdsp_patient_id, bdsp_encounter_id,
                                shifted_contact_date, deid_text, deid_key))
                status_records.append((note_csn_id, deid_key, "deidentified"))
            except Exception as e1:
                # Fallback to original method
                try:
                    deid_text = _philter_instance.transform_text_asterisk(
                        texts_dict[deid_key],
                        deid_key
                    )
                    note_csn_id, bdsp_patient_id, bdsp_encounter_id, shifted_contact_date = record_map[deid_key]
                    results.append((bdsp_patient_id, bdsp_encounter_id,
                                    shifted_contact_date, deid_text, deid_key))
                    status_records.append((note_csn_id, deid_key, "deidentified"))
                except Exception as e2:
                    logger.warning(f"Worker {os.getpid()}: Failed record {deid_key}: fast={e1}, fallback={e2}")
    except TimeoutError:
        signal.alarm(0)
        logger.warning(f"Worker {os.getpid()}: TIMEOUT — batch of {len(texts_dict)} records falling back to full redaction")
        # Fallback: fully redact all alphanumeric — safe, fast, no records lost
        for deid_key in texts_dict.keys():
            try:
                deid_text = re.sub(r'[a-zA-Z0-9]', '*', texts_dict[deid_key])
                note_csn_id, bdsp_patient_id, bdsp_encounter_id, shifted_contact_date = record_map[deid_key]
                results.append((bdsp_patient_id, bdsp_encounter_id,
                                shifted_contact_date, deid_text, deid_key))
                status_records.append((note_csn_id, deid_key, "full_redaction"))
            except Exception:
                pass
    except Exception as e:
        signal.alarm(0)
        logger.error(f"Worker {os.getpid()}: map_coordinates failed for {len(texts_dict)} texts: {e}")
        import traceback
        traceback.print_exc()

    # Explicitly clear ALL large objects and force garbage collection to prevent memory growth
    texts_dict.clear()
    record_map.clear()
    _philter_instance.include_map.map.clear()
    _philter_instance.include_map.all_coords.clear()
    _philter_instance.include_map.coord2pattern.clear()
    _philter_instance.exclude_map.map.clear()
    _philter_instance.exclude_map.all_coords.clear()
    _philter_instance.exclude_map.coord2pattern.clear()
    _philter_instance.data_all_files.clear()
    _philter_instance.texts.clear()
    _philter_instance.filenames.clear()
    for phi_type in _philter_instance.phi_type_list:
        _philter_instance.phi_type_dict[phi_type][0].map.clear()
        _philter_instance.phi_type_dict[phi_type][0].all_coords.clear()
        _philter_instance.phi_type_dict[phi_type][0].coord2pattern.clear()
    gc.collect()

    return results, status_records


def _process_batch_bi(batch_data):
    """
    Process a batch of BI_Clinical_Notes records.

    Args:
        batch_data: List of (clinicalnotetextkey, type_val, shifted_creation, count_val,
                    text, deidentified_name) tuples

    Returns:
        Tuple of:
          - List of (deidentified_name, deid_text, type_val, shifted_creation, count_val) tuples
          - List of (CLINICALNOTETEXTKEY, DeidentifiedName, status) tuples
    """
    global _philter_instance

    if _philter_instance is None:
        return [], []

    texts_dict = {}
    record_map = {}
    status_records = []

    for clinicalnotetextkey, type_val, shifted_creation, count_val, text, deidentified_name in batch_data:
        if not text or len(str(text).strip()) == 0:
            status_records.append((clinicalnotetextkey, str(deidentified_name), "skipped"))
            continue

        deid_key = str(deidentified_name)
        cleaned_text = keyword_removal.remove_keywords(str(text))
        texts_dict[deid_key] = cleaned_text
        record_map[deid_key] = (clinicalnotetextkey, type_val, shifted_creation, count_val)

    if not texts_dict:
        return [], status_records

    _philter_instance.include_map.map.clear()
    _philter_instance.include_map.all_coords.clear()
    _philter_instance.include_map.coord2pattern.clear()
    _philter_instance.exclude_map.map.clear()
    _philter_instance.exclude_map.all_coords.clear()
    _philter_instance.exclude_map.coord2pattern.clear()
    _philter_instance.data_all_files.clear()
    for phi_type in _philter_instance.phi_type_list:
        _philter_instance.phi_type_dict[phi_type][0].map.clear()
        _philter_instance.phi_type_dict[phi_type][0].all_coords.clear()
        _philter_instance.phi_type_dict[phi_type][0].coord2pattern.clear()

    for i, data in _pattern_data_cache.items():
        _philter_instance.patterns[i]["data"] = data

    results = []

    def _alarm_handler(signum, frame):
        raise TimeoutError("Batch timed out")

    try:
        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(60)

        _philter_instance.texts = texts_dict
        _philter_instance.filenames = list(texts_dict.keys())
        _philter_instance.map_coordinates()

        signal.alarm(0)

        for deid_key in texts_dict.keys():
            try:
                deid_text = _fast_transform(texts_dict[deid_key], deid_key, _philter_instance)
                clinicalnotetextkey, type_val, shifted_creation, count_val = record_map[deid_key]
                results.append((deid_key, deid_text, type_val, shifted_creation, count_val))
                status_records.append((clinicalnotetextkey, deid_key, "deidentified"))
            except Exception as e1:
                try:
                    deid_text = _philter_instance.transform_text_asterisk(texts_dict[deid_key], deid_key)
                    clinicalnotetextkey, type_val, shifted_creation, count_val = record_map[deid_key]
                    results.append((deid_key, deid_text, type_val, shifted_creation, count_val))
                    status_records.append((clinicalnotetextkey, deid_key, "deidentified"))
                except Exception as e2:
                    logger.warning(f"Worker {os.getpid()}: Failed record {deid_key}: fast={e1}, fallback={e2}")
    except TimeoutError:
        signal.alarm(0)
        logger.warning(f"Worker {os.getpid()}: TIMEOUT — batch of {len(texts_dict)} records falling back to full redaction")
        for deid_key in texts_dict.keys():
            try:
                deid_text = re.sub(r'[a-zA-Z0-9]', '*', texts_dict[deid_key])
                clinicalnotetextkey, type_val, shifted_creation, count_val = record_map[deid_key]
                results.append((deid_key, deid_text, type_val, shifted_creation, count_val))
                status_records.append((clinicalnotetextkey, deid_key, "full_redaction"))
            except Exception:
                pass
    except Exception as e:
        signal.alarm(0)
        logger.error(f"Worker {os.getpid()}: map_coordinates failed for {len(texts_dict)} texts: {e}")
        import traceback
        traceback.print_exc()

    texts_dict.clear()
    record_map.clear()
    _philter_instance.include_map.map.clear()
    _philter_instance.include_map.all_coords.clear()
    _philter_instance.include_map.coord2pattern.clear()
    _philter_instance.exclude_map.map.clear()
    _philter_instance.exclude_map.all_coords.clear()
    _philter_instance.exclude_map.coord2pattern.clear()
    _philter_instance.data_all_files.clear()
    _philter_instance.texts.clear()
    _philter_instance.filenames.clear()
    for phi_type in _philter_instance.phi_type_list:
        _philter_instance.phi_type_dict[phi_type][0].map.clear()
        _philter_instance.phi_type_dict[phi_type][0].all_coords.clear()
        _philter_instance.phi_type_dict[phi_type][0].coord2pattern.clear()
    gc.collect()

    return results, status_records


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
        key = key.rstrip('/')
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
            key = key.rstrip('/')
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


def write_parquet_batch(results, output_path, batch_num, text_col='NoteTXT', note_type='clinicalnotes'):
    """Write results to Parquet file."""
    if note_type == 'bi_clinicalnotes':
        df = pd.DataFrame(results, columns=['DeidentifiedName', 'TEXT', 'TYPE', 'CREATIONINSTANT', 'COUNT'])
    else:
        df = pd.DataFrame(results, columns=['BDSPPatientID', 'bdsp_encounter_id', 'ShiftedContactDate', text_col, 'de_id_filename'])
    table = pa.Table.from_pandas(df)

    output_file = f"{output_path.rstrip('/')}/batch_{batch_num:06d}.parquet"

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


def write_status_csv(status_records, output_path, partition_id, id_col='NoteCSNID', filename_col='de_id_filename'):
    """Append status records to the partition-level status CSV."""
    if output_path.startswith('s3://'):
        # S3 doesn't support append — write locally on the instance
        csv_file = os.path.expanduser(f"~/status_partition_{partition_id}.csv")
    else:
        csv_file = os.path.join(output_path, f"status_partition_{partition_id}.csv")
    write_header = not os.path.exists(csv_file)

    with open(csv_file, 'a', newline='') as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([id_col, filename_col, "status"])
        writer.writerows(status_records)


def process_partition(input_path, output_path, partition_id, num_workers, batch_size, philter_config, file_start=0, file_end=None, id_col='NoteCSNID', text_col='NoteTXT', note_type='clinicalnotes', filename_col='de_id_filename'):
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

    # Define columns needed based on note type (must be before schema check)
    if note_type == 'bi_clinicalnotes':
        NEED_COLS = [id_col, 'TYPE', 'SHIFTED_CREATIONINSTANT', 'COUNT', text_col, filename_col]
        batch_fn = _process_batch_bi
    else:
        NEED_COLS = ['BDSPPatientID', 'bdsp_encounter_id', 'ShiftedContactDate', filename_col, id_col, text_col]
        batch_fn = _process_batch

    # Verify schema from first remaining file
    first_table = pq.read_table(remaining_files[0])
    cols = first_table.column_names
    missing = [c for c in NEED_COLS if c not in cols]
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
                maxtasksperchild=50)
    init_elapsed = (datetime.now() - init_start).total_seconds()
    logger.warning(f"✓ Workers ready in {init_elapsed:.1f}s")

    # Process in batches — one parquet file at a time
    logger.warning("\n" + "=" * 80)
    logger.warning("PROCESSING STARTED")
    logger.warning("=" * 80)

    process_start = datetime.now()
    all_results = []
    total_records = 0

    # Chunked reading: read large files in 500K-row chunks to avoid 32GB+ memory spikes
    CHUNK_ROWS = 500000
    SUB_BATCH_SIZE = 20
    WRITE_THRESHOLD = 100000  # Write every 100K results to cap memory
    all_status_records = []

    for file_idx, parquet_file in enumerate(remaining_files):
        file_start_time = datetime.now()
        file_basename = os.path.basename(parquet_file)
        logger.warning(f"\n--- File {file_idx+1}/{len(remaining_files)}: {file_basename} ---")

        # Open file handle for chunked reading
        if parquet_file.startswith('s3://'):
            import s3fs
            _fs = s3fs.S3FileSystem()
            _fh = _fs.open(parquet_file)
            pf = pq.ParquetFile(_fh)
        else:
            _fh = None
            pf = pq.ParquetFile(parquet_file)

        file_records = pf.metadata.num_rows
        total_records += file_records
        num_chunks = (file_records + CHUNK_ROWS - 1) // CHUNK_ROWS
        logger.warning(f"  File has {file_records:,} records, reading in {num_chunks} chunk(s) of {CHUNK_ROWS:,}")

        file_result_count = 0
        total_sub_batches = (file_records + SUB_BATCH_SIZE - 1) // SUB_BATCH_SIZE
        sub_batches_done_total = 0
        chunk_num = 0

        for record_batch in pf.iter_batches(batch_size=CHUNK_ROWS, columns=NEED_COLS):
            chunk_num += 1
            df_chunk = record_batch.to_pandas()
            chunk_records = len(df_chunk)
            chunk_gb = df_chunk.memory_usage(deep=True).sum() / 1024**3
            logger.warning(f"  Chunk {chunk_num}/{num_chunks}: {chunk_records:,} records ({chunk_gb:.1f} GB)")

            if note_type == 'bi_clinicalnotes':
                file_data = list(zip(
                    df_chunk[id_col].values,
                    df_chunk['TYPE'].values,
                    df_chunk['SHIFTED_CREATIONINSTANT'].values,
                    df_chunk['COUNT'].values,
                    df_chunk[text_col].values,
                    df_chunk[filename_col].values
                ))
            else:
                file_data = list(zip(
                    df_chunk[id_col].values,
                    df_chunk['BDSPPatientID'].values,
                    df_chunk['bdsp_encounter_id'].values,
                    df_chunk['ShiftedContactDate'].values,
                    df_chunk[text_col].values,
                    df_chunk[filename_col].values
                ))
            del df_chunk

            worker_batches = [file_data[j:j+SUB_BATCH_SIZE]
                              for j in range(0, len(file_data), SUB_BATCH_SIZE)]

            sub_batches_done = 0
            progress_interval = max(1, min(100, len(worker_batches) // 10))

            for result_list, status_list in pool.imap_unordered(batch_fn, worker_batches):
                all_results.extend(result_list)
                all_status_records.extend(status_list)
                file_result_count += len(result_list)
                sub_batches_done += 1
                sub_batches_done_total += 1

                if sub_batches_done % progress_interval == 0 or sub_batches_done == len(worker_batches):
                    elapsed_file = (datetime.now() - file_start_time).total_seconds()
                    rate = file_result_count / elapsed_file if elapsed_file > 0 else 0
                    pct = 100 * sub_batches_done_total / total_sub_batches
                    logger.warning(f"  Progress: {sub_batches_done_total}/{total_sub_batches} sub-batches ({pct:.0f}%), "
                                   f"{file_result_count:,} output, {rate:.1f} rec/sec")

                # Intermediate write to cap memory
                if len(all_results) >= WRITE_THRESHOLD:
                    batch_num += 1
                    write_parquet_batch(all_results, output_path, batch_num, text_col=text_col, note_type=note_type)
                    all_results = []
                    gc.collect()

                # Flush status CSV periodically to avoid large in-memory accumulation
                if len(all_status_records) >= WRITE_THRESHOLD:
                    write_status_csv(all_status_records, output_path, partition_id, id_col=id_col, filename_col=filename_col)
                    all_status_records = []

            del file_data
            del worker_batches
            gc.collect()

        # Close S3 file handle
        if _fh is not None:
            _fh.close()

        total_processed += file_records

        # Write remaining results for this file
        if all_results:
            batch_num += 1
            write_parquet_batch(all_results, output_path, batch_num, text_col=text_col, note_type=note_type)
            all_results = []

        # Flush remaining status records for this file
        if all_status_records:
            write_status_csv(all_status_records, output_path, partition_id, id_col=id_col, filename_col=filename_col)
            all_status_records = []

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
        write_parquet_batch(all_results, output_path, batch_num, text_col=text_col, note_type=note_type)

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
    parser.add_argument('--note-type', choices=['clinicalnotes', 'imagingreport', 'bi_clinicalnotes'], default='clinicalnotes',
                        help='Type of notes: clinicalnotes (NoteTXT/NoteCSNID), imagingreport (ReportTXT/OrderProcedureID), or bi_clinicalnotes (TEXT/CLINICALNOTETEXTKEY)')

    args = parser.parse_args()

    if args.note_type == 'imagingreport':
        id_col = 'OrderProcedureID'
        text_col = 'ReportTXT'
        filename_col = 'de_id_filename'
    elif args.note_type == 'bi_clinicalnotes':
        id_col = 'CLINICALNOTETEXTKEY'
        text_col = 'TEXT'
        filename_col = 'DeidentifiedName'
    else:
        id_col = 'NoteCSNID'
        text_col = 'NoteTXT'
        filename_col = 'de_id_filename'

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
        file_end=args.file_end,
        id_col=id_col,
        text_col=text_col,
        note_type=args.note_type,
        filename_col=filename_col,
    )


if __name__ == "__main__":
    main()
