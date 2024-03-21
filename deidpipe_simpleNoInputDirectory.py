import sys
import os
import json
import pymongo
from pymongo import MongoClient
from phitexts import Phitexts
from datetime import date
from read_texts_mbw import read_texts
from phitexts_mbw import phitexts_mbw

# Hardcoded input arguments
args_input = "data/input/"
args_output = "data/output/"
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

batch = 0
db=None
mongo=None
global args_deid_filename  # Add this line


inputdir = "/Users/mbw/cdac Dropbox/brandon westover/ECG_Deidentification/philter-plus-deidentification/data/input/"
texts = read_texts(inputdir)
print(texts)

phitexts = Phitexts(inputdir, args_xml, batch, db, mongo)
print(phitexts)

print("Detecting PHI...")
phitexts.detect_phi(args_filters, verbose=args_verbose)

if phitexts.coords:
    print("PHI coordinates detected, processing...")
    phitexts.detect_phi_types()
    phitexts.normalize_phi()
    phitexts.substitute_phi(look_up_table_path=args_surrogate_info, ref_date=args_refdate)
    phitexts.transform()

# print(phitexts)

