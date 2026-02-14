"""
Test run de-identification on 500 rows from the database.
"""
import pyodbc
import logging
from datetime import datetime
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from philter import Philter
import keyword_removal
from philter_helper import run_philter_on_dict

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


def _deidentify_batch(records, philter_config_path):
    """
    De-identify a batch of records using Philter.

    Args:
        records: List of (note_id, de_id_name, report_text, shifted_year) tuples
        philter_config_path: Path to Philter configuration

    Returns:
        List of (note_id, deid_text, de_id_name, shifted_year, success) tuples
    """
    # Note: Philter will be initialized in the helper function for each batch
    logger.info(f"Processing batch with Philter config: {philter_config_path}")

    deid_to_record = {}
    texts_to_process = []

    for note_id, de_id_name, report_text, shifted_year in records:
        if not report_text or len(str(report_text).strip()) == 0:
            continue

        deid_key = de_id_name
        deid_to_record[deid_key] = (note_id, shifted_year)

        try:
            cleaned = keyword_removal.remove_keywords(str(report_text))
            texts_to_process.append((deid_key, cleaned))
        except Exception as e:
            # Skip records that fail keyword removal
            continue

    if not texts_to_process:
        return []

    try:
        philter_results = run_philter_on_dict(dict(texts_to_process), philter_config_path)
    except Exception as e:
        logger.error(f"Philter processing failed: {e}")
        # Return failed results for all texts in this batch
        return [(deid_to_record[k][0], "", k, deid_to_record[k][1], False)
                for k, _ in texts_to_process]

    results = []
    for deid_key, deid_text in philter_results.items():
        note_id, shifted_year = deid_to_record[deid_key]
        success = True
        results.append((note_id, deid_text, deid_key, shifted_year, success))

    return results


def main():
    # Configuration
    server = "172.18.160.211,1433"
    database = "bdsp_prod"
    username = "bdsp"
    password = "$Spikewave2022!"

    source_table = "bdsp_prod.Clinical.to_deIdentify_notes"
    note_id_column = "NoteCSNID"
    deid_name_column = "DeIDNoteID"
    text_column = "NoteTXT"
    shifted_year_column = "ShiftedContactYear"

    philter_config = "configs/philter_one.json"
    num_workers = 4
    limit = 500

    logger.info("=" * 60)
    logger.info("Test Run: De-identifying 500 records")
    logger.info("=" * 60)
    logger.info(f"Server: {server}")
    logger.info(f"Database: {database}")
    logger.info(f"Source: {source_table}")
    logger.info(f"Limit: {limit} records")
    logger.info(f"Workers: {num_workers}")
    logger.info(f"Philter config: {philter_config}")
    logger.info("=" * 60)

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
        sys.exit(1)

    cursor = conn.cursor()

    # Fetch 500 records
    logger.info(f"Fetching {limit} records from {source_table}...")
    query = f"""
        SELECT TOP {limit}
            [{note_id_column}],
            [{deid_name_column}],
            [{text_column}],
            [{shifted_year_column}]
        FROM {source_table}
        WHERE [{text_column}] IS NOT NULL
          AND DATALENGTH([{text_column}]) > 0
        ORDER BY [{note_id_column}]
    """

    cursor.execute(query)
    records = cursor.fetchall()
    logger.info(f"✓ Fetched {len(records)} records")

    if len(records) == 0:
        logger.warning("No records to process")
        cursor.close()
        conn.close()
        return

    # Show sample of input
    logger.info("\nSample input (first record):")
    logger.info(f"  NoteCSNID: {records[0][0]}")
    logger.info(f"  DeIDNoteID: {records[0][1]}")
    logger.info(f"  Text length: {len(records[0][2])} characters")
    logger.info(f"  Text preview: {records[0][2][:100]}...")
    logger.info(f"  ShiftedContactYear: {records[0][3]}")

    # Process records (single-threaded for test)
    logger.info(f"\nProcessing {len(records)} records...")
    start_time = datetime.now()

    all_results = []
    successful = 0
    failed = 0

    # Process in single thread to avoid multiprocessing issues
    try:
        batch_results = _deidentify_batch(records, philter_config)
        all_results.extend(batch_results)
        for result in batch_results:
            if result[4]:  # success flag
                successful += 1
            else:
                failed += 1
    except Exception as e:
        logger.error(f"Processing failed: {e}")
        import traceback
        traceback.print_exc()
        failed = len(records)

    elapsed = (datetime.now() - start_time).total_seconds()

    logger.info(f"\n✓ Processing complete")
    logger.info(f"  Time: {elapsed:.1f}s ({len(records)/elapsed:.1f} records/sec)")
    logger.info(f"  Successful: {successful}")
    logger.info(f"  Failed: {failed}")

    # Show sample of output
    if all_results:
        logger.info("\nSample output (first record):")
        logger.info(f"  NoteCSNID: {all_results[0][0]}")
        logger.info(f"  DeIDNoteID: {all_results[0][2]}")
        logger.info(f"  De-identified text length: {len(all_results[0][1])} characters")
        logger.info(f"  De-identified text preview: {all_results[0][1][:100]}...")
        logger.info(f"  ShiftedContactYear: {all_results[0][3]}")

        # Save results to file
        output_file = f"test_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write("Test De-identification Results\n")
            f.write("=" * 80 + "\n\n")
            for i, (note_id, deid_text, de_id_name, shifted_year, success) in enumerate(all_results[:10], 1):
                f.write(f"Record {i}:\n")
                f.write(f"  NoteCSNID: {note_id}\n")
                f.write(f"  DeIDNoteID: {de_id_name}\n")
                f.write(f"  ShiftedContactYear: {shifted_year}\n")
                f.write(f"  Success: {success}\n")
                f.write(f"  De-identified text:\n")
                f.write(f"  {deid_text[:500]}...\n")
                f.write("\n" + "=" * 80 + "\n\n")

        logger.info(f"\n✓ Sample results saved to: {output_file}")

    cursor.close()
    conn.close()
    logger.info("\n✓ Test complete!")


if __name__ == "__main__":
    main()
