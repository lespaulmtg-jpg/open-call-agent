"""
Stopping a run must not lose or corrupt results: every finished site is saved, and running again continues.
Uses a fake model and fake pages for Swiss festival websites, so no GPU or network is needed.
"""
import pytest

from open_call_agent.open_call import review as review_module
from open_call_agent.open_call.checker import Page
from open_call_agent.ra.events import read_rows, write_rows
from open_call_agent.open_call.batch import RESULT_COLUMNS
from open_call_agent.storage import read_json, write_json_atomic

SITES = {
    "https://fogstudio.ch": "https://www.fogstudio.ch/proposals",
    "https://www.sunicefestival.ch": "https://www.sunicefestival.ch/about",
    "https://hiveclub.ch": "https://www.hiveclub.ch/community",
}


def first_pass_csv(path):
    rows = [{column: "" for column in RESULT_COLUMNS} | {"website": website, "status": "needs_review",
                                                         "status_reason": "Possible call (unclear)",
                                                         "announcement_url": announcement}
            for website, announcement in SITES.items()]
    rows.append({column: "" for column in RESULT_COLUMNS} | {"website": "https://kaufleuten.ch", "status": "none"})
    write_rows(rows, path, RESULT_COLUMNS)
    return path


class FakeServer:
    slots = 1

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass


def fake_classifier(calls, stop_at=None):
    class FakeClassifier:
        def __init__(self, server):
            self.stats = type("Stats", (), {"as_dict": lambda self: {}})()

        def classify(self, url, title, text):
            calls.append(url)
            if url == stop_at:
                raise KeyboardInterrupt  # Simulates Ctrl+C or kill while the model is working on this site
            return {"reasoning": "No call.", "status": "none", "opportunity_types": [], "quotes": [], "deadline": "",
                    "how_to_apply": ""}
    return FakeClassifier


def fake_evidence(row, ignore_robots, min_page_date=None):
    url = row["announcement_url"]
    return {"announcements": [Page(url, f"<title>{url}</title><p>{url} text</p>", False)], "forms": [], "errors": [],
            "too_old": [], "announcement_errors": 0}


@pytest.fixture
def fake_review(monkeypatch):
    monkeypatch.setattr(review_module, "LlamaServer", FakeServer)
    monkeypatch.setattr(review_module, "fetch_evidence", fake_evidence)
    monkeypatch.setattr(review_module, "resolve_model_path", lambda *args, **kwargs: "qwen2.5-7b-test.gguf")
    return monkeypatch


def test_stopped_review_keeps_finished_sites_and_continues(tmp_path, fake_review):
    input_path = first_pass_csv(tmp_path / "open_calls" / "open_calls_ch_festivals.csv")
    output_path = input_path.with_name("open_calls_ch_festivals_reviewed.csv")

    calls = []
    fake_review.setattr(review_module, "ServerClassifier", fake_classifier(calls, stop_at=SITES["https://hiveclub.ch"]))
    with pytest.raises(KeyboardInterrupt):
        review_module.review_open_calls(input_path, llm_slots=1, fetch_workers=1, data_dir=tmp_path)

    # The two sites reviewed before the stop are in the CSV and saved as reviews; the third is still pending
    rows = {row["website"]: row for row in read_rows(output_path)}
    assert len(rows) == 4
    assert rows["https://fogstudio.ch"]["large_model_status"] == "none"
    assert rows["https://www.sunicefestival.ch"]["large_model_status"] == "none"
    assert rows["https://hiveclub.ch"]["review_reason"] == "Review pending"
    assert rows["https://hiveclub.ch"]["status"] == "needs_review"
    assert read_json(tmp_path / "open_calls" / "reviews" / "fogstudio.ch.json")["model"] == "qwen2.5-7b-test.gguf"
    assert not (tmp_path / "open_calls" / "reviews" / "hiveclub.ch.json").exists()

    # Running again only sends the unfinished site to the model
    calls.clear()
    fake_review.setattr(review_module, "ServerClassifier", fake_classifier(calls))
    review_module.review_open_calls(input_path, llm_slots=1, fetch_workers=1, data_dir=tmp_path)
    assert calls == [SITES["https://hiveclub.ch"]]
    assert all(row["large_model_status"] for row in read_rows(output_path) if row["website"] in SITES)


def test_atomic_writes_leave_no_partial_files(tmp_path):
    path = tmp_path / "reports" / "fogstudio.ch.json"
    write_json_atomic(path, {"site": "https://fogstudio.ch", "status": "open"})
    assert read_json(path)["status"] == "open"
    assert [file.name for file in path.parent.iterdir()] == ["fogstudio.ch.json"]

    # A half-written file from an interrupted older run is treated as "not checked" instead of crashing
    path.write_text('{"site": "https://fogst')
    assert read_json(path) is None
