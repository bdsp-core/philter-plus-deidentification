import os
import shutil
import subprocess

def list_txt_files(directory):
    """List all .txt files in the given directory."""
    return [f for f in os.listdir(directory) if f.endswith('.txt')]

def process_file(file_name, source_dir, input_dir, output_dir):
    """Process a file by copying it to input_dir, running deidpipe_simple.py, and then removing it from input_dir."""
    source_file = os.path.join(source_dir, file_name)
    input_file = os.path.join(input_dir, file_name)

    # Copy file to input_dir
    shutil.copy(source_file, input_dir)

    # Run deidpipe_simple.py
    subprocess.run(['python', 'deidpipe_simple1.py'])

    # Remove the file from input_dir
    os.remove(input_file)

def main():
    source_dir = 'data/waitingRoom1'
    input_dir = 'data/input1'
    output_dir = 'data/output2'

    source_files = list_txt_files(source_dir)
    output_files = list_txt_files(output_dir)

    for file in source_files:
        if file not in output_files:
            print(f"Processing file: {file}")
            process_file(file, source_dir, input_dir, output_dir)
        else:
            print(f"Skipping already processed file: {file}")

if __name__ == "__main__":
    main()
