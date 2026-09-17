"""
What you add to a festival in the dashboard: an application status and a free note, stored in
data/dashboard/notes.json and kept across runs and rebuilds of the table.
"""
from datetime import datetime

from open_call_agent.storage import read_json, write_json_atomic

# Statuses you can set on a festival. "applied", "accepted" and "rejected" count as a completed application.
NOTE_STATUSES = [
    {"value": "", "label": "No status"},
    {"value": "interested", "label": "Interested"},
    {"value": "applied", "label": "Applied"},
    {"value": "accepted", "label": "Accepted"},
    {"value": "rejected", "label": "Rejected"},
    {"value": "not_a_festival", "label": "Not a festival"},
    {"value": "ignored", "label": "Ignored"},
]
COMPLETED_STATUSES = {"applied", "accepted", "rejected"}
EXCLUDED_STATUSES = {"not_a_festival", "ignored"}
VALID_STATUSES = {status["value"] for status in NOTE_STATUSES}


def notes_path(data_dir):
    """
    :return: The Path of the notes file.
    """
    return data_dir / "dashboard" / "notes.json"


def read_notes(data_dir):
    """
    :param data_dir: The data directory.
    :return: A dict of festival id -> {"status", "note", "updated"}.
    """
    return read_json(notes_path(data_dir)) or {}


def save_note(data_dir, festival_id, status="", note=""):
    """
    Add or update a festival's note, or remove it when both fields are empty.

    :param data_dir: The data directory.
    :param festival_id: A festival id from the table.
    :param status: One of NOTE_STATUSES.
    :param note: Free text.
    :return: All notes.
    :raises ValueError: If the id is empty or the status is unknown.
    """
    if not festival_id:
        raise ValueError("A festival id is required")
    if status not in VALID_STATUSES:
        raise ValueError(f"Unknown status '{status}'")

    notes = read_notes(data_dir)
    if status or note.strip():
        notes[festival_id] = {"status": status, "note": note.strip(),
                              "updated": datetime.now().isoformat(timespec="seconds")}
    else:
        notes.pop(festival_id, None)
    write_json_atomic(notes_path(data_dir), notes)
    return notes
