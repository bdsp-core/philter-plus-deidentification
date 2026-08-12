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
import sys
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
_surrogate_mode = True          # replace PHI with deterministic fakes (vs asterisks)
_default_shift = 0              # per-note date shift applied when no ShiftDays column is present
_fallback_count = 0             # surrogate->asterisk fallbacks; MUST stay 0 in surrogate mode
_batch_timeout = 60             # seconds per sub-batch before FULL REDACTION (see --batch-timeout)


def _preflight_surrogate(config_path):
    """Verify the surrogate machinery actually works BEFORE processing millions of notes.
    The asterisk fallback is silent by design, so without this a missing dependency costs a full run."""
    import surrogate_names
    surrogate_names._load()          # raises if the nltk 'names' corpus or surname list is unavailable
    return True


def _init_worker(config_path, surrogate_mode=True, default_shift=0, batch_timeout=60):
    """Initialize Philter once per worker."""
    global _philter_instance, _philter_config_path, _pattern_data_cache, _surrogate_mode, _default_shift, _batch_timeout
    _batch_timeout = batch_timeout
    _philter_config_path = config_path
    _surrogate_mode = surrogate_mode
    _default_shift = default_shift

    try:
        philter_config = {
            "filters": config_path,
            "phi_text": {},
            "filenames": [],
            "verbose": False,
            "run_eval": False,
            "default_date_shift": default_shift,
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


def _deid_one(deid_key, text, shift_days, philter):
    """De-identify one record. In surrogate mode, replaces PHI with deterministic fakes and shifts
    in-text dates by the per-note `shift_days` (falls back to the worker default). Otherwise uses the
    legacy fast asterisk transform. Same PHI coordinates either way — only the replacement differs."""
    if _surrogate_mode:
        sd = shift_days if shift_days is not None else _default_shift
        try:
            philter.date_shifts[deid_key] = int(sd)
        except (ValueError, TypeError):
            philter.date_shifts[deid_key] = _default_shift
        return philter.transform_text_surrogate(text, deid_key)
    return _fast_transform(text, deid_key, philter)


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
    shift_map = {}
    status_records = []

    # KNOWN-NAME INJECTION. Philter surrogates the patient's OWN name by identity rather than by
    # guessing, which is the only way to catch names its NER misses (non-Western names especially).
    # It reads them from `known_names`, keyed by the same deid_key as the date shift -- but nothing
    # ever populated that map, so name handling rested entirely on NER plus census name lists.
    # MEASURED against the structured record on a 2,000-note sample: 8.91% of patients had their own
    # real name left in their de-identified note. Pattern-based recall checks are blind to this,
    # because a surname has no shape to match.
    _philter_instance.known_names = {}
    for _row in batch_data:
        (note_csn_id, bdsp_patient_id, bdsp_encounter_id, shifted_contact_date,
         text, de_id_filename, shift_days) = _row[:7]
        _names = _row[7] if len(_row) > 7 else None
        if not text or len(str(text).strip()) == 0:
            status_records.append((note_csn_id, str(de_id_filename), "skipped"))
            continue

        deid_key = str(de_id_filename)
        cleaned_text = keyword_removal.remove_keywords(str(text))
        texts_dict[deid_key] = cleaned_text
        record_map[deid_key] = (note_csn_id, bdsp_patient_id, bdsp_encounter_id, shifted_contact_date)
        shift_map[deid_key] = shift_days
        if _names:
            _philter_instance.known_names[deid_key] = [str(n) for n in _names
                                                       if n and len(str(n).strip()) >= 3]

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
        signal.alarm(_batch_timeout)  # per-batch cap; normal batch ~2s. See --batch-timeout.

        _philter_instance.texts = texts_dict
        _philter_instance.filenames = list(texts_dict.keys())
        _philter_instance.map_coordinates()

        signal.alarm(0)  # cancel alarm — map_coordinates succeeded

        for deid_key in texts_dict.keys():
            try:
                deid_text = _deid_one(
                    deid_key,
                    texts_dict[deid_key],
                    shift_map.get(deid_key),
                    _philter_instance
                )
                note_csn_id, bdsp_patient_id, bdsp_encounter_id, shifted_contact_date = record_map[deid_key]
                results.append((note_csn_id, bdsp_patient_id, bdsp_encounter_id,
                                shifted_contact_date, deid_text, deid_key))
                status_records.append((note_csn_id, deid_key, "deidentified"))
            except Exception as e1:
                # LOUD fallback. Previously this swallowed e1 entirely and still recorded
                # "deidentified", so a missing NLTK 'names' corpus silently turned an entire
                # 62M-note run into asterisk redaction. Never degrade quietly.
                global _fallback_count
                _fallback_count += 1
                if _fallback_count <= 5 or _fallback_count % 10000 == 0:
                    logger.error(f"SURROGATE FAILED (fallback #{_fallback_count}) -> asterisk redaction: "
                                 f"{type(e1).__name__}: {str(e1)[:200]}")
                try:
                    deid_text = _philter_instance.transform_text_asterisk(
                        texts_dict[deid_key],
                        deid_key
                    )
                    note_csn_id, bdsp_patient_id, bdsp_encounter_id, shifted_contact_date = record_map[deid_key]
                    results.append((note_csn_id, bdsp_patient_id, bdsp_encounter_id,
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
                results.append((note_csn_id, bdsp_patient_id, bdsp_encounter_id,
                                shifted_contact_date, deid_text, deid_key))
                # Tagged full_redaction: every alphanumeric became '*', so the note is DESTROYED.
                # The parent collects these from status_records and writes a retry queue to S3 —
                # a worker-local list would never reach the parent under multiprocessing.
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
    shift_map = {}
    status_records = []

    for clinicalnotetextkey, type_val, shifted_creation, count_val, text, deidentified_name, shift_days in batch_data:
        if not text or len(str(text).strip()) == 0:
            status_records.append((clinicalnotetextkey, str(deidentified_name), "skipped"))
            continue

        deid_key = str(deidentified_name)
        cleaned_text = keyword_removal.remove_keywords(str(text))
        texts_dict[deid_key] = cleaned_text
        record_map[deid_key] = (clinicalnotetextkey, type_val, shifted_creation, count_val)
        shift_map[deid_key] = shift_days

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
                deid_text = _deid_one(deid_key, texts_dict[deid_key], shift_map.get(deid_key), _philter_instance)
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
        # NoteCSNID emitted as a REAL column. It was previously dropped from results (kept only in the
        # status CSV), leaving note identity recoverable solely by string-splitting de_id_filename
        # ("{person_id}_{note_id}"). That makes the primary key a derived string and breaks on any null id.
        # Must be right BEFORE the fleet runs — adding it later means re-scrubbing ~975M notes.
        df = pd.DataFrame(results, columns=['NoteCSNID', 'BDSPPatientID', 'bdsp_encounter_id',
                                            'ShiftedContactDate', text_col, 'de_id_filename'])
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


def write_retry_queue(status_records, output_path, partition_id):
    """Persist the keys destroyed by the timeout path so a second pass can redo them properly.

    The timeout fallback replaces EVERY alphanumeric character with '*', so those notes carry no
    clinical content at all. Previously they were only recorded in a local status CSV that never left
    the instance, so ~1% of notes were silently shipped as garbage.
    """
    keys = [r[1] for r in status_records if len(r) > 2 and r[2] == "full_redaction"]
    if not keys:
        return 0
    body = "\n".join(keys) + "\n"
    dest = f"{output_path.rstrip('/')}/_retry/timeouts_partition_{partition_id}.txt"
    if dest.startswith("s3://"):
        import s3fs
        with s3fs.S3FileSystem().open(dest, "w") as f:
            f.write(body)
    else:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, "w").write(body)
    logger.warning(f"  RETRY QUEUE: {len(keys):,} notes hit the timeout and were FULLY REDACTED -> {dest}")
    logger.warning(f"  Re-run these with a larger --batch-timeout before treating the output as complete.")
    return len(keys)


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


def process_partition(input_path, output_path, partition_id, num_workers, batch_size, philter_config, file_start=0, file_end=None, id_col='NoteCSNID', text_col='NoteTXT', note_type='clinicalnotes', filename_col='de_id_filename', surrogate_mode=True, default_shift=0, shift_col=None, batch_timeout=60, only_keys=None, names_path=None):
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

    # --only-keys: retry mode. Process ONLY the notes listed (the timeout queue from a prior run).
    only_set = None
    if only_keys:
        if only_keys.startswith("s3://"):
            import s3fs
            with s3fs.S3FileSystem().open(only_keys, "r") as f:
                only_set = {ln.strip() for ln in f if ln.strip()}
        else:
            only_set = {ln.strip() for ln in open(only_keys) if ln.strip()}
        logger.warning(f"  RETRY MODE: restricted to {len(only_set):,} keys from {only_keys}")

    # Define columns needed based on note type (must be before schema check)
    if note_type == 'loom':
        # Loom note-text corpus: uniform schema across all sources, carrying the per-patient shift.
        # ShiftedContactDate and de_id_filename are DERIVED here rather than materialised, so we do not
        # rewrite ~1 TB of parquet just to rename columns.
        # note_csn_id and de_id_filename are PRECOMPUTED by loom/note_corpus_keys.py and MUST be read,
        # not derived. Deriving de_id_filename as person_id_note_id reintroduces the collisions the keying
        # pass exists to remove (31.6M cross-source + ~149M duplicate-id rows), which would silently make
        # different notes share a surrogate mapping.
        NEED_COLS = ['person_id', 'note_id', 'note_datetime', 'note_text',
                     'note_csn_id', 'de_id_filename']
        # Optional: the patient's own name, carried alongside the note so philter can surrogate it by
        # IDENTITY instead of guessing. Absent in older corpus builds -> falls back to NER-only, which
        # measured an 8.91% patient-name leak.
        NAME_COLS = ['patient_first_name', 'patient_middle_name', 'patient_last_name']
        batch_fn = _process_batch
    elif note_type == 'bi_clinicalnotes':
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
    # Per-note date-shift column (canonical per-patient offset): read if present, else use default_shift.
    have_name_cols = note_type == 'loom' and all(c in cols for c in NAME_COLS)
    # SIDE-CAR NAME LOOKUP. The keyed corpus is ~944M rows / ~2.26 TB of note text; rewriting all of it
    # just to carry three name columns costs hours of compute to add a few hundred MB of data. Load the
    # names once here instead (12.4M patients, ~150 MB parquet) and attach them per row, so an existing
    # corpus gains known-name injection with no rebuild.
    names_lookup = None
    if note_type == 'loom' and not have_name_cols and names_path:
        import pyarrow.dataset as _ds
        _t = _ds.dataset(names_path, format='parquet').to_table(
            columns=['person_id', 'first_name', 'middle_name', 'last_name'])
        names_lookup = {str(k): (a, b, c) for k, a, b, c in
                        zip(_t.column('person_id').to_pylist(), _t.column('first_name').to_pylist(),
                            _t.column('middle_name').to_pylist(), _t.column('last_name').to_pylist())}
        del _t
        logger.warning(f"  Known-name lookup loaded: {len(names_lookup):,} patients")
    if note_type == 'loom':
        if have_name_cols:
            NEED_COLS = NEED_COLS + NAME_COLS
            logger.warning("  Known-name injection ON (name columns on the corpus)")
        elif names_lookup:
            logger.warning("  Known-name injection ON (side-car lookup)")
        else:
            logger.warning("  Known-name injection OFF -- no name columns and no --names-path; "
                           "names rely on NER only (measured 8.91% patient-name leak)")
    have_shift_col = bool(shift_col) and shift_col in cols
    if surrogate_mode:
        if have_shift_col:
            NEED_COLS = NEED_COLS + [shift_col]
            logger.warning(f"  Surrogate mode ON; per-note date shift from column '{shift_col}'")
        else:
            logger.warning(f"  Surrogate mode ON; in-text dates shifted by default {default_shift} days "
                           f"({'no shift column present' if shift_col else 'no --shift-col given'})")
    else:
        logger.warning("  Surrogate mode OFF (legacy asterisk redaction)")
    logger.warning(f"  Columns: {cols}")
    del first_table

    # Initialize worker pool
    logger.warning(f"\nInitializing {num_workers} workers...")
    init_start = datetime.now()
    pool = Pool(processes=num_workers, initializer=_init_worker,
                initargs=(philter_config, surrogate_mode, default_shift, batch_timeout),
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
            if only_set is not None:
                _key = 'de_id_filename' if 'de_id_filename' in df_chunk.columns else filename_col
                df_chunk = df_chunk[df_chunk[_key].astype(str).isin(only_set)]
                if df_chunk.empty:
                    continue
            chunk_records = len(df_chunk)
            chunk_gb = df_chunk.memory_usage(deep=True).sum() / 1024**3
            logger.warning(f"  Chunk {chunk_num}/{num_chunks}: {chunk_records:,} records ({chunk_gb:.1f} GB)")

            # per-note shift column (or a column of None -> workers apply the default shift)
            shift_vals = df_chunk[shift_col].values if have_shift_col else [None] * len(df_chunk)
            if note_type == 'loom':
                _shift = pd.to_numeric(df_chunk[shift_col], errors='coerce').fillna(0).astype(int) \
                         if have_shift_col else pd.Series([0] * len(df_chunk))
                _shifted_date = (pd.to_datetime(df_chunk['note_datetime'], errors='coerce')
                                 + pd.to_timedelta(_shift, unit='D')).dt.date
                file_data = list(zip(
                    df_chunk['note_csn_id'].values,      # PRECOMPUTED unique note key
                    df_chunk['person_id'].values,        # BDSPPatientID
                    [None] * len(df_chunk),              # bdsp_encounter_id (not carried in corpus)
                    _shifted_date.values,                # ShiftedContactDate = real + ShiftedDays
                    df_chunk['note_text'].values,        # text
                    df_chunk['de_id_filename'].values,   # PRECOMPUTED surrogate key — never derive here
                    shift_vals,
                    list(zip(df_chunk['patient_first_name'].values,
                             df_chunk['patient_middle_name'].values,
                             df_chunk['patient_last_name'].values))
                    if have_name_cols else
                    ([names_lookup.get(str(p)) for p in df_chunk['person_id'].values]
                     if names_lookup else [None] * len(df_chunk))
                ))
            elif note_type == 'bi_clinicalnotes':
                file_data = list(zip(
                    df_chunk[id_col].values,
                    df_chunk['TYPE'].values,
                    df_chunk['SHIFTED_CREATIONINSTANT'].values,
                    df_chunk['COUNT'].values,
                    df_chunk[text_col].values,
                    df_chunk[filename_col].values,
                    shift_vals
                ))
            else:
                file_data = list(zip(
                    df_chunk[id_col].values,
                    df_chunk['BDSPPatientID'].values,
                    df_chunk['bdsp_encounter_id'].values,
                    df_chunk['ShiftedContactDate'].values,
                    df_chunk[text_col].values,
                    df_chunk[filename_col].values,
                    shift_vals
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
            # Persist the notes destroyed by the timeout path (every alphanumeric -> '*') so a second
            # pass with a larger --batch-timeout can redo them, instead of shipping garbage silently.
            write_retry_queue(all_status_records, output_path, partition_id)
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
    parser.add_argument('--note-type', choices=['clinicalnotes', 'imagingreport', 'bi_clinicalnotes', 'loom'], default='clinicalnotes',
                        help='Type of notes: clinicalnotes (NoteTXT/NoteCSNID), imagingreport (ReportTXT/OrderProcedureID), or bi_clinicalnotes (TEXT/CLINICALNOTETEXTKEY)')
    parser.add_argument('--surrogate', dest='surrogate', action='store_true', default=True,
                        help='Replace PHI with deterministic fakes + shifted dates (default)')
    parser.add_argument('--no-surrogate', dest='surrogate', action='store_false',
                        help='Legacy behavior: redact PHI with asterisks')
    parser.add_argument('--default-shift', type=int, default=0,
                        help='Days to shift in-text dates when no per-note shift column is present')
    parser.add_argument('--batch-timeout', type=int, default=60,
                        help='Seconds per sub-batch before FULL REDACTION (destroys the note). '
                             'Raise it for slow/long notes; timed-out keys are written to _retry/ for a second pass.')
    parser.add_argument('--names-path', default=None,
                        help="Parquet of person_id -> first/middle/last name, used to surrogate the "
                             "PATIENT'S OWN name by identity. Without it names rest on NER alone, "
                             "measured at an 8.91%% patient-name leak on a 2,000-note sample.")
    parser.add_argument('--only-keys', default=None,
                        help='Path (local or s3://) to a newline-separated list of de_id_filename values. '
                             'Process ONLY those notes — used to retry the _retry/ timeout queue.')
    parser.add_argument('--shift-col', default=None,
                        help='Parquet column holding the per-note (canonical per-patient) date-shift in days')

    args = parser.parse_args()

    if args.note_type == 'loom':
        id_col = 'note_id'
        text_col = 'note_text'
        filename_col = 'de_id_filename'      # DERIVED in process_partition, not read from the file
    elif args.note_type == 'imagingreport':
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

    # Preflight: prove the surrogate machinery works before touching any data. Without this the
    # asterisk fallback silently produces a redacted corpus that still passes row-count and
    # PHI-pattern checks.
    if args.surrogate:
        try:
            _preflight_surrogate(args.philter_config)
            logger.warning("  Surrogate preflight OK (name lists loaded)")
        except Exception as e:
            logger.error(f"SURROGATE PREFLIGHT FAILED: {type(e).__name__}: {e}")
            logger.error("Refusing to run: output would silently be asterisk-redacted, not surrogate-replaced.")
            logger.error("Most likely cause: the NLTK 'names' corpus is not installed "
                         "(python -m nltk.downloader names).")
            sys.exit(3)

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
        batch_timeout=args.batch_timeout,
        only_keys=args.only_keys,
        names_path=args.names_path,
        filename_col=filename_col,
        surrogate_mode=args.surrogate,
        default_shift=args.default_shift,
        shift_col=args.shift_col,
    )


if __name__ == "__main__":
    main()
