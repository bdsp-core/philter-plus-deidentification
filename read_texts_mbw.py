import os
from chardet.universaldetector import UniversalDetector
from keyword_removal import remove_keywords

def detect_encoding(filepath):
    detector = UniversalDetector()
    with open(filepath, "rb") as f:
        for line in f:
            detector.feed(line)
            if detector.done: 
                break
        detector.close()
    return detector.result

def read_texts(inputdir):
    if not inputdir:
        raise Exception("Input directory undefined: ", inputdir)

    texts = {}
    for root, dirs, files in os.walk(inputdir):
        for filename in files:
            if not filename.endswith("txt") or 'meta_data' in filename:
                continue

            filepath = os.path.join(root, filename)
            encoding = detect_encoding(filepath)
            with open(filepath, "r", encoding=encoding['encoding'], errors='surrogateescape') as fhandle:
                note_to_be_deidentified = fhandle.read()

            result = remove_keywords(note_to_be_deidentified)
            print("Pre Deidentified Note:", result)

            texts[filepath] = result

    return texts
