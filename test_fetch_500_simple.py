"""
Test to fetch 500 rows, de-identify with full Philter, and insert to target table.
(Using keyword_removal + full Philter pipeline for comprehensive de-identification)
"""
import pyodbc
import logging
from datetime import datetime
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
    limit = 500

    logger.info("=" * 60)
    logger.info("Test: Fetch, de-identify, and insert 500 records")
    logger.info("=" * 60)
    logger.info(f"Server: {server}")
    logger.info(f"Database: {database}")
    logger.info(f"Source: {source_table}")
    logger.info(f"Target: {output_table}")
    logger.info(f"Limit: {limit} records")
    logger.info(f"De-identification: Philter (330 patterns) + keyword_removal")
    logger.info("=" * 60)

    # Note: Philter initialization will happen per-text in the helper function
    logger.info("\nPhilter will be used for de-identification")
    logger.info(f"  Config: {philter_config_path}")
    logger.info(f"  Method: keyword_removal + Philter (330 patterns)")

    # Verify config file exists
    import os
    if not os.path.exists(philter_config_path):
        logger.error(f"✗ Philter config not found: {philter_config_path}")
        return

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

    # Fetch 500 records
    logger.info(f"\nFetching {limit} records...")
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

    start_time = datetime.now()
    cursor.execute(query)
    records = cursor.fetchall()
    elapsed = (datetime.now() - start_time).total_seconds()

    logger.info(f"✓ Fetched {len(records)} records in {elapsed:.2f}s")

    if len(records) == 0:
        logger.warning("No records found")
        cursor.close()
        conn.close()
        return

    # Check which records are already de-identified
    logger.info("\n" + "=" * 60)
    logger.info("Checking for already de-identified records")
    logger.info("=" * 60)

    # Get set of already processed NoteCSNIDs
    note_ids = [r[0] for r in records]
    placeholders = ','.join('?' * len(note_ids))

    cursor.execute(f"""
        SELECT NoteCSNID
        FROM {output_table}
        WHERE NoteCSNID IN ({placeholders})
    """, note_ids)

    already_processed = set(row[0] for row in cursor.fetchall())
    logger.info(f"Found {len(already_processed)} records already de-identified")
    logger.info(f"Need to process {len(records) - len(already_processed)} new records")

    # De-identify and insert only new records
    logger.info("\n" + "=" * 60)
    logger.info("De-identifying and inserting new records")
    logger.info("=" * 60)

    insert_count = 0
    error_count = 0
    skipped_existing = len(already_processed)
    skipped_empty = 0
    start_time = datetime.now()

    for i, record in enumerate(records, 1):
        note_id, deid_name, text, shifted_year = record

        # Skip if already processed
        if note_id in already_processed:
            continue

        # Skip empty texts
        if not text or len(str(text).strip()) == 0:
            skipped_empty += 1
            continue

        try:
            # Apply keyword removal first
            cleaned_text = keyword_removal.remove_keywords(str(text))

            # Apply Philter de-identification using helper function
            philter_results = run_philter_on_dict({deid_name: cleaned_text}, philter_config_path)
            deid_text = philter_results.get(deid_name, cleaned_text)

            # Insert the de-identified record
            cursor.execute(f"""
                INSERT INTO {output_table}
                (NoteCSNID, NoteTXT, DeIDNoteID, ShiftedContactYear)
                VALUES (?, ?, ?, ?)
            """, (note_id, deid_text, deid_name, shifted_year))
            insert_count += 1

            # Commit every 100 records
            if insert_count % 100 == 0:
                conn.commit()
                logger.info(f"  Processed {i}/{len(records)}, inserted {insert_count} de-identified records...")

        except Exception as e:
            logger.error(f"Error processing record {note_id}: {e}")
            error_count += 1

    # Final commit
    conn.commit()
    elapsed = (datetime.now() - start_time).total_seconds()

    logger.info(f"\n✓ Processing complete")
    logger.info(f"  De-identified and inserted: {insert_count} records")
    logger.info(f"  Skipped (already processed): {skipped_existing}")
    logger.info(f"  Skipped (empty text): {skipped_empty}")
    logger.info(f"  Errors: {error_count}")
    if insert_count > 0:
        logger.info(f"  Time: {elapsed:.2f}s ({insert_count/elapsed:.1f} records/sec)")
    else:
        logger.info(f"  Time: {elapsed:.2f}s (no new records to process)")

    # Verify inserted count
    cursor.execute(f"SELECT COUNT(*) FROM {output_table}")
    total_count = cursor.fetchone()[0]
    logger.info(f"  Total records in target table: {total_count:,}")

    # Show sample de-identified output
    logger.info("\n" + "=" * 60)
    logger.info("Sample de-identified output")
    logger.info("=" * 60)

    cursor.execute(f"""
        SELECT TOP 3 NoteCSNID, DeIDNoteID, LEFT(NoteTXT, 300) as sample_text
        FROM {output_table}
        ORDER BY NoteCSNID
    """)

    for idx, row in enumerate(cursor.fetchall(), 1):
        logger.info(f"\nRecord {idx}:")
        logger.info(f"  NoteCSNID: {row[0]}")
        logger.info(f"  DeIDNoteID: {row[1]}")
        logger.info(f"  Text preview: {row[2]}...")

    cursor.close()
    conn.close()
    logger.info("\n✓ Test complete!")
    logger.info("\nNote: You can delete these test records manually with:")
    logger.info(f"  DELETE FROM {output_table} WHERE NoteCSNID IN (SELECT TOP {limit} NoteCSNID FROM {source_table} ORDER BY NoteCSNID)")


if __name__ == "__main__":
    main()
