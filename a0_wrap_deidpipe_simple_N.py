next_file = "ecg_diagnosis_input_1980_chunk1.txt"

import os
import shutil
import subprocess
import pandas as pd

import sys
import os
import json
import pymongo
from pymongo import MongoClient
from phitexts import Phitexts
from datetime import date

def list_txt_files(directory):
    """List all .txt files in the given directory."""
    return [f for f in os.listdir(directory) if f.endswith('.txt')]

def read_mongo_config(mongofile):
    if not os.path.exists(mongofile):
        raise Exception("Filepath does not exist", mongofile)
    mongo_details = json.loads(open(mongofile, "r").read())
    return mongo_details

def get_mongo_handle(mongo):
    client = MongoClient(mongo["client"], username=mongo["username"], password=mongo["password"])
    try:
        db = client[mongo['db']]
    except pymongo.errors.PyMongoError as mongoerrors:
        print("Mongo Server not available")
        print(mongoerrors.__dict__.keys())
        sys.exit(EXIT_FAILURE)
    return db


#####################

doBatchNumber = 5 # specify batch number to process
source_dir = 'data/waitingRoom'
output_dir = 'data/output'

df = pd.read_csv('schedulingCSV.csv') # read scheduling CSV
# next_file = df.loc[(df['batchNumber'] == doBatchNumber) & (df['done'].isnull())]

# create a directory for processing this file
folderName = os.path.splitext(next_file)[0] 
input_dir = f'data/{folderName}'
os.makedirs(input_dir, exist_ok=True)

input_dir = f'data/input{doBatchNumber}' 
os.makedirs(input_dir, exist_ok=True)
file_name = next_file

#  process_file(file_name, source_dir, input_dir, output_dir)

"""Process a file by copying it to input_dir, running deidpipe_simple.py, and then removing it from input_dir."""
source_file = os.path.join(source_dir, file_name)
input_file = os.path.join(input_dir, file_name)

# Copy file to input_dir
shutil.copy(source_file, input_dir)

# Run deidpipe_simple.py
# subprocess.run(['python', 'deidpipe_simple.py'])

#############################
##### PROCESS THE FILE ######
#############################

# Hardcoded input arguments
args_input = input_dir
args_output = output_dir
args_filters = "configs/philter_one.json"
args_log = True
args_mongodb = None  # Assuming default None, modify as needed
args_surrogate_info = None  # Assuming default None, modify as needed
args_deid_filename = True  # Default value
args_dynamic_blacklist = None  # Assuming default None, modify as needed
args_eval = False  # Default value
args_anno = './data/i2b2_xml'  # Default value
args_xml = False  # Default value
args_verbose = False  # Assuming default False, modify as needed
args_batch = None  # Assuming default None, modify as needed
args_refdate = str(date.today())  # Default value

db=None, 
mongo=None
batch = 0

phitexts = Phitexts(args_input, args_xml, batch, db, mongo)
print(f"Processing files in: {args_input}")

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

# saves output
if (args_deid_filename and not args_surrogate_info) and (args_deid_filename and not args_mongodb):
    print("WARNING: no surrogate info provided, saving output with "
           + "identified note key")
    args_deid_filename=False
if mongo is not None:
   if args_output:
      print("WARNING: Output path and mongodb provided writing deid notes into mongodb")
   phitexts.save_mongo(mongo)
else:
   phitexts.save(args_output, use_deid_note_key=args_deid_filename,
           suf="", ext="txt")

if args_eval:
    print("Evaluating...")
    phitexts.eval(args_anno, args_output)

print("Process completed.")

#############################
# Remove the file from input_dir
os.remove(input_file)

# Remove input_dir
shutil.rmtree(input_dir)
####################