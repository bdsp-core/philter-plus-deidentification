import argparse
import pandas as pd
import os
import subprocess

def process_files_in_batch(batch_number):
    # Constants for the directory paths
    DONE_FOLDER_DIR = 'data/doneFolder'
    CSV_FILE_PATH = 'schedulingCSV.csv'
    
    # Ensure the done folder exists
    os.makedirs(DONE_FOLDER_DIR, exist_ok=True)
    
    # Read the CSV file into a DataFrame
    df = pd.read_csv(CSV_FILE_PATH)
    
    # Filter the DataFrame for the given batchNumber
    batch_files = df[df['batchNumber'] == batch_number]
    
    # Initialize a flag to track if any file was processed
    processed_any_file = False

    # Process each file in the batch
    for input_file_name in batch_files['inputFileName']:
        done_path = os.path.join(DONE_FOLDER_DIR, input_file_name)
        
        # Check if file exists in the done folder
        if not os.path.exists(done_path):
            # Call the fcnDeID_File.py script with the found file
            subprocess.run(['python', 'fcnDeID_File.py', input_file_name], check=True)
            print(f"Processed file: {input_file_name}")
            processed_any_file = True
        else:
            print(f"File already processed: {input_file_name}")

    if not processed_any_file:
        print("No more files to process for the given batch number.")

def main():
    parser = argparse.ArgumentParser(description="Process files in a given batch.")
    parser.add_argument('batch_number', type=int, help="Batch number to process")

    args = parser.parse_args()
    process_files_in_batch(args.batch_number)

if __name__ == "__main__":
    main()
