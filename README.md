# philter-plus-deidentification
This is the repository to store the code related to de-identifying unstructured clinical data. 

Please follow these steps to run the code:

1) Create a conda environment with Python 3.10. 
2) Install all the packages listed in the requirements.txt using pip install -r requirements.txt in the conda environment.
3) You might need to install two additional nltk packages separately. Please make sure that packages are installed before you start the deidentification process. You can do that using these commands:
   ```
   import nltk
   nltk.download('punkt')
   nltk.download('averaged_perceptron_tagger')
   ```
4) After you have the environment set up correctly please make sure you have the notes that you need to deidentify in .txt format and place it in the input folder. 
5) The deidentified output file will have the same name as the input file so make sure that you do not have any PHI in the name of the file.
6) For the purpose of this code we are using data/input as the input directory to store all the identified notes which need to be deidentified.
7) Please run the following command to deidentify your notes: 

python deidpipe.py -i data/input/ -o data/output/ -f configs/philter_one.json -l False

Flags:
```
-i (input_dir):  Path to the directory or the file that contains the PHI note
-o (output_dir):  Path to the directory to save PHI-reduced notes
-l (True,False):  When this is true, the pipeline prints and saves log in a subdirectory in each output directory, the default is True
```
8) After the program finishes you can find the deidentified notes in the data/output folder.
9) If you see any PHI not being removed please contact Aditya immediately so that he can make the necessary changes at agupta41@mgh.harvard.edu.
10) Always deidentify a small batch of files to check if you are getting the correct output and all the PHI keywords are being removed correctly.
11) For more information please refer to this link: https://github.com/BCHSI/philter-deidstable1_mirror
