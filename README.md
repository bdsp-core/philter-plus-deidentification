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
4) Please add all the site specific keywords in the philter-plus-deidentification/keyword_removal.py in the format in which other keywords have been added. Please have a look at the file for reference. Please do not proceed until this step is complete.  
5) After you have the environment set up correctly please make sure you have the notes that you need to deidentify in .txt format and place it in the /data/waitingRoom/ folder. 
6) The deidentified output file will have the same name as the input file so make sure that you do not have any PHI in the name of the file.
7) For the purpose of this code we are using data/waitingRoom as the input directory to store all the identified notes which need to be deidentified.
8) Please deidentify only a small set of notes first to ensure that all the site specific keywords are being removed. If they are not being removed then please go back to step 4.
9) Please run the following command to deidentify your notes: 
   ```
   python parallel_process.py
   ```

10) There are three directories that you should know about:
    ```
    'data/waitingRoom': "Directory where the source files are located"
    'data/output': "Directory where the output will be saved"
    'data/doneFolder': "Directory where the processed file will be moved"
    ```
11) After the program finishes you can find the deidentified notes in the /data/output/ folder.
12) You can remove all the files from the /data/doneFolder/ after the deidentification is complete.
13) If you have a large number of files that need to be deidentiified please make sure that computer has enough computing power or try to run the program in smaller batches. The batch size will depend on the computing power of your machine.  
14) If you see any PHI not being removed please contact Aditya immediately so that he can make the necessary changes at agupta41@mgh.harvard.edu.
15) Always deidentify a small batch of files to check if you are getting the correct output and all the PHI keywords are being removed correctly.
16) For more information please refer to this link: https://github.com/BCHSI/philter-deidstable1_mirror
