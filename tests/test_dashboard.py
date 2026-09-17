"""
Dashboard tests: the table built from Swiss data, the notes, the command each job form builds, and the API endpoints.
No model and no network, apart from the Swiss fixture the other tests already scrape.
"""
import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from dashboard import jobs as jobs_module
from dashboard.jobs import JobRunner, build_command
from dashboard.notes import read_notes, save_note
from dashboard.server import DashboardHandler
from dashboard.sources import list_sources, load_rows, row_id
from dashboard.stats import scrape_stats, table_stats
from open_call_agent.open_call.batch import RESULT_COLUMNS
from open_call_agent.ra.events import write_rows

from .conftest import COUNTRY

SLEEP_JOB = {"label": "Sleep", "command": None, "gpu": True, "fields": []}


@pytest.fixture
def results_dir(swiss_data_dir):
    """
    A results CSV next to the Swiss festivals, marking fogstudio.ch as an open call.
    """
    path = swiss_data_dir / "open_calls" / "open_calls_ch_festivals_2026-09-17_reviewed.csv"
    write_rows([{column: "" for column in RESULT_COLUMNS} |
                {"website": "https://fogstudio.ch", "status": "open", "deadline": "2026-11-15",
                 "open_call_url": "https://docs.google.com/forms/fog", "large_model_status": "open"}],
               path, RESULT_COLUMNS)
    return swiss_data_dir


@pytest.mark.network
def test_table_rows_and_stats(results_dir):
    [source] = list_sources(results_dir)
    assert source["kind"] == "results" and "reviewed" in source["label"]

    rows = load_rows(results_dir, source["id"])
    fog = next(row for row in rows if row["festival"].startswith("FOG"))
    assert fog["status"] == "open" and fog["deadline"] == "2026-11-15" and fog["month"] == "Sep 2026"
    assert fog["open_call_url"] == "https://docs.google.com/forms/fog"
    assert fog["festival_url"] == "https://fogstudio.ch", "the festival's own website"
    assert fog["source"] == "RA" and fog["source_url"].startswith("https://ra.co/events/")
    assert {row["country"] for row in rows} == {"CH"} and "zurich" in {row["city"] for row in rows}
    assert "Duration Hours" in fog["details"] and "" not in fog["details"].values()
    assert len({row["id"] for row in rows}) == len(rows), "one row per festival"

    save_note(results_dir, fog["id"], "applied", "sent a live set")
    notes = read_notes(results_dir)
    assert notes[fog["id"]]["note"] == "sent a live set"

    stats = {stat["label"]: stat["value"] for stat in table_stats(results_dir, source["id"], notes)}
    assert stats["Total Completed Applications"] == 1
    assert stats["Possible Applications"] == 0, "a festival applied to is no longer a possible application"

    save_note(results_dir, fog["id"], "", "")
    stats = {stat["label"]: stat["value"] for stat in table_stats(results_dir, source["id"], read_notes(results_dir))}
    assert stats["Possible Applications"] == 1 and stats["Total Completed Applications"] == 0

    scraped = {stat["label"]: stat["value"] for stat in scrape_stats(results_dir, JobRunner(results_dir))}
    assert scraped["Festivals Found"] == len(rows) >= 1 and scraped["Scrapers Running"] == 0


@pytest.mark.network
def test_row_id_is_stable_across_editions(results_dir):
    assert row_id({"Festival": "FOG Festival 2026", "Country Code": "ch", "City": "basel", "Event ID": "1"}) == \
           row_id({"Festival": "FOG festival 2025", "Country Code": "ch", "City": "basel", "Event ID": "2"}), \
           "a note stays with the festival when a new edition is scraped"


def test_job_commands(tmp_path):
    command = build_command("scrape", {"where": "city", "where_value": "ch/zurich", "start": "2026-09-16",
                                       "end": "2026-09-18", "festivals": True}, tmp_path)
    assert command[2:] == ["-m", "open_call_agent.cli", "--data-dir", str(tmp_path), "scrape", "--city", "ch/zurich",
                           "--start", "2026-09-16", "--end", "2026-09-18", "--festivals"]

    command = build_command("check-open-calls", {"where": "city", "country": "ch", "where_value": "zurich",
                                                 "festivals_only": True, "limit": 5}, tmp_path)
    assert "--country ch --city zurich --festivals-only --limit 5" in " ".join(command)
    assert "--no-review" not in command and "--crawl-workers" in command

    assert "--summary-only" in build_command("check-europe", {"summary_only": True}, tmp_path)
    with pytest.raises(ValueError, match="Start date"):
        build_command("scrape", {"where": "country", "where_value": "ch", "start": ""}, tmp_path)
    with pytest.raises(ValueError, match="ch"):
        build_command("scrape", {"where": "country", "where_value": " "}, tmp_path)
    with pytest.raises(ValueError, match="Unknown job"):
        build_command("rm", {}, tmp_path)


def test_only_one_gpu_job_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setitem(jobs_module.JOB_TYPES, "sleep", SLEEP_JOB)
    monkeypatch.setattr(jobs_module, "build_command",
                        lambda kind, options, data_dir: ["python", "-c", "import time; time.sleep(30)"])
    runner = JobRunner(tmp_path)
    job = runner.start("sleep", {})
    try:
        with pytest.raises(ValueError, match="GPU"):
            runner.start("sleep", {})
        assert runner.running_jobs(["sleep"]) == 1
    finally:
        runner.stop(job.id)

    for _ in range(50):
        if not job.running:
            break
        time.sleep(0.1)
    assert job.state() == "stopped", "a stopped job is not a failure"
    assert runner.start("sleep", {}).running, "the GPU is free once the job has stopped"
    runner.stop_all()


def request(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    with urlopen(Request(url, data=data, headers=headers)) as response:
        return json.loads(response.read())


@pytest.mark.network
def test_api_endpoints(results_dir, monkeypatch):
    from http.server import ThreadingHTTPServer
    from functools import partial
    import threading

    context = {"data_dir": results_dir, "runner": JobRunner(results_dir)}
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(DashboardHandler, context=context))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        config = request(f"{url}/api/config")
        assert config["default_source"].startswith("results:") and "scrape" in config["job_types"]

        festivals = request(f"{url}/api/festivals")
        assert festivals["rows"] and festivals["stats"]

        fog = next(row for row in festivals["rows"] if row["festival"].startswith("FOG"))
        saved = request(f"{url}/api/notes", {"id": fog["id"], "status": "interested", "note": "check line-up"})
        assert saved["notes"][fog["id"]]["status"] == "interested"

        with pytest.raises(HTTPError) as error:
            request(f"{url}/api/notes", {"id": fog["id"], "status": "maybe"})
        assert error.value.code == 400

        with pytest.raises(HTTPError) as error:
            request(f"{url}/api/festivals?source=results:../../../etc/passwd")
        assert error.value.code == 404, "a source outside the data directory is not readable"

        assert request(f"{url}/api/jobs")["jobs"] == []
        assert request(f"{url}/api/stats/llm")["stats"][0]["label"] == "Websites Checked"
    finally:
        server.shutdown()
        server.server_close()
