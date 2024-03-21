import argparse
import os
import subprocess

def process_file(input_file_name):
    # Constants for the directory paths
    DONE_FOLDER_DIR = 'data/doneFolder'
    
    # Ensure the done folder exists
    os.makedirs(DONE_FOLDER_DIR, exist_ok=True)
    
    # Define the path where the file will be moved after processing
    done_path = os.path.join(DONE_FOLDER_DIR, input_file_name)
    
    # Process the file: call the fcnDeID_File.py script with the found file
    subprocess.run(['python', 'fcnDeID_File.py', input_file_name], check=True)
    print(f"Processed file: {input_file_name}")
    

def main():
    parser = argparse.ArgumentParser(description="Process a specified file.")
    parser.add_argument('input_file_name', type=str, help="Name of the file to process")

    args = parser.parse_args()
    process_file(args.input_file_name)

if __name__ == "__main__":
    main()

# usage: 
# python process_file.py data/input/ecg_diagnosis_input_1990_chunk43.txt