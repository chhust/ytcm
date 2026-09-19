import logging
logger = logging.getLogger(__name__)

import glob
import json
import os
import shutil

from ytcm.config import (CACHE_FILE, COMMENTS_FOLDER, FILTER_JSON, MARKERS_TXT,
                         COMMENTS_JSON, COMMENTS_HTML, COMMENTS_GEXF_ON, COMMENTS_GEXF_OFF, COMMENTS_CSV)

def inside_working_directory(path):
    root = os.path.realpath(os.getcwd())
    target = os.path.realpath(os.path.join(root, path))
    return target == root or target.startswith(root + os.sep)


def refuse_outside(path):
    if not inside_working_directory(path):
        raise ValueError(
            f"'{path}' is outside the working directory ({os.getcwd()}). Give a "
            f"path inside it.")
    return path


def prepare_output_directory(directory):

    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as e:
        logger.error(f"Error creating output directory '{directory}': {e}.")
        raise


def load_existing_comments(filename):

    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except json.JSONDecodeError as e:
            logger.error(f"JSON format error reading from '{filename}': {e}.")
        except (OSError, UnicodeDecodeError) as e:
            logger.error(f"Could not read '{filename}': {e}.")
    else:
        logger.info(f"File '{filename}' does not exist. Starting with an empty collection.")
    return {}


def delete_all_files():
    files_to_delete = [
        CACHE_FILE,
        COMMENTS_JSON,
        COMMENTS_HTML,
        COMMENTS_GEXF_ON,
        COMMENTS_GEXF_OFF,
        COMMENTS_CSV,
        FILTER_JSON,
        MARKERS_TXT,
    ]
    for pattern in ("*.cleaned.json", "*.jsonl", "*.gexf", "*.fingerprint",
                   "semsearch.csv", "communities.csv"):
        files_to_delete.extend(sorted(glob.glob(pattern)))

    for filepath in files_to_delete:
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                logger.info(f"{filepath} deleted.")
        except OSError as e:
            logger.error(f"Failed to delete {filepath}: {e}.")

    try:
        folder = os.path.realpath(COMMENTS_FOLDER)
        here = os.path.realpath(os.getcwd())
        if folder == here or not folder.startswith(here + os.sep):
            logger.error(f"Refusing to delete '{COMMENTS_FOLDER}': it is not a "
                         f"directory inside the working directory.")
        elif os.path.isdir(folder):
            shutil.rmtree(folder)
            logger.info(f"{COMMENTS_FOLDER} and all files in it deleted.")
    except OSError as e:
        logger.error(f"Failed to delete {COMMENTS_FOLDER}: {e}.")
