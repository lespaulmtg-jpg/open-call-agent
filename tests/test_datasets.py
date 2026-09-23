"""
The Datasets view: what each dataset covers and which steps have been run over it. A check that was stopped must be
shown as stopped, not as a finished dataset. No network and no model.
"""
import re
import shutil

import pytest

from dashboard.datasets import dataset_stats, list_datasets
from dashboard.sources import list_sources
from open_call_agent.open_call.batch import RESULT_COLUMNS
from open_call_agent.open_call.review import REVIEW_COLUMNS
from open_call_agent.paths import reports_dir, reviews_dir, scraped_dir
from open_call_agent.ra.events import write_rows
from open_call_agent.storage import write_json_atomic

SMALL_MODEL_FILE = "qwen2.5-3b-instruct-q4_k_m.gguf"
LARGE_MODEL_FILE = "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf"


def result_row(website, status, **extra):
    return {column: "" for column in REVIEW_COLUMNS} | {"website": website, "status": status,
                                                        "checked_on": "2026-09-21"} | extra


def steps_of(dataset):
    return {step["label"]: (step["state"], step["detail"]) for step in dataset["steps"]}


@pytest.fixture
def checked_data(swiss_data_dir, tmp_path):
    """
    A copy of the Swiss festivals plus a check that was stopped part way: two sites were checked, nothing was link
    checked and the review never ran. Copied, because the scrape is shared with every other test and these results
    would otherwise show up in theirs.
    """
    shutil.copytree(scraped_dir(swiss_data_dir), scraped_dir(tmp_path))
    swiss_data_dir = tmp_path
    rows = [result_row("https://fogstudio.ch", "open", report_path="missing.json"),
            result_row("https://enorm.club", "none")]
    write_rows(rows, swiss_data_dir / "open_calls" / "open_calls_ch_festivals_2026-09-21.csv", RESULT_COLUMNS)
    for website in ("fogstudio.ch", "enorm.club"):
        write_json_atomic(reports_dir(swiss_data_dir) / f"{website}.json",
                          {"status": "none", "model": SMALL_MODEL_FILE, "checked_on": "2026-09-21"})
    return swiss_data_dir


@pytest.mark.network
def test_a_dataset_says_what_it_covers_and_what_was_run_over_it(checked_data):
    datasets = list_datasets(checked_data)
    check = next(dataset for dataset in datasets if dataset["kind"] == "results")
    facts = {fact["label"]: fact["value"] for fact in check["facts"]}
    assert facts["Rows"] == 2 and facts["Open"] == 1 and facts["None"] == 1
    assert check["source_id"] in {source["id"] for source in list_sources(checked_data)}, "it can be browsed"

    steps = steps_of(check)
    assert steps["Small model"][0] == "done" and SMALL_MODEL_FILE in steps["Small model"][1]
    assert "2026-09-21" in steps["Small model"][1], "when it was applied"
    assert steps["Link and email checks"][0] == "none", "they only run when a check finishes"
    assert steps["Large model review"] == ("none", "not run")


@pytest.mark.network
def test_a_scrape_says_how_much_of_it_has_been_checked(checked_data):
    scrape = next(dataset for dataset in list_datasets(checked_data) if dataset["kind"] == "scrape")
    facts = {fact["label"]: fact["value"] for fact in scrape["facts"]}
    assert facts["Countries"] == 1 and facts["Festivals"] >= 1 and facts["Events"] > facts["Festivals"]
    assert facts["Websites"] >= 2

    steps = steps_of(scrape)
    assert steps["Scraped from ra.co"][0] == "done" and "1 country" in steps["Scraped from ra.co"][1]
    assert steps["Festivals identified"][0] == "done"
    # Some of the scrape's websites have a report and the rest don't, which is what a stopped check looks like
    assert steps["Websites checked"][0] == "partial"
    checked, total = re.match(r"(\d+) of ([\d,]+) festival websites", steps["Websites checked"][1]).groups()
    assert 0 < int(checked) < int(total.replace(",", "")) == facts["Websites"]


@pytest.mark.network
def test_a_reviewed_check_names_both_models(checked_data):
    rows = [result_row("https://fogstudio.ch", "open", small_model_status="open", large_model_status="open",
                       website_check="ok", report_path="missing.json")]
    write_rows(rows, checked_data / "open_calls" / "open_calls_ch_festivals_2026-09-21_reviewed.csv", REVIEW_COLUMNS)
    write_json_atomic(reviews_dir(checked_data) / "fogstudio.ch.json",
                      {"website": "https://fogstudio.ch", "model": LARGE_MODEL_FILE, "review": {}})

    reviewed = next(dataset for dataset in list_datasets(checked_data) if "reviewed" in dataset["label"])
    steps = steps_of(reviewed)
    assert steps["Link and email checks"] == ("done", "1 of 1 rows checked")
    assert steps["Large model review"][0] == "done" and LARGE_MODEL_FILE in steps["Large model review"][1]
    assert "1 of 1 open or needs-review sites" in steps["Large model review"][1]


@pytest.mark.network
def test_the_figures_at_the_top(checked_data):
    datasets = list_datasets(checked_data)
    stats = {stat["label"]: stat["value"] for stat in dataset_stats(datasets)}
    assert stats["Datasets"] == len(datasets) and stats["Countries Scraped"] == 1
    assert stats["Websites Checked"] == 2 and stats["Festivals Scraped"] >= 1


def test_no_data_is_not_an_error(tmp_path):
    assert list_datasets(tmp_path) == []
    assert [stat["value"] for stat in dataset_stats([])] == [0, 0, 0, 0]
