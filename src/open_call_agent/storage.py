"""
Crash-safe file writes and stop handling, so a run can be stopped at any point without losing or corrupting results.
"""
import json
import os
import signal
from pathlib import Path


def write_text_atomic(path, text):
    """
    Write a file via a temporary file and a rename, so a stop mid-write never leaves a half-written file.

    :param path: The file Path.
    :param text: The content.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def write_json_atomic(path, data):
    """
    :param path: The file Path.
    :param data: JSON-serialisable data.
    """
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False))


def read_json(path):
    """
    :param path: The file Path.
    :return: The parsed JSON, or None if the file is missing or unreadable (e.g. left half-written by an older version).
    """
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def stop_on_sigterm():
    """
    Treat SIGTERM (plain `kill`) like Ctrl+C, so model servers are shut down and results saved so far are kept.
    """
    def handle(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handle)
