#!/bin/bash
# Runtime data dependencies for philter-plus. These are NOT pip packages and NOT in requirements.txt.
# A missing 'names' corpus silently degrades surrogate de-identification to asterisk redaction.
set -e
python3 -m pip install --quiet s3fs fsspec        # missing from requirements.txt; needed for S3 I/O
python3 -m nltk.downloader punkt punkt_tab averaged_perceptron_tagger \
    averaged_perceptron_tagger_eng maxent_ne_chunker maxent_ne_chunker_tab words names
# prove surrogates can actually run before anyone processes millions of notes
python3 -c "import surrogate_names; d=surrogate_names._load(); \
print(f'surrogate lists OK: {len(d[\"male\"])} male, {len(d[\"female\"])} female first names')"
