import subprocess

def run_deidpipe(input_file_path):
    # Define the output directory and configuration file
    output_dir = "data/output/"
    config_file = "configs/philter_one.json"
    logging = "False"
    python_interpreter = '/opt/homebrew/Caskroom/miniforge/base/bin/python'

    try:
        # Running the deidpipe.py script with the specified arguments
        subprocess.run([python_interpreter, 'deidpipe.py', '-i', input_file_path, '-o', output_dir, '-f', config_file, '-l', logging], check=True)
        print("deidpipe.py executed successfully.")
    except subprocess.CalledProcessError as e:
        print("An error occurred while running deidpipe.py:", e)

if __name__ == "__main__":
    # Process a file
    fileName = "chunk1.txt"
    input_file_path = "data/input/" + fileName
    run_deidpipe(input_file_path)
