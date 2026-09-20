"""
Sanity-check the links and contacts in small model results: ping each URL, and check that each email's domain can
receive mail.

Results are written to *_check columns, one value per link in the same order as the link column, separated by " | ":

- ok: the URL responded (2xx or 3xx), or the email domain has a mail server.
- broken (404), broken (410), broken (DNS): the page or domain doesn't exist.
- blocked (403), blocked (429), ...: the site refused an automated request, so the link may still work in a browser.
- error (500), timeout, unreachable: a server or connection problem that may be temporary.
- redirected (<path>): the page sends visitors somewhere else once its JavaScript runs, e.g. a volunteer portal whose
  event page is gone and which lands everyone on its generic list of open shifts. A plain request sees only 200 OK, so
  application links are opened in a browser to catch this.
- form closed: the application form itself says it stopped accepting applications. Organisers often leave the page
  announcing a call unchanged, so the form is the better witness: an open call whose forms are all closed is closed.
- unverifiable: Instagram answers every profile URL, existing or not, with the same page to logged-out visitors.
- invalid: the email address is malformed.
- no mail server: the email domain exists but has no MX or A record.

Only "broken" changes a status: an open call whose announcement or open call links are all broken needs review.
Mailboxes themselves aren't checked: that requires talking to mail servers, which is unreliable and often blocked.
"""
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import dns.exception
import dns.resolver
import requests

from ..progress import progress
from ..ra.events import read_rows, split_links, write_rows
from .checker import FORM_CLOSED_RE

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TIMEOUT = 15
LINK_WORKERS = 16
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
DNS_ERROR_RE = re.compile(r"NameResolution|Name or service not known|nodename nor servname|getaddrinfo failed|"
                          r"No address associated|Failed to resolve", re.IGNORECASE)
UNVERIFIABLE_HOSTS = ("instagram.com",)

# CSV column holding the links -> column holding their check results
CHECK_COLUMNS = {"website": "website_check", "announcement_url": "announcement_url_check",
                 "open_call_url": "open_call_url_check", "promoter_instagrams": "instagram_check",
                 "contact_emails": "email_check"}

_local = threading.local()


def session():
    """
    :return: A requests Session for the current thread.
    """
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
        _local.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en,de;q=0.8,fr;q=0.7"})
    return _local.session


def check_url(url):
    """
    Check that a URL responds. HEAD first, then GET for servers that don't support HEAD or refuse it.

    :param url: The URL.
    :return: A check result, e.g. "ok", "broken (404)" or "blocked (403)".
    """
    host = urlparse(url).netloc.lower()
    if any(host == domain or host.endswith(f".{domain}") for domain in UNVERIFIABLE_HOSTS):
        return "unverifiable"
    try:
        response = session().head(url, timeout=TIMEOUT, allow_redirects=True)
        if response.status_code in (400, 403, 405, 406, 429, 501, 999):
            response = session().get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
            response.close()
    except requests.exceptions.Timeout:
        return "timeout"
    except requests.exceptions.ConnectionError as error:
        return "broken (DNS)" if DNS_ERROR_RE.search(str(error)) else "unreachable"
    except requests.exceptions.RequestException:
        return "unreachable"

    status = response.status_code
    if status < 400:
        return "ok"
    if status in (404, 410):
        return f"broken ({status})"
    if status in (401, 403, 429, 999):
        return f"blocked ({status})"
    return f"error ({status})"


def redirect_target(page_url, final_url):
    """
    Decide whether a page sent the visitor somewhere else, ignoring differences that keep the same page: the scheme,
    "www.", a trailing slash, the query and the fragment.

    :param page_url: The URL that was opened.
    :param final_url: The URL the browser ended on.
    :return: The path landed on, or None if it is the same page.
    """
    opened, landed = urlparse(page_url), urlparse(final_url)
    if opened.netloc.lower().removeprefix("www.") != landed.netloc.lower().removeprefix("www."):
        return final_url
    if opened.path.rstrip("/") == landed.path.rstrip("/"):
        return None
    return landed.path or "/"


def check_urls_in_browser(urls, timeout=TIMEOUT):
    """
    Open URLs in a headless browser to see where a visitor actually ends up. Some application portals answer every
    request with 200 OK and then redirect with JavaScript, so their links look fine to a plain request.

    :param urls: The URLs to open.
    :param timeout: Seconds to wait per page.
    :return: A dict of URL -> "ok", "redirected (<path>)" or None when the browser couldn't decide.
    """
    from playwright.sync_api import Error as PlaywrightError, sync_playwright

    results = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(user_agent=USER_AGENT)
            for url in progress(urls, desc="Opening application links", unit="link"):
                try:
                    page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
                    target = redirect_target(url, page.url)
                    if FORM_CLOSED_RE.search(page.inner_text("body")):
                        results[url] = "form closed"
                    else:
                        results[url] = f"redirected ({target})" if target else "ok"
                except PlaywrightError as error:
                    logger.debug("Could not open %s in a browser: %s", url, error)
                    results[url] = None
            browser.close()
    except Exception as error:
        # Without a browser the HTTP checks still stand
        logger.warning("Skipping the browser link check: %s", error)
    return results


def check_email(email):
    """
    Check that an email address is well formed and its domain can receive mail (MX record, or an A record as fallback).

    :param email: The email address.
    :return: A check result: "ok", "invalid", "broken (DNS)", "no mail server" or "timeout".
    """
    if not EMAIL_RE.fullmatch(email):
        return "invalid"
    domain = email.rsplit("@", 1)[1].lower()
    resolver = dns.resolver.Resolver()
    resolver.lifetime = TIMEOUT
    for record_type in ("MX", "A"):
        try:
            resolver.resolve(domain, record_type)
            return "ok"
        except dns.resolver.NXDOMAIN:
            return "broken (DNS)"
        except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            continue
        except dns.exception.Timeout:
            return "timeout"
    return "no mail server"


def check_rows(rows, workers=LINK_WORKERS, in_browser=True):
    """
    Check every link and email in result rows, each unique one once, and add the *_check columns. An open call whose
    announcement pages or open call links are all broken is changed to needs_review.

    :param rows: Result rows, updated in place.
    :param workers: Links checked at the same time.
    :param in_browser: Also open the application links of found calls in a browser, to catch JavaScript redirects.
    :return: The rows.
    """
    targets = {(value, column == "contact_emails") for row in rows for column in CHECK_COLUMNS
               for value in split_links(row.get(column))}
    checked = {}
    with ThreadPoolExecutor(workers, thread_name_prefix="link-check") as pool, \
            progress(total=len(targets), desc="Link checks", unit="link") as bar:
        futures = {pool.submit(check_email if is_email else check_url, value): (value, is_email)
                   for value, is_email in targets}
        for future in as_completed(futures):
            checked[futures[future]] = future.result()
            bar.update(1)

    # Only the application links of calls that were found: opening a page in a browser costs seconds, not milliseconds
    if in_browser:
        application_links = [value for row in rows if row.get("status") in ("open", "needs_review")
                             for value in split_links(row.get("open_call_url"))
                             if checked.get((value, False)) == "ok"]
        for url, result in check_urls_in_browser(list(dict.fromkeys(application_links))).items():
            if result:
                checked[(url, False)] = result

    for row in rows:
        for column, check_column in CHECK_COLUMNS.items():
            row[check_column] = " | ".join(checked[(value, column == "contact_emails")]
                                           for value in split_links(row.get(column)))

        broken = [column for column in ("announcement_url", "open_call_url")
                  if row[CHECK_COLUMNS[column]] and all(result.startswith("broken")
                                                        for result in row[CHECK_COLUMNS[column]].split(" | "))]
        if row["status"] == "open" and broken:
            row["status"] = "needs_review"
            row["status_reason"] = f"{row['status_reason']}; link check: {' and '.join(broken)} broken"

        # The form outranks the page announcing the call: organisers often leave "applications are open" behind
        application_checks = [result for result in row[CHECK_COLUMNS["open_call_url"]].split(" | ") if result]
        if row["status"] == "open" and application_checks and "form closed" in application_checks and \
                "ok" not in application_checks:
            row["status"] = "closed"
            row["status_reason"] = f"{row['status_reason']}; link check: the application form says it is closed"
    logger.info("Checked %d links and emails", len(targets))
    return rows


def check_links_in_csv(path, columns):
    """
    Add link check columns to a results CSV in place.

    :param path: The results CSV Path.
    :param columns: The CSV columns to write, including the *_check columns.
    :return: The path.
    """
    write_rows(check_rows(read_rows(path)), path, columns)
    return path
