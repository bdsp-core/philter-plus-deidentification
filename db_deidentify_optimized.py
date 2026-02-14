"""
Optimized de-identification for BDSP database with parallel processing.
Configured for high-performance on multi-core systems.
"""
import pyodbc
import logging
from datetime import datetime
from multiprocessing import Pool, cpu_count
import keyword_removal
from philter import Philter
import os

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# Global variable for worker processes
_philter_instance = None
_philter_config_path = None


def _init_worker(config_path):
    """Initialize Philter once per worker process."""
    global _philter_instance, _philter_config_path
    _philter_config_path = config_path
    logger.info(f"Worker {os.getpid()}: Initializing Philter...")
    try:
        philter_config = {
            "filters": config_path,
            "phi_text": {},
            "filenames": [],
            "verbose": False,
            "run_eval": False
        }
        _philter_instance = Philter(philter_config)
        logger.info(f"Worker {os.getpid()}: Philter initialized with {len(_philter_instance.patterns)} patterns")
    except Exception as e:
        logger.error(f"Worker {os.getpid()}: Failed to initialize Philter: {e}")
        raise


def _process_batch(batch_records):
    """
    Process a batch of records in a worker process.
    Philter is initialized once per worker and reused for all batches.

    Args:
        batch_records: List of (note_id, deid_name, text, shifted_year) tuples

    Returns:
        List of (note_id, deid_text, deid_name, shifted_year, success) tuples
    """
    global _philter_instance

    if _philter_instance is None:
        logger.error(f"Worker {os.getpid()}: Philter not initialized!")
        return [(r[0], "", r[1], r[3], False) for r in batch_records]

    results = []
    texts_dict = {}
    record_map = {}

    # Prepare batch
    for note_id, deid_name, text, shifted_year in batch_records:
        if not text or len(str(text).strip()) == 0:
            results.append((note_id, "", deid_name, shifted_year, False))
            continue

        try:
            # Apply keyword removal
            cleaned_text = keyword_removal.remove_keywords(str(text))
            texts_dict[deid_name] = cleaned_text
            record_map[deid_name] = (note_id, shifted_year)
        except Exception as e:
            logger.error(f"Worker {os.getpid()}: Keyword removal failed for {note_id}: {e}")
            results.append((note_id, "", deid_name, shifted_year, False))

    if not texts_dict:
        return results

    # Process entire batch with Philter
    try:
        # Update Philter's text dictionary
        _philter_instance.texts = texts_dict
        _philter_instance.filenames = list(texts_dict.keys())

        # Map coordinates
        _philter_instance.map_coordinates()

        # Transform each text
        for deid_name in texts_dict.keys():
            try:
                deid_text = _philter_instance.transform_text_asterisk(texts_dict[deid_name], deid_name)
                note_id, shifted_year = record_map[deid_name]
                results.append((note_id, deid_text, deid_name, shifted_year, True))
            except Exception as e:
                logger.error(f"Worker {os.getpid()}: Transform failed for {deid_name}: {e}")
                note_id, shifted_year = record_map[deid_name]
                results.append((note_id, "", deid_name, shifted_year, False))

    except Exception as e:
        logger.error(f"Worker {os.getpid()}: Batch processing failed: {e}")
        for deid_name, (note_id, shifted_year) in record_map.items():
            results.append((note_id, "", deid_name, shifted_year, False))

    return results


def main():
    # Configuration
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

    # Performance tuning parameters
    # With 32 cores/64 threads, use 40 workers (leave some for DB/OS)
    num_workers = 40
    # Process records in batches - larger batches = better Philter efficiency
    batch_size = 100
    # Database fetch size
    fetch_batch_size = 5000
    # Commit frequency
    commit_every = 500

    logger.info("=" * 80)
    logger.info("OPTIMIZED De-identification Pipeline")
    logger.info("=" * 80)
    logger.info(f"Server: {server}")
    logger.info(f"Database: {database}")
    logger.info(f"Source: {source_table}")
    logger.info(f"Target: {output_table}")
    logger.info(f"")
    logger.info("Performance Configuration:")
    logger.info(f"  CPU cores available: {cpu_count()}")
    logger.info(f"  Parallel workers: {num_workers}")
    logger.info(f"  Batch size per worker: {batch_size}")
    logger.info(f"  Database fetch size: {fetch_batch_size}")
    logger.info(f"  Commit frequency: {commit_every} records")
    logger.info("=" * 80)

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
        logger.info("✓ Connected to database")
    except Exception as e:
        logger.error(f"✗ Connection failed: {e}")
        return

    cursor = conn.cursor()

    # Get total count
    logger.info("\nCounting total records to process...")
    cursor.execute(f"""
        SELECT COUNT(*)
        FROM {source_table}
        WHERE [{text_column}] IS NOT NULL
          AND DATALENGTH([{text_column}]) > 0
    """)
    total_records = cursor.fetchone()[0]
    logger.info(f"Total records in source: {total_records:,}")

    # Check already processed
    cursor.execute(f"SELECT COUNT(*) FROM {output_table}")
    already_processed = cursor.fetchone()[0]
    logger.info(f"Already processed: {already_processed:,}")
    logger.info(f"Remaining to process: {(total_records - already_processed):,}")

    # Initialize worker pool
    logger.info(f"\nInitializing worker pool with {num_workers} workers...")
    pool = Pool(processes=num_workers, initializer=_init_worker, initargs=(philter_config_path,))
    logger.info("✓ Worker pool initialized")

    # Process in chunks
    total_processed = 0
    total_inserted = 0
    total_failed = 0
    total_skipped = 0
    start_time = datetime.now()
    last_note_id = 0

    try:
        while True:
            # Fetch batch from database
            fetch_start = datetime.now()
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
            fetch_elapsed = (datetime.now() - fetch_start).total_seconds()

            if not records:
                logger.info("\n✓ No more records to fetch")
                break

            logger.info(f"\nFetched {len(records)} records in {fetch_elapsed:.1f}s")
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
            logger.info(f"  Already processed in this batch: {len(already_done)}")
            logger.info(f"  New records to process: {len(records_to_process)}")
            total_skipped += len(already_done)

            if not records_to_process:
                continue

            # Split into batches for parallel processing
            batches = []
            for i in range(0, len(records_to_process), batch_size):
                batches.append(records_to_process[i:i + batch_size])

            logger.info(f"  Processing {len(batches)} batches in parallel...")
            process_start = datetime.now()

            # Process batches in parallel
            batch_results = pool.map(_process_batch, batches)

            process_elapsed = (datetime.now() - process_start).total_seconds()
            logger.info(f"  ✓ Parallel processing complete in {process_elapsed:.1f}s")

            # Insert results
            insert_start = datetime.now()
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

                            # Commit periodically
                            if batch_inserted % commit_every == 0:
                                conn.commit()

                        except Exception as e:
                            logger.error(f"Insert failed for {note_id}: {e}")
                            batch_failed += 1
                    else:
                        batch_failed += 1

            # Final commit for this batch
            conn.commit()
            insert_elapsed = (datetime.now() - insert_start).total_seconds()

            total_processed += len(records_to_process)
            total_inserted += batch_inserted
            total_failed += batch_failed

            # Progress update
            elapsed = (datetime.now() - start_time).total_seconds()
            records_per_sec = total_processed / elapsed if elapsed > 0 else 0

            logger.info(f"  ✓ Inserted {batch_inserted} records in {insert_elapsed:.1f}s")
            logger.info(f"")
            logger.info(f"Progress: {total_processed:,} processed, {total_inserted:,} inserted, {total_failed:,} failed")
            logger.info(f"Speed: {records_per_sec:.1f} records/sec (avg)")

            if total_processed >= total_records:
                break

    except KeyboardInterrupt:
        logger.warning("\n⚠ Interrupted by user")
    except Exception as e:
        logger.error(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        pool.close()
        pool.join()

    # Final summary
    elapsed = (datetime.now() - start_time).total_seconds()

    logger.info("\n" + "=" * 80)
    logger.info("FINAL SUMMARY")
    logger.info("=" * 80)
    logger.info(f"Total time: {elapsed:.1f}s ({elapsed/60:.1f} minutes)")
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
    logger.info("\n✓ Complete!")


if __name__ == "__main__":
    main()
