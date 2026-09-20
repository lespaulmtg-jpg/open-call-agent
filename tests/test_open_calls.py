import json
import re
from pathlib import Path

import pytest

from open_call_agent.open_call.batch import row_websites

from open_call_agent.cli import main
from open_call_agent.open_call.batch import STATUSES, check_open_calls, collect_websites, website_key
from open_call_agent.open_call.review import check_and_review_open_calls
from open_call_agent.paths import FESTIVALS_PREFIX, find_csvs
from open_call_agent.ra.events import read_rows

from .conftest import COUNTRY

pytestmark = pytest.mark.network


def test_website_key_ignores_scheme_www_and_trailing_slash():
    assert website_key("http://www.fogstudio.ch/") == website_key("https://fogstudio.ch") == "fogstudio.ch"


def test_collect_websites_from_festivals(swiss_data_dir):
    sites = collect_websites(find_csvs(swiss_data_dir, FESTIVALS_PREFIX, COUNTRY))
    assert "fogstudio.ch" in sites
    assert "FOG Festival 2026" in sites["fogstudio.ch"]["event_names"]


def test_check_open_calls_requires_existing_data(tmp_path):
    with pytest.raises(FileNotFoundError):
        check_open_calls(tmp_path, COUNTRY)


@pytest.mark.llm
def test_swiss_festival_open_calls_small_then_large_model(swiss_data_dir, monkeypatch):
    # fogstudio.ch disallows crawlers in robots.txt, but has an open call at /proposals linking to a Google Form.
    # The large model's server can only start once the small model's has released the GPU (GPUInUseError otherwise).
    first_pass_path, reviewed_path = check_and_review_open_calls(swiss_data_dir, country_code=COUNTRY,
                                                                 festivals_only=True, ignore_robots=True)
    first_pass = {website_key(row["website"]): row for row in read_rows(first_pass_path)}
    reviewed = {website_key(row["website"]): row for row in read_rows(reviewed_path)}
    assert set(first_pass) == set(reviewed)
    assert all(row["status"] in STATUSES for row in list(first_pass.values()) + list(reviewed.values()))

    for fog in (first_pass["fogstudio.ch"], reviewed["fogstudio.ch"]):
        assert fog["status"] == "open"
        assert fog["announcement_url"].rstrip("/").endswith("/proposals")
        assert "docs.google.com/forms" in fog["open_call_url"]
        assert "?page_id=" not in fog["open_call_url"], "menu link counted as an open call link"
    # Deadlines use one format: YYYY-MM-DD, several separated by " | "; the page's wording is kept in deadline_text
    for row in list(first_pass.values()) + list(reviewed.values()):
        assert re.fullmatch(r"(\d{4}-\d{2}-\d{2}( \| \d{4}-\d{2}-\d{2})*)?", row["deadline"]), row["deadline"]
    # Links the small model found were pinged: the announcement page and Google Form respond
    for fog in (first_pass["fogstudio.ch"], reviewed["fogstudio.ch"]):
        assert fog["announcement_url_check"] == "ok" and fog["open_call_url_check"] == "ok"
        assert fog["website_check"] in ("ok", "blocked (403)")
        assert set(fog["instagram_check"].split(" | ")) <= {"unverifiable", ""}
        assert fog["email_check"] and "invalid" not in fog["email_check"]
    assert reviewed["fogstudio.ch"]["small_model_status"] == "open"
    assert reviewed["fogstudio.ch"]["large_model_status"] == "open"
    assert reviewed["fogstudio.ch"]["review_quotes"]

    # Rows the small model didn't flag are passed through without a large-model status
    for key, row in reviewed.items():
        if first_pass[key]["status"] not in ("open", "needs_review"):
            assert row["status"] == first_pass[key]["status"] and not row["large_model_status"]

    # Sites already checked or reviewed by the same model aren't checked again: a second run must not load any model
    import open_call_agent.open_call.batch as batch_module
    import open_call_agent.open_call.review as review_module

    def no_model(*args, **kwargs):
        raise AssertionError("a model was loaded for sites that were already checked")

    monkeypatch.setattr(batch_module, "create_classifier", no_model)
    monkeypatch.setattr(review_module, "LlamaServer", no_model)
    rerun_first_pass, rerun_reviewed = check_and_review_open_calls(swiss_data_dir, country_code=COUNTRY,
                                                                   festivals_only=True, ignore_robots=True,
                                                                   output_path=swiss_data_dir / "rerun.csv")
    rerun_statuses = {website_key(row["website"]): row["status"] for row in read_rows(rerun_reviewed)}
    if all(first_pass[key]["report_path"] and json.loads(Path(first_pass[key]["report_path"]).read_text())["model"]
           for key in first_pass):
        assert rerun_statuses == {key: row["status"] for key, row in reviewed.items()}


def test_only_one_model_holds_the_gpu():
    from open_call_agent.open_call.classifier import GPUInUseError, acquire_gpu, release_gpu

    acquire_gpu("small model")
    try:
        with pytest.raises(GPUInUseError):
            acquire_gpu("large model")
    finally:
        release_gpu("small model")
    acquire_gpu("large model")
    release_gpu("large model")


def test_cli_check_open_calls_without_data_fails(tmp_path, capsys):
    assert main(["--data-dir", str(tmp_path), "check-open-calls", "--country", COUNTRY]) == 1
    assert "Run the scraper first" in capsys.readouterr().err


def test_mailto_links_are_decoded():
    from open_call_agent.open_call.checker import Page

    # fogstudio.ch links to "mailto:%20info@fogstudio.ch"
    page = Page("https://www.fogstudio.ch/contact-us", '<a href="mailto:%20info@fogstudio.ch">Email</a>', False)
    assert page.emails == {"info@fogstudio.ch"}


def test_linked_form_decides_whether_a_described_call_is_open():
    from open_call_agent.open_call.review import page_status

    # fogstudio.ch: the 7B model called "send a proposal for 2027" not yet open, but its Google Form accepts responses
    call = {"status": "not_yet_open", "verified_quotes": ["If you want to send a proposal for 2027, use the following link."]}
    assert page_status(call, True, "accepting") == "open"
    assert page_status(call, True, "closed") == "closed"
    assert page_status(call, True, None) == "needs_review"
    # An accepting form doesn't make a page that describes no call open (m4music.ch information event)
    assert page_status({"status": "none", "verified_quotes": []}, True, "accepting") == "none"


def test_review_does_not_process_announcements_older_than_the_cutoff():
    from open_call_agent.open_call.review import review_row

    row = {"website": "https://www.hiveclub.ch", "status": "open", "status_reason": "Open call announced on 1 page(s)",
           "deadline": "2019-06-01", "deadline_text": "1 June 2019", "open_call_url": "",
           "announcement_url": "https://www.hiveclub.ch/open-call-2019", "opportunity_types": "dj", "how_to_apply": ""}
    evidence = {"announcements": [], "forms": [], "errors": [], "announcement_errors": 0,
                "too_old": ["Too old: https://www.hiveclub.ch/open-call-2019 was last modified or created 2019-05-01 "
                            "(sitemap lastmod), before 2025-01-01"]}
    reviewed = review_row(row, evidence, {})
    assert reviewed["status"] == "none" and reviewed["small_model_status"] == "open"
    assert reviewed["deadline"] == "" and reviewed["review_reason"].startswith("Not processed: Too old:")

    # If the page couldn't be fetched at all, it still needs review
    evidence = {**evidence, "too_old": [], "errors": ["Could not fetch https://www.hiveclub.ch/open-call-2019"],
                "announcement_errors": 1}
    assert review_row(row, evidence, {})["status"] == "needs_review"


def test_broken_links_send_an_open_call_to_review(monkeypatch):
    from open_call_agent.open_call import links

    responses = {"https://www.fogstudio.ch/proposals": "broken (404)",
                 "https://docs.google.com/forms/d/e/fog/viewform": "ok",
                 "https://fogstudio.ch": "ok", "https://www.instagram.com/fog_festival": "unverifiable"}
    monkeypatch.setattr(links, "check_url", lambda url: responses[url])
    monkeypatch.setattr(links, "check_email", lambda email: "ok")
    row = {"website": "https://fogstudio.ch", "status": "open", "status_reason": "Open call announced on 1 page(s)",
           "announcement_url": "https://www.fogstudio.ch/proposals",
           "open_call_url": "https://docs.google.com/forms/d/e/fog/viewform",
           "promoter_instagrams": "https://www.instagram.com/fog_festival", "contact_emails": "info@fogstudio.ch"}
    [checked] = links.check_rows([dict(row)], in_browser=False)
    assert checked["status"] == "needs_review" and "announcement_url broken" in checked["status_reason"]
    assert (checked["announcement_url_check"], checked["open_call_url_check"], checked["instagram_check"],
            checked["email_check"]) == ("broken (404)", "ok", "unverifiable", "ok")

    # Blocked or unreachable links don't change the status: they may work in a browser
    responses["https://www.fogstudio.ch/proposals"] = "blocked (403)"
    assert links.check_rows([dict(row)], in_browser=False)[0]["status"] == "open"


def test_application_links_are_opened_in_a_browser(monkeypatch):
    """
    Some portals answer every request with 200 OK and then redirect with JavaScript, e.g. helfereinsatz.ch sends
    visitors from a festival's shift page to its generic list of open shifts once that festival is over.
    """
    from open_call_agent.open_call import links

    assert links.redirect_target("https://portal.ch/kfka/de/browse/open/event/210209",
                                 "https://portal.ch/kfka/de/a/open") == "/kfka/de/a/open"
    assert links.redirect_target("https://portal.ch/x", "https://www.portal.ch/x/?lang=de") is None, "the same page"
    assert links.redirect_target("https://portal.ch/x", "https://other.com/x") == "https://other.com/x"

    monkeypatch.setattr(links, "check_url", lambda url: "ok")
    monkeypatch.setattr(links, "check_email", lambda email: "ok")
    monkeypatch.setattr(links, "check_urls_in_browser",
                        lambda urls, **kwargs: {url: "redirected (/kfka/de/a/open)" for url in urls})
    row = {"website": "https://www.detectclassic.com", "status": "open", "status_reason": "Open call on 2 page(s)",
           "announcement_url": "https://detectclassic.com/betweenmountains/about/mitmachen",
           "open_call_url": "https://portal.helfereinsatz.ch/kfka/de/browse/open/event/210209",
           "promoter_instagrams": "", "contact_emails": ""}

    [checked] = links.check_rows([dict(row)], in_browser=True)
    assert checked["open_call_url_check"] == "redirected (/kfka/de/a/open)"
    assert checked["announcement_url_check"] == "ok", "only application links are opened in a browser"

    # A site the models found nothing on isn't worth opening
    quiet = dict(row, status="none")
    monkeypatch.setattr(links, "check_urls_in_browser", lambda urls, **kwargs: {url: "redirected (/x)" for url in urls})
    assert links.check_rows([quiet], in_browser=True)[0]["open_call_url_check"] == "ok"


def test_a_closed_form_closes_the_call(monkeypatch):
    """
    Organisers leave "applications are open" on their pages long after the form stops accepting them, as Between
    Mountains did, so the form itself decides.
    """
    from open_call_agent.open_call import links

    assert links.FORM_CLOSED_RE.search("Sorry this form Volunteer Application Form - ENG is now closed")
    assert links.FORM_CLOSED_RE.search("This form is no longer accepting responses")
    assert not links.FORM_CLOSED_RE.search("Fill in the form to apply, applications open until June")

    monkeypatch.setattr(links, "check_url", lambda url: "ok")
    monkeypatch.setattr(links, "check_email", lambda email: "ok")
    row = {"website": "https://www.detectclassic.com", "status": "open", "status_reason": "Open call on 2 page(s)",
           "announcement_url": "https://detectclassic.com/betweenmountains/about/mitmachen",
           "open_call_url": "https://portal.helfereinsatz.ch/x, https://detectclassic.festivalpro.com/form/y",
           "promoter_instagrams": "", "contact_emails": ""}
    states = {"https://portal.helfereinsatz.ch/x": "redirected (/a/open)",
              "https://detectclassic.festivalpro.com/form/y": "form closed"}
    monkeypatch.setattr(links, "check_urls_in_browser", lambda urls, **kwargs: {url: states[url] for url in urls})

    [checked] = links.check_rows([dict(row)], in_browser=True)
    assert checked["open_call_url_check"] == "redirected (/a/open) | form closed"
    assert checked["status"] == "closed" and "form says it is closed" in checked["status_reason"]

    # One form still accepting keeps the call open
    states["https://detectclassic.festivalpro.com/form/y"] = "ok"
    assert links.check_rows([dict(row)], in_browser=True)[0]["status"] == "open"


def test_row_websites_put_the_festivals_own_site_first():
    """
    The checker crawls the festival's own website before its promoters', which can cover several festivals.
    """
    row = {"Event Website": "http://betweenmountains.ch/", "Promoter Websites": "https://www.detectclassic.com"}
    assert row_websites(row) == ["http://betweenmountains.ch/", "https://www.detectclassic.com"]
    assert row_websites({"Promoter Websites": "https://fogstudio.ch"}) == ["https://fogstudio.ch"]
    assert row_websites({"Event Website": "https://x.ch", "Promoter Websites": "https://x.ch"}) == ["https://x.ch"]
    assert row_websites({}) == []
