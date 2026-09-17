"""
Sanity-check the links and contacts in small model results: ping each URL, and check that each email's domain can
receive mail.

Results are written to *_check columns, one value per link in the same order as the link column, separated by " | ":

- ok: the URL responded (2xx or 3xx), or the email domain has a mail server.
- broken (404), broken (410), broken (DNS): the page or domain doesn't exist.
- blocked (403), blocked (429), ...: the site refused an automated request, so the link may still work in a browser.
- error (500), timeout, unreachable: a server or connection problem that may be temporary.
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


def check_rows(rows, workers=LINK_WORKERS):
    """
    Check every link and email in result rows, each unique one once, and add the *_check columns. An open call whose
    announcement pages or open call links are all broken is changed to needs_review.

    :param rows: Result rows, updated in place.
    :param workers: Links checked at the same time.
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
