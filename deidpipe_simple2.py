#!/usr/local/bin/python3
import sys
import os
import json
import pymongo
from pymongo import MongoClient
from phitexts import Phitexts
from datetime import date

print(sys.executable)

# TODO: replace this by Python's POSIX compliant versions
EXIT_SUCCESS = 0
EXIT_FAILURE = -1

# Hardcoded input arguments
args_input = "data/input2/"
args_output = "data/output2/"
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

def main():
    print("Starting main function")
    if args_mongodb:
        mongo = read_mongo_config(args_mongodb)
        try:
            db = get_mongo_handle(mongo)
        except pymongo.errors.PyMongoError as e:
            print("Mongo Server not available:", e)
            sys.exit(EXIT_FAILURE)
        main_mongo(db=db, mongo=mongo)
    else:
        main_mongo()

def main_mongo(db=None, mongo=None):
    global args_deid_filename  # Add this line

    print("Executing main_mongo...")
    
    batch = 0
    if args_batch is not None:
        batch = int(float(args_batch))

    phitexts = Phitexts(args_input, args_xml, batch, db, mongo)
    print(f"Processing files in: {args_input}")

    if args_xml:
        print("Detecting XML PHI...")
        phitexts.detect_xml_phi()
    elif args_dynamic_blacklist:
        print("Detecting PHI with dynamic blacklist...")
        phitexts.detect_phi(args_filters, args_dynamic_blacklist, verbose=args_verbose)
    else:
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
    return EXIT_SUCCESS

if __name__ == "__main__":
    sys.exit(main())
