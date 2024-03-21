import argparse

def process_ecg_file(next_file, source_dir='data/waitingRoom', output_dir='data/output'):
    """
    Process a given ECG file.

    Args:
        next_file (str): The name of the file to process.
        source_dir (str, optional): Directory where the source files are located. Defaults to 'data/waitingRoom'.
        output_dir (str, optional): Directory where the output should be saved. Defaults to 'data/output'.
    """
    # Load packages
    import os
    import shutil
    import pandas as pd
    from datetime import date
    from phitexts import Phitexts

    # Create directories
    folderName = os.path.splitext(next_file)[0]
    input_dir = f'data/{folderName}'
    os.makedirs(input_dir, exist_ok=True)

    # Process the file
    source_file = os.path.join(source_dir, next_file)
    input_file = os.path.join(input_dir, next_file)

    # Copy file to input_dir
    shutil.copy(source_file, input_dir)

    # Other hardcoded input arguments
    args_input = input_dir
    args_output = output_dir
    args_filters = "configs/philter_one.json"
    args_log = True
    args_mongodb = None
    args_surrogate_info = None
    args_deid_filename = True
    args_dynamic_blacklist = None
    args_eval = False
    args_anno = './data/i2b2_xml'
    args_xml = False
    args_verbose = False
    args_batch = None
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
        if not args_xml:
            phitexts.detect_phi_types()
        
        phitexts.normalize_phi()

        if mongo is not None:
            if args_surrogate_info:
                print("WARNING: Surrogate meta file and mongodb were passed as arguments. Ignoring surrogate meta file and using mongodb")
            phitexts.substitute_phi(look_up_table_path=mongo, db=db, ref_date=args_refdate)
        elif args_surrogate_info:
            phitexts.substitute_phi(look_up_table_path=args_surrogate_info, ref_date=args_refdate)

    phitexts.transform()

    # Save output
    if (args_deid_filename and not args_surrogate_info) and (args_deid_filename and not args_mongodb):
        print("WARNING: no surrogate info provided, saving output with identified note key")
        args_deid_filename = False

    if mongo is not None:
        if args_output:
            print("WARNING: Output path and mongodb provided writing deid notes into mongodb")
        phitexts.save_mongo(mongo)
    else:
        phitexts.save(args_output, use_deid_note_key=args_deid_filename, suf="", ext="txt")

    if args_eval:
        print("Evaluating...")
        phitexts.eval(args_anno, args_output)

    print("Process completed.")

    # Cleanup
    os.remove(input_file)
    shutil.rmtree(input_dir)

def main():
    # Create the parser
    parser = argparse.ArgumentParser(description="Process an ECG file")

    # Add arguments
    parser.add_argument("next_file", help="The name of the file to process")
    parser.add_argument("--source_dir", default='data/waitingRoom', help="Directory where the source files are located (default: data/waitingRoom)")
    parser.add_argument("--output_dir", default='data/output', help="Directory where the output should be saved (default: data/output)")

    # Parse the arguments
    args = parser.parse_args()

    # Call the function
    process_ecg_file(args.next_file, args.source_dir, args.output_dir)

if __name__ == "__main__":
    main()

# Example usage
# from a1_wrap_deidpipe_N_fxn import process_ecg_file
# doBatchNumber = 5
# source_dir='data/waitingRoom'
# output_dir='data/output'
# process_ecg_file("ecg_diagnosis_input_1980_chunk1.txt", source_dir, output_dir)
