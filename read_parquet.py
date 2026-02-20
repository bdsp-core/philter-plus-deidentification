"""
Read and print a Parquet file (local or S3).

Usage:
    python read_parquet.py <path_to_parquet>
    python read_parquet.py <path_to_parquet> --rows 20
    python read_parquet.py s3://bdsp-site-mgb/philter-deidentify/test_output/ --profile bidmc
"""
import argparse
import pyarrow.parquet as pq
import pandas as pd
import os


def read_parquet(path, num_rows=10, profile=None):
    if path.startswith("s3://"):
        import s3fs
        kwargs = {"profile": profile} if profile else {}
        fs = s3fs.S3FileSystem(**kwargs)
        dataset = pq.ParquetDataset(path, filesystem=fs)
        table = dataset.read()
    elif os.path.isdir(path):
        dataset = pq.ParquetDataset(path)
        table = dataset.read()
    else:
        table = pq.read_table(path)

    df = table.to_pandas()

    print("=" * 80)
    print(f"Parquet file: {path}")
    print("=" * 80)
    print(f"Rows:    {len(df):,}")
    print(f"Columns: {len(df.columns)}")
    print(f"Schema:")
    for col in df.columns:
        print(f"  {col:30s} {str(df[col].dtype)}")

    print("\n" + "=" * 80)
    print(f"First {min(num_rows, len(df))} rows:")
    print("=" * 80)

    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_colwidth", 100)
    print(df.head(num_rows).to_string())

    # Print full text of first row if NoteTXT column exists
    text_col = None
    for col in ["NoteTXT", "ReportTXT", "text", "note_text"]:
        if col in df.columns:
            text_col = col
            break

    if text_col and len(df) > 0:
        print("\n" + "=" * 80)
        print(f"Full {text_col} of first row ({len(str(df[text_col].iloc[0]))} chars):")
        print("=" * 80)
        print(str(df[text_col].iloc[0])[:2000])
        if len(str(df[text_col].iloc[0])) > 2000:
            print(f"\n... truncated ({len(str(df[text_col].iloc[0]))} total chars)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read and print a Parquet file")
    parser.add_argument("path", help="Path to Parquet file or directory (local or s3://)")
    parser.add_argument("--rows", type=int, default=10, help="Number of rows to display (default: 10)")
    parser.add_argument("--profile", default=None, help="AWS profile for S3 paths")
    args = parser.parse_args()

    read_parquet(args.path, args.rows, args.profile)
