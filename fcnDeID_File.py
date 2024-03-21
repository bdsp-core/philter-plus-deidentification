import argparse
import os
import shutil
from datetime import date
from phitexts import Phitexts

def process_ecg_file(next_file, source_dir='data/waitingRoom', output_dir='data/output', doneFolder='data/doneFolder'):
    """
    Process a given ECG file and move it to a done folder after processing.

    Args:
        next_file (str): The name of the file to process.
        source_dir (str, optional): Directory where the source files are located. Defaults to 'data/waitingRoom'.
        output_dir (str, optional): Directory where the output should be saved. Defaults to 'data/output'.
        doneFolder (str, optional): Directory where the processed file will be moved. Defaults to 'data/doneFolder'.
    """
    # Create directories
    folderName = os.path.splitext(next_file)[0]
    input_dir = f'data/{folderName}'
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(doneFolder, exist_ok=True)  # Create done folder if it does not exist

    # Process the file
    source_file = os.path.join(source_dir, next_file)
    input_file = os.path.join(input_dir, next_file)

    # Copy file to input_dir
    shutil.copy(source_file, input_dir)

    # Other hardcoded input arguments
    args_filters = "configs/philter_one.json"
    args_xml = False
    args_verbose = False
    args_refdate = str(date.today())

    db = None
    mongo = None
    batch = 0

    # Initialize and process with Phitexts
    phitexts = Phitexts(input_dir, args_xml, batch, db, mongo)
    print(f"Processing files in: {input_dir}")
    print("Detecting PHI...")
    phitexts.detect_phi(args_filters, verbose=args_verbose)

    if phitexts.coords:
        print("PHI coordinates detected, processing...")
        phitexts.detect_phi_types() if not args_xml else None
        phitexts.normalize_phi()
        phitexts.substitute_phi(look_up_table_path=mongo, db=db, ref_date=args_refdate) if mongo else None

    phitexts.transform()

    # Save output
    phitexts.save(output_dir, use_deid_note_key=False, suf="", ext="txt")

    print("Process completed.")


    # Cleanup
    destination_file = os.path.join(doneFolder, next_file)
    if not os.path.exists(destination_file):
        shutil.move(input_file, destination_file)
    else:
        print(f"File {next_file} already exists in {doneFolder}, skipping move.")
    os.remove(source_file)  # Remove the original file from the source directory
    shutil.rmtree(input_dir)  # Remove the temporary input directory

def main():
    # Create the parser
    parser = argparse.ArgumentParser(description="Process an ECG file")

    # Add arguments
    parser.add_argument("next_file", help="The name of the file to process")
    parser.add_argument("--source_dir", default='data/waitingRoom', help="Directory where the source files are located")
    parser.add_argument("--output_dir", default='data/output', help="Directory where the output should be saved")
    parser.add_argument("--doneFolder", default='data/doneFolder', help="Directory where the processed file will be moved")

    # Parse the arguments
    args = parser.parse_args()

    # Call the function
    process_ecg_file(args.next_file, args.source_dir, args.output_dir, args.doneFolder)

if __name__ == "__main__":
    main()


# python fcnDeID_File.py ecg_diagnosis_input_1980_chunk1.txt