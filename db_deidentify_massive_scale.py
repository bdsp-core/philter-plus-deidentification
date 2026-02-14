"""
Massive-scale de-identification for 500M+ records.
Optimized for maximum throughput on high-end hardware.

Configuration for 192GB RAM, 32 cores/64 threads:
- 60 parallel workers
- Large batch sizes
- Minimal logging overhead
- Checkpoint-based resume capability
"""
import pyodbc
import logging
from datetime import datetime
from multiprocessing import Pool, cpu_count, Manager
import keyword_removal
from philter import Philter
import os
import json

# Setup logging - minimal overhead
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Global variables for worker processes
_philter_instance = None
_philter_config_path = None
_worker_stats = {}


def _init_worker(config_path):
    """Initialize Philter once per worker process."""
    global _philter_instance, _philter_config_path
    _philter_config_path = config_path
    pid = os.getpid()

    try:
        philter_config = {
            "filters": config_path,
            "phi_text": {},
            "filenames": [],
            "verbose": False,
            "run_eval": False
        }
        _philter_instance = Philter(philter_config)
        # Only log from first worker to reduce noise
        if pid % 10 == 0:
            logger.info(f"Worker {pid}: Initialized with {len(_philter_instance.patterns)} patterns")
    except Exception as e:
        logger.error(f"Worker {pid}: Initialization failed: {e}")
        raise


def _process_batch(batch_records):
    """
    Process a batch of records. Optimized for speed.

    Args:
        batch_records: List of (note_id, deid_name, text, shifted_year) tuples

    Returns:
        List of (note_id, deid_text, deid_name, shifted_year, success) tuples
    """
    global _philter_instance

    if _philter_instance is None:
        return [(r[0], "", r[1], r[3], False) for r in batch_records]

    results = []
    texts_dict = {}
    record_map = {}

    # Prepare batch - keyword removal
    for note_id, deid_name, text, shifted_year in batch_records:
        if not text or len(str(text).strip()) == 0:
            results.append((note_id, "", deid_name, shifted_year, False))
            continue

        try:
            cleaned_text = keyword_removal.remove_keywords(str(text))
            texts_dict[deid_name] = cleaned_text
            record_map[deid_name] = (note_id, shifted_year)
        except:
            results.append((note_id, "", deid_name, shifted_year, False))

    if not texts_dict:
        return results

    # Process batch with Philter
    try:
        _philter_instance.texts = texts_dict
        _philter_instance.filenames = list(texts_dict.keys())
        _philter_instance.map_coordinates()

        for deid_name in texts_dict.keys():
            try:
                deid_text = _philter_instance.transform_text_asterisk(texts_dict[deid_name], deid_name)
                note_id, shifted_year = record_map[deid_name]
                results.append((note_id, deid_text, deid_name, shifted_year, True))
            except:
                note_id, shifted_year = record_map[deid_name]
                results.append((note_id, "", deid_name, shifted_year, False))

    except Exception as e:
        for deid_name, (note_id, shifted_year) in record_map.items():
            results.append((note_id, "", deid_name, shifted_year, False))

    return results


def save_checkpoint(checkpoint_file, last_note_id, total_processed, total_inserted):
    """Save progress checkpoint."""
    checkpoint = {
        'last_note_id': last_note_id,
        'total_processed': total_processed,
        'total_inserted': total_inserted,
        'timestamp': datetime.now().isoformat()
    }
    with open(checkpoint_file, 'w') as f:
        json.dump(checkpoint, f, indent=2)


def load_checkpoint(checkpoint_file):
    """Load progress checkpoint."""
    if os.path.exists(checkpoint_file):
        with open(checkpoint_file, 'r') as f:
            return json.load(f)
    return None


def main():
    # ============================================================================
    # CONFIGURATION - Tuned for 500M records
    # ============================================================================

    # Database connection
    server = "172.18.160.211,1433"
    database = "bdsp_prod"
    username = "bdsp"
    password = "$Spikewave2022!"

    source_table = "bdsp_prod.Clinical.to_deIdentify_notes"
    output_table = "bdsp_opendata.Clinical.bdsp_notes_deid"
    note_id_column = "NoteCSNID"
    deid_name_column = "DeIDNoteID"
    text_column = "NoteTXT"
    shifted_year_column = "ShiftedContactYear"

    philter_config_path = "configs/philter_one.json"

    # ============================================================================
    # PERFORMANCE TUNING - AGGRESSIVE SETTINGS FOR 500M RECORDS
    # ============================================================================

    # Workers: Use 60 workers (leaves 4 cores for DB/OS)
    # Each worker uses ~800MB-1.5GB RAM
    # 60 workers × 1.5GB = 90GB max (leaves 102GB for OS/DB)
    num_workers = 60

    # Batch size: 150 records per worker batch
    # Larger batches = better Philter efficiency
    # Balance between efficiency and worker memory usage
    batch_size = 150

    # Database fetch: 20,000 records at a time
    # With 500M records, minimize DB round-trips
    # 20K records × 2KB avg = ~40MB per fetch
    fetch_batch_size = 20000

    # Commit frequency: Every 1000 records
    # Less frequent commits = faster processing
    # Trade-off: if crash, lose up to 1000 records of work
    commit_every = 1000

    # Checkpoint: Save progress every 100K records
    # Allows safe resume for very long runs
    checkpoint_every = 100000
    checkpoint_file = "deidentify_checkpoint.json"

    # Progress reporting: Every 50K records
    # Reduce logging overhead
    report_every = 50000

    # ============================================================================

    logger.info("=" * 80)
    logger.info("MASSIVE-SCALE De-identification Pipeline")
    logger.info("=" * 80)
    logger.info(f"Target: 500,000,000 records")
    logger.info(f"")
    logger.info("Database:")
    logger.info(f"  Server: {server}")
    logger.info(f"  Database: {database}")
    logger.info(f"  Source: {source_table}")
    logger.info(f"  Target: {output_table}")
    logger.info(f"")
    logger.info("Performance Configuration:")
    logger.info(f"  Available CPU cores: {cpu_count()}")
    logger.info(f"  Parallel workers: {num_workers}")
    logger.info(f"  Batch size per worker: {batch_size}")
    logger.info(f"  Database fetch size: {fetch_batch_size:,}")
    logger.info(f"  Commit frequency: every {commit_every:,} records")
    logger.info(f"  Checkpoint frequency: every {checkpoint_every:,} records")
    logger.info(f"")
    logger.info("Estimated Performance:")
    logger.info(f"  Conservative: 80 rec/sec = 72 days for 500M")
    logger.info(f"  Realistic: 100 rec/sec = 58 days for 500M")
    logger.info(f"  Optimistic: 120 rec/sec = 48 days for 500M")
    logger.info("=" * 80)

    # Check for checkpoint
    checkpoint = load_checkpoint(checkpoint_file)
    if checkpoint:
        logger.info(f"\n✓ Checkpoint found from {checkpoint['timestamp']}")
        logger.info(f"  Last processed NoteCSNID: {checkpoint['last_note_id']}")
        logger.info(f"  Total processed: {checkpoint['total_processed']:,}")
        logger.info(f"  Total inserted: {checkpoint['total_inserted']:,}")
        resume = input("\nResume from checkpoint? (y/n): ").strip().lower()
        if resume != 'y':
            checkpoint = None
            logger.info("Starting fresh...")

    # Connect to database
    try:
        connection_string = (
            f"DRIVER={{SQL Server}};"
            f"SERVER={server};"
            f"DATABASE={database};"
            f"UID={username};"
            f"PWD={password}"
        )
        conn = pyodbc.connect(connection_string, timeout=30)
        logger.info("\n✓ Connected to database")
    except Exception as e:
        logger.error(f"\n✗ Connection failed: {e}")
        return

    cursor = conn.cursor()

    # Get total count
    logger.info("\nCounting total records...")
    cursor.execute(f"""
        SELECT COUNT(*)
        FROM {source_table}
        WHERE [{text_column}] IS NOT NULL
          AND DATALENGTH([{text_column}]) > 0
    """)
    total_records = cursor.fetchone()[0]
    logger.info(f"Total records in source: {total_records:,}")

    # Initialize counters
    if checkpoint:
        last_note_id = checkpoint['last_note_id']
        total_processed = checkpoint['total_processed']
        total_inserted = checkpoint['total_inserted']
        total_failed = 0
        total_skipped = 0
    else:
        last_note_id = 0
        total_processed = 0
        total_inserted = 0
        total_failed = 0
        total_skipped = 0

    # Initialize worker pool
    logger.info(f"\nInitializing {num_workers} workers...")
    logger.info("(This may take 30-60 seconds...)")
    init_start = datetime.now()
    pool = Pool(processes=num_workers, initializer=_init_worker, initargs=(philter_config_path,))
    init_elapsed = (datetime.now() - init_start).total_seconds()
    logger.info(f"✓ Worker pool ready in {init_elapsed:.1f}s")

    logger.info("\n" + "=" * 80)
    logger.info("PROCESSING STARTED")
    logger.info("=" * 80)

    start_time = datetime.now()
    last_report_count = total_processed
    last_report_time = start_time

    try:
        while True:
            # Fetch batch from database
            query = f"""
                SELECT TOP {fetch_batch_size}
                    [{note_id_column}],
                    [{deid_name_column}],
                    [{text_column}],
                    [{shifted_year_column}]
                FROM {source_table}
                WHERE [{note_id_column}] > ?
                  AND [{text_column}] IS NOT NULL
                  AND DATALENGTH([{text_column}]) > 0
                ORDER BY [{note_id_column}]
            """
            cursor.execute(query, (last_note_id,))
            records = cursor.fetchall()

            if not records:
                logger.info("\n✓ No more records to process")
                break

            last_note_id = records[-1][0]

            # Check which are already processed
            note_ids = [r[0] for r in records]
            placeholders = ','.join('?' * len(note_ids))
            cursor.execute(f"""
                SELECT NoteCSNID
                FROM {output_table}
                WHERE NoteCSNID IN ({placeholders})
            """, note_ids)
            already_done = set(row[0] for row in cursor.fetchall())

            # Filter out already processed
            records_to_process = [r for r in records if r[0] not in already_done]
            total_skipped += len(already_done)

            if not records_to_process:
                continue

            # Split into batches for parallel processing
            batches = []
            for i in range(0, len(records_to_process), batch_size):
                batches.append(records_to_process[i:i + batch_size])

            # Process batches in parallel
            batch_results = pool.map(_process_batch, batches)

            # Insert results
            batch_inserted = 0
            batch_failed = 0

            for results in batch_results:
                for note_id, deid_text, deid_name, shifted_year, success in results:
                    if success and deid_text:
                        try:
                            cursor.execute(f"""
                                INSERT INTO {output_table}
                                (NoteCSNID, NoteTXT, DeIDNoteID, ShiftedContactYear)
                                VALUES (?, ?, ?, ?)
                            """, (note_id, deid_text, deid_name, shifted_year))
                            batch_inserted += 1

                            if batch_inserted % commit_every == 0:
                                conn.commit()
                        except:
                            batch_failed += 1
                    else:
                        batch_failed += 1

            # Final commit for this batch
            conn.commit()

            total_processed += len(records_to_process)
            total_inserted += batch_inserted
            total_failed += batch_failed

            # Checkpoint
            if total_processed % checkpoint_every < len(records_to_process):
                save_checkpoint(checkpoint_file, last_note_id, total_processed, total_inserted)

            # Progress reporting
            if total_processed % report_every < len(records_to_process) or total_processed < report_every:
                now = datetime.now()
                elapsed = (now - start_time).total_seconds()
                recent_elapsed = (now - last_report_time).total_seconds()
                recent_count = total_processed - last_report_count

                overall_rate = total_processed / elapsed if elapsed > 0 else 0
                recent_rate = recent_count / recent_elapsed if recent_elapsed > 0 else 0

                remaining = total_records - total_processed
                eta_seconds = remaining / overall_rate if overall_rate > 0 else 0
                eta_hours = eta_seconds / 3600
                eta_days = eta_hours / 24

                progress_pct = (total_processed / total_records * 100) if total_records > 0 else 0

                logger.info(f"")
                logger.info(f"Progress: {total_processed:,} / {total_records:,} ({progress_pct:.2f}%)")
                logger.info(f"  Inserted: {total_inserted:,} | Failed: {total_failed:,} | Skipped: {total_skipped:,}")
                logger.info(f"  Speed: {overall_rate:.1f} rec/sec (overall) | {recent_rate:.1f} rec/sec (recent)")
                logger.info(f"  Elapsed: {elapsed/3600:.1f} hours | ETA: {eta_days:.1f} days")

                last_report_count = total_processed
                last_report_time = now

    except KeyboardInterrupt:
        logger.warning("\n\n⚠ Interrupted by user")
        logger.info("Saving checkpoint...")
        save_checkpoint(checkpoint_file, last_note_id, total_processed, total_inserted)
        logger.info("✓ Checkpoint saved. You can resume later.")
    except Exception as e:
        logger.error(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        logger.info("\nSaving checkpoint...")
        save_checkpoint(checkpoint_file, last_note_id, total_processed, total_inserted)
    finally:
        logger.info("\nShutting down worker pool...")
        pool.close()
        pool.join()

    # Final summary
    elapsed = (datetime.now() - start_time).total_seconds()

    logger.info("\n" + "=" * 80)
    logger.info("FINAL SUMMARY")
    logger.info("=" * 80)
    logger.info(f"Total time: {elapsed/3600:.1f} hours ({elapsed/3600/24:.1f} days)")
    logger.info(f"Records processed: {total_processed:,}")
    logger.info(f"Successfully inserted: {total_inserted:,}")
    logger.info(f"Skipped (already done): {total_skipped:,}")
    logger.info(f"Failed: {total_failed:,}")
    if total_processed > 0:
        logger.info(f"Average speed: {total_processed/elapsed:.1f} records/sec")

    # Verify final count
    cursor.execute(f"SELECT COUNT(*) FROM {output_table}")
    final_count = cursor.fetchone()[0]
    logger.info(f"Total records in target table: {final_count:,}")

    cursor.close()
    conn.close()

    # Remove checkpoint if complete
    if total_processed >= total_records:
        if os.path.exists(checkpoint_file):
            os.remove(checkpoint_file)
        logger.info("\n✓ COMPLETE! All records processed.")
    else:
        logger.info(f"\n✓ Partial run complete. Resume anytime to continue.")


if __name__ == "__main__":
    main()
