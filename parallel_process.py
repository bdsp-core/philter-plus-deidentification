import argparse
import os
import subprocess
from concurrent.futures import ProcessPoolExecutor
from glob import glob

def process_file(input_file_path):
    DONE_FOLDER_DIR = 'data/doneFolder'
    os.makedirs(DONE_FOLDER_DIR, exist_ok=True)
    
    input_file_name = os.path.basename(input_file_path)
    done_path = os.path.join(DONE_FOLDER_DIR, input_file_name)
    
    # Process the file only if it's not in the done folder
    if not os.path.exists(done_path):
        try:
            subprocess.run(['python', 'fcnDeID_File.py', input_file_name], check=True)
            print(f"Processed file: {input_file_name}")
        except subprocess.CalledProcessError as e:
            print(f"An error occurred while processing {input_file_name}: {e}")
    else:
        print(f"File {input_file_name} is already processed.")

def main(file_paths, num_processes):
    with ProcessPoolExecutor(max_workers=num_processes) as executor:
        executor.map(process_file, file_paths)

if __name__ == "__main__":
    WAITING_ROOM_DIR = 'data/waitingRoom'
    # Get list of all .txt files in the waiting room directory
    file_paths = glob(os.path.join(WAITING_ROOM_DIR, '*.txt'))
    
    # Number of CPU cores - 1 to leave a core free for other tasks
    num_processes = os.cpu_count() - 1

    # Process the files in parallel
    main(file_paths, num_processes)
