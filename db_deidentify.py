"""
SQL Server integration for Philter de-identification.

Reads records from a SQL Server source table, de-identifies text in memory
using the Philter pipeline, and writes results to an output table.

Usage:
    python db_deidentify.py \
      --connection-string "DRIVER={ODBC Driver 17 for SQL Server};SERVER=myserver;DATABASE=mydb;UID=user;PWD=pass" \
      --source-table "dbo.notes" \
      --output-table "dbo.deid_notes" \
      --batch-size 100000 \
      --philter-config "configs/philter_one.json"
"""

import os
import sys
import time
import logging
import argparse
import pyodbc
from concurrent.futures import ProcessPoolExecutor, as_completed

logger = logging.getLogger(__name__)


# ============================================================
# WORKER FUNCTION (top-level for pickle compatibility)
# ============================================================

def _deidentify_batch(records, philter_config_path):
    """
    Worker function executed in a child process.

    Receives a list of (note_id, de_id_name, report_text) tuples, runs the
    full Philter de-identification pipeline in memory, and returns results.

    Args:
        records: List of (note_id, de_id_name, report_text) tuples
        philter_config_path: Path to Philter JSON config file

    Returns:
        List of (note_id, de_id_name, deidentified_text, error_or_None) tuples
    """
    from phitexts import Phitexts
    from keyword_removal import remove_keywords

    results = []

    # Separate empty/null texts from processable texts
    texts_dict = {}
    filenames_list = []
    # Map de_id_name back to note_id for result assembly
    deid_to_noteid = {}

    for note_id, de_id_name, report_text in records:
        deid_key = str(de_id_name)
        deid_to_noteid[deid_key] = note_id
        if report_text is None or report_text.strip() == "":
            results.append((note_id, de_id_name, "", None))
            continue
        cleaned_text = remove_keywords(report_text)
        texts_dict[deid_key] = cleaned_text
        filenames_list.append(deid_key)

    if not texts_dict:
        return results

    try:
        # Create Phitexts with xml=True to skip _read_texts() file I/O
        phitexts = Phitexts(inputdir=None, xml=True, batch=None, db=None,
                            mongo=None)
        phitexts.texts = texts_dict
        phitexts.filenames = filenames_list

        # Run the detection and transformation pipeline
        phitexts.detect_phi(philter_config_path, verbose=False)
        phitexts.transform()

        # Collect results
        for deid_key in filenames_list:
            deidentified = phitexts.textsout.get(deid_key, "")
            results.append((deid_to_noteid[deid_key], deid_key,
                            deidentified, None))

    except Exception as e:
        # If the whole sub-batch fails, mark all processable records as failed
        for deid_key in filenames_list:
            results.append((deid_to_noteid[deid_key], deid_key, "", str(e)))

    return results


# ============================================================
# MAIN ORCHESTRATOR
# ============================================================

class SQLServerDeidentifier:
    """Orchestrates batch de-identification from SQL Server."""

    def __init__(self, connection_string, source_table, output_table,
                 note_id_column="note_id", deid_name_column="de_id_name",
                 text_column="ReportTXT",
                 output_text_column="DeId_ReportTXT",
                 batch_size=100_000, num_workers=None,
                 philter_config="configs/philter_one.json",
                 progress_table="dbo.deidentify_progress"):
        self.connection_string = connection_string
        self.source_table = source_table
        self.output_table = output_table
        self.note_id_column = note_id_column
        self.deid_name_column = deid_name_column
        self.text_column = text_column
        self.output_text_column = output_text_column
        self.batch_size = batch_size
        self.num_workers = num_workers or max(1, os.cpu_count() - 1)
        self.philter_config = philter_config
        self.progress_table = progress_table

    def _get_connection(self):
        """Create a new pyodbc connection."""
        conn = pyodbc.connect(self.connection_string, autocommit=False)
        return conn

    def _ensure_output_table(self, conn):
        """Create the output table if it doesn't exist."""
        cursor = conn.cursor()
        cursor.execute(f"""
            IF NOT EXISTS (
                SELECT * FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_NAME = '{self.output_table.split('.')[-1]}'
            )
            CREATE TABLE {self.output_table} (
                [{self.note_id_column}] NVARCHAR(500) PRIMARY KEY,
                [{self.deid_name_column}] NVARCHAR(500),
                [{self.output_text_column}] NVARCHAR(MAX)
            )
        """)
        conn.commit()
        cursor.close()

    def _ensure_progress_table(self, conn):
        """Create progress tracking table if it doesn't exist."""
        table_name = self.progress_table.split('.')[-1]
        cursor = conn.cursor()
        cursor.execute(f"""
            IF NOT EXISTS (
                SELECT * FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_NAME = '{table_name}'
            )
            CREATE TABLE {self.progress_table} (
                batch_number INT PRIMARY KEY,
                last_processed_id NVARCHAR(500),
                records_processed INT,
                errors INT,
                elapsed_seconds FLOAT,
                completed_at DATETIME DEFAULT GETDATE()
            )
        """)
        conn.commit()
        cursor.close()

    def _get_last_processed_id(self, conn):
        """Query progress table to find the resume point."""
        cursor = conn.cursor()
        try:
            cursor.execute(f"""
                SELECT TOP 1 last_processed_id
                FROM {self.progress_table}
                ORDER BY batch_number DESC
            """)
            row = cursor.fetchone()
            return row[0] if row else None
        except pyodbc.ProgrammingError:
            return None
        finally:
            cursor.close()

    def _get_total_count(self, conn):
        """Get total row count from source table."""
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {self.source_table}")
        count = cursor.fetchone()[0]
        cursor.close()
        return count

    def _get_next_batch_number(self, conn):
        """Get the next batch number for progress tracking."""
        cursor = conn.cursor()
        try:
            cursor.execute(f"""
                SELECT ISNULL(MAX(batch_number), 0) + 1
                FROM {self.progress_table}
            """)
            return cursor.fetchone()[0]
        except pyodbc.ProgrammingError:
            return 1
        finally:
            cursor.close()

    def _fetch_batch(self, conn, last_id, batch_size):
        """
        Fetch a batch of rows using keyset pagination on note_id.
        Returns list of (note_id, de_id_name, report_text) tuples.
        """
        cursor = conn.cursor()
        if last_id is None:
            cursor.execute(f"""
                SELECT TOP (?) [{self.note_id_column}],
                               [{self.deid_name_column}],
                               [{self.text_column}]
                FROM {self.source_table}
                ORDER BY [{self.note_id_column}]
            """, (batch_size,))
        else:
            cursor.execute(f"""
                SELECT TOP (?) [{self.note_id_column}],
                               [{self.deid_name_column}],
                               [{self.text_column}]
                FROM {self.source_table}
                WHERE [{self.note_id_column}] > ?
                ORDER BY [{self.note_id_column}]
            """, (batch_size, last_id))

        rows = cursor.fetchall()
        cursor.close()
        return [(row[0], row[1], row[2]) for row in rows]

    def _write_batch(self, conn, results):
        """Bulk insert de-identified results into output table."""
        if not results:
            return
        cursor = conn.cursor()
        cursor.fast_executemany = True
        cursor.executemany(f"""
            INSERT INTO {self.output_table}
            ([{self.note_id_column}], [{self.deid_name_column}],
             [{self.output_text_column}])
            VALUES (?, ?, ?)
        """, results)
        conn.commit()
        cursor.close()

    def _mark_batch_complete(self, conn, batch_number, last_id,
                             records_processed, errors, elapsed):
        """Record batch completion in progress table."""
        cursor = conn.cursor()
        cursor.execute(f"""
            INSERT INTO {self.progress_table}
            (batch_number, last_processed_id, records_processed, errors,
             elapsed_seconds)
            VALUES (?, ?, ?, ?, ?)
        """, (batch_number, last_id, records_processed, errors, elapsed))
        conn.commit()
        cursor.close()

    def _split_into_sub_batches(self, records, num_workers):
        """Split records evenly across workers."""
        sub_batches = []
        chunk_size = max(1, len(records) // num_workers)
        for i in range(0, len(records), chunk_size):
            sub_batches.append(records[i:i + chunk_size])
        return sub_batches

    def _process_batch(self, records):
        """
        Process a batch of records using ProcessPoolExecutor.
        Returns (successful, failed) where:
          successful = [(note_id, de_id_name, deidentified_text), ...]
          failed = [(note_id, error_message), ...]
        """
        sub_batches = self._split_into_sub_batches(records, self.num_workers)

        successful = []
        failed = []

        with ProcessPoolExecutor(max_workers=self.num_workers) as executor:
            futures = {}
            for i, sub_batch in enumerate(sub_batches):
                future = executor.submit(
                    _deidentify_batch, sub_batch, self.philter_config
                )
                futures[future] = i

            for future in as_completed(futures):
                sub_batch_idx = futures[future]
                try:
                    results = future.result(timeout=7200)
                    for note_id, de_id_name, text, error in results:
                        if error is None:
                            successful.append((note_id, de_id_name, text))
                        else:
                            failed.append((note_id, error))
                except Exception as e:
                    logger.error(
                        f"Sub-batch {sub_batch_idx} failed: {e}"
                    )
                    for note_id, de_id_name, _ in sub_batches[sub_batch_idx]:
                        failed.append((str(note_id), str(e)))

        return successful, failed

    def run(self):
        """Main entry point. Processes all records in batches."""
        conn = self._get_connection()
        self._ensure_output_table(conn)
        self._ensure_progress_table(conn)

        total_rows = self._get_total_count(conn)
        last_id = self._get_last_processed_id(conn)
        batch_number = self._get_next_batch_number(conn)

        if last_id:
            logger.info(f"Resuming after ID: {last_id}")
        logger.info(
            f"Total source rows: {total_rows:,} | "
            f"Batch size: {self.batch_size:,} | "
            f"Workers: {self.num_workers}"
        )

        total_processed = 0
        total_errors = 0
        start_time = time.time()

        while True:
            batch_start = time.time()

            # 1. Read batch from SQL Server
            records = self._fetch_batch(conn, last_id, self.batch_size)
            if not records:
                logger.info("No more records to process.")
                break

            logger.info(
                f"Batch {batch_number}: read {len(records)} records"
            )

            # 2. Process in parallel
            successful, failed = self._process_batch(records)

            # 3. Write results to output table
            if successful:
                self._write_batch(conn, successful)

            # 4. Log failures
            for note_id, error in failed:
                logger.warning(
                    f"Failed record note_id={note_id}: {error}"
                )

            # 5. Track progress (paginate by note_id, which is records[-1][0])
            last_id = str(records[-1][0])
            elapsed = time.time() - batch_start
            self._mark_batch_complete(
                conn, batch_number, last_id,
                len(successful), len(failed), elapsed
            )

            total_processed += len(successful)
            total_errors += len(failed)
            rate = len(records) / elapsed if elapsed > 0 else 0

            logger.info(
                f"Batch {batch_number}: "
                f"{len(successful)} OK, {len(failed)} failed, "
                f"{elapsed:.1f}s ({rate:.0f} records/s) | "
                f"Total: {total_processed:,} processed, "
                f"{total_errors:,} errors"
            )

            batch_number += 1

        total_elapsed = time.time() - start_time
        logger.info(
            f"Complete. {total_processed:,} records processed, "
            f"{total_errors:,} errors in {total_elapsed:.0f}s"
        )
        conn.close()


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="De-identify SQL Server records using Philter"
    )
    parser.add_argument(
        "--connection-string", required=True,
        help='pyodbc connection string, e.g. '
             '"DRIVER={ODBC Driver 17 for SQL Server};'
             'SERVER=myserver;DATABASE=mydb;UID=user;PWD=pass"'
    )
    parser.add_argument(
        "--source-table", required=True,
        help="Source table name, e.g. dbo.notes"
    )
    parser.add_argument(
        "--output-table", required=True,
        help="Output table name, e.g. dbo.deid_notes"
    )
    parser.add_argument(
        "--note-id-column", default="note_id",
        help="Name of the note ID column (default: note_id)"
    )
    parser.add_argument(
        "--deid-name-column", default="de_id_name",
        help="Name of the de_id_name column (default: de_id_name)"
    )
    parser.add_argument(
        "--text-column", default="ReportTXT",
        help="Name of the text column in the source table (default: ReportTXT)"
    )
    parser.add_argument(
        "--output-text-column", default="DeId_ReportTXT",
        help="Name of the deidentified text column in the output table "
             "(default: DeId_ReportTXT)"
    )
    parser.add_argument(
        "--batch-size", type=int, default=100_000,
        help="Number of rows per batch (default: 100000)"
    )
    parser.add_argument(
        "--num-workers", type=int, default=None,
        help="Number of parallel workers (default: cpu_count - 1)"
    )
    parser.add_argument(
        "--philter-config", default="configs/philter_one.json",
        help="Path to Philter filter config (default: configs/philter_one.json)"
    )
    parser.add_argument(
        "--progress-table", default="dbo.deidentify_progress",
        help="Table to track batch progress (default: dbo.deidentify_progress)"
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)"
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    deidentifier = SQLServerDeidentifier(
        connection_string=args.connection_string,
        source_table=args.source_table,
        output_table=args.output_table,
        note_id_column=args.note_id_column,
        deid_name_column=args.deid_name_column,
        text_column=args.text_column,
        output_text_column=args.output_text_column,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
        philter_config=args.philter_config,
        progress_table=args.progress_table,
    )

    deidentifier.run()


if __name__ == "__main__":
    main()
