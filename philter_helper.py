"""
Helper functions for using Philter with text dictionaries.
"""
from philter import Philter


def run_philter_on_dict(texts_dict, config_path):
    """
    Run Philter de-identification on a dictionary of texts.

    Args:
        texts_dict: Dictionary of {filename: text_content}
        config_path: Path to Philter configuration JSON file

    Returns:
        Dictionary of {filename: de-identified_text}
    """
    if not texts_dict:
        return {}

    # Prepare Philter config with text dictionary
    philter_config = {
        "filters": config_path,
        "phi_text": texts_dict,
        "filenames": list(texts_dict.keys()),
        "verbose": False,
        "run_eval": False
    }

    # Initialize Philter
    philter = Philter(philter_config)

    # Map coordinates to identify PHI locations
    philter.map_coordinates()

    # Transform each text using the asterisk method
    results = {}
    for filename in texts_dict.keys():
        deid_text = philter.transform_text_asterisk(texts_dict[filename], filename)
        results[filename] = deid_text

    return results
