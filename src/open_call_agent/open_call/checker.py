"""
Check whether a website currently has an open call (for music, visuals, workshops, etc.).

Pipeline per site:
1. Discover URLs: robots.txt / sitemap.xml plus homepage links, with a prioritised BFS crawl as fallback.
2. Fetch pages statically; re-render with a headless browser when the HTML looks JS-rendered or blocked.
3. Shortlist pages with multilingual keyword regexes (EN/DE/FR/IT), plus a few "likely" pages
   (About, Contact, Get Involved, ...) even without a keyword hit.
4. Classify shortlisted pages with a LocalClassifier. A page only counts as an open call if the model's quote is
   found verbatim in the page text and the call is currently open.
5. Report a site verdict alongside coverage and errors, so "couldn't check" is never reported as "no".
"""
import gzip
import logging
import re
import time
from collections import deque
from datetime import date
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, unquote, urlencode
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from ..urls import base_domain, matches_domain
from .classifier import MAX_CHUNKS_PER_PAGE, most_positive, split_text, verify
from .dates import MIN_PAGE_DATE, format_date, html_page_dates, http_last_modified, is_too_old
from .text import normalize_text

logger = logging.getLogger(__name__)

USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
REQUEST_TIMEOUT = 20
RENDER_TIMEOUT_MS = 15000  # Headless browser page load
MAX_CRAWL_SECONDS = 120  # Per site; one slow JavaScript site took 500s without a limit
DELAY = 0.5  # Seconds between requests to the same site

# Strong signals: wording that describes a call for participation. Deliberately excludes generic
# contact wording ("contact us", "email", "get in touch"), which appears on nearly every site.
STRONG_PATTERNS = [
    # English
    r"open[\s-]?calls?", r"call\s+(for|to)\s+(artists?|entries|submissions?|proposals?|applications?|participation|"
    r"projects?|works?|musicians?|djs?|performers?|creatives?|volunteers?|vendors?|makers?|contributors?)",
    r"(now\s+)?accepting\s+(submissions?|applications?|proposals?|entries|demos?|mixes)",
    r"(submissions?|applications?|entries)\s+(are\s+)?(now\s+)?open", r"open\s+for\s+(submissions?|applications?|proposals?)",
    r"(submit|send)\s+(us\s+)?(your|a|an)\s+(proposals?|projects?|applications?|work|mix|demo|idea|portfolio|pitch)",
    r"proposals?\s+for\s+(the\s+)?(20\d\d|next\s+(year|edition|season))", r"submission\s+guidelines",
    r"apply\s+(now|here|to\s+(play|perform|exhibit|join))", r"application\s+(form|deadline)", r"how\s+to\s+apply",
    r"we('re|\s+are)\s+looking\s+for\s+(artists?|djs?|musicians?|performers?|creatives?|volunteers?|vendors?|workshop)",
    # German
    r"ausschreibung(en)?", r"(jetzt\s+)?bewerben", r"bewirb\s+dich", r"bewerbung(en|sfrist|sformular)?", r"einreich(en|ung|ungen)",
    r"aufruf\s+(an|für)", r"wir\s+suchen\s+(künstler|djs?|musiker|performer|kreative|helfer|freiwillige)", r"mitmachen",
    # French
    r"appels?\s+(à|a)\s+(projets?|candidatures?|artistes?|participation|propositions?|contributions?)",
    r"candidatures?\s+(ouvertes?|en\s+ligne)", r"postule[rz]", r"déposer\s+(un|votre)\s+(dossier|projet)",
    # Italian
    r"band[oi]\b", r"invia\s+(la\s+tua|una)\s+(candidatura|proposta)", r"candidature\s+aperte",
]
STRONG_RE = re.compile("|".join(f"(?:{pattern})" for pattern in STRONG_PATTERNS), re.IGNORECASE)

# Pages likely to mention an opportunity without using any of the wording above
LIKELY_PAGE_RE = re.compile(
    r"about|contact|kontakt|get[-_ ]?involved|participat|mitmach|opportunit|apply|submit|submission|join|"
    r"volunteer|helfer|jobs?|karriere|careers?|info|faq|artists?|kuenstler|künstler|programm?e?|proposals?|"
    r"a-propos|propos|nous-rejoindre|rejoindre|candidat|chi-siamo|contatti|partecipa",
    re.IGNORECASE)

EMAIL_IN_TEXT_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SKIP_EXTENSIONS = re.compile(r"\.(jpe?g|png|gif|webp|svg|ico|css|js|mjs|json|xml|zip|rar|gz|mp3|mp4|mov|wav|webm|woff2?|ttf|eot)$",
                             re.IGNORECASE)
DOCUMENT_EXTENSIONS = re.compile(r"\.(pdf|docx?|odt|pptx?)$", re.IGNORECASE)
BLOCKED_PAGE_RE = re.compile(r"just a moment|attention required|checking your browser|access denied|captcha", re.IGNORECASE)
# Hosted form services commonly used for open call applications
FORM_URL_RE = re.compile(r"docs\.google\.com/forms|forms\.gle|typeform\.com|jotform\.com|airtable\.com/(app|shr)|tally\.so|"
                         r"cognitoforms\.com|formstack\.com|submittable\.com|forms\.office\.com|wufoo\.com|zohoforms|"
                         r"123formbuilder|paperform\.co|fillout\.com|festivalpro\.com/form|"
                         r"eventbrite\.[a-z.]+/e/.*call", re.IGNORECASE)
# What a form says once it stops accepting applications. "this form <its name> is now closed" puts the form's name
# between the words, so the wording is matched with the name allowed in between.
FORM_CLOSED_RE = re.compile(
    r"no longer accepting responses|not accepting responses|isn't accepting responses|"
    r"\b(form|survey|umfrage|formular)\b[^.!?\n]{0,80}?\b(is |are |was |were )?(now |already )?closed\b|"
    r"form (is )?closed|closed for (submissions|responses|applications)|"
    r"(applications?|submissions?|bewerbungen|bewerbungsphase|einreichungen) [^.!?\n]{0,40}?"
    r"(are closed|is closed|closed|vorbei|beendet|geschlossen)|"
    r"nimmt keine antworten mehr an|akzeptiert keine antworten|n'accepte plus (les |de )?réponses|"
    r"n'accepte plus de réponse|non accetta più risposte|no longer available", re.IGNORECASE)
# Link text that points to an application or submission, e.g. "Send us your proposal", "Jetzt bewerben"
CALL_LINK_TEXT_RE = re.compile(r"apply|application|submit|submission|proposal|open[\s-]?call|bewerb|einreich|anmeld|"
                               r"candidat|postule|propos|inscri|iscriv", re.IGNORECASE)
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|igsh)", re.IGNORECASE)



def normalize_url(url):
    """
    Normalise a URL for de-duplication: drop fragments and tracking parameters, and trailing slashes.

    :param url: The URL to normalise.
    :return: The normalised URL.
    """
    parsed = urlparse(url)
    query = urlencode([(key, value) for key, value in parse_qsl(parsed.query) if not TRACKING_PARAMS.match(key)])
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", query, ""))


def parse_sitemap_date(value):
    """
    :param value: A sitemap lastmod value (W3C datetime, e.g. "2026-09-16" or "2026-09-16T07:45:25+00:00").
    :return: A date, or None.
    """
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value or "")
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def open_call_urls(page, how_to_apply=""):
    """
    Get the links on a page that lead to an application: hosted forms first, then links whose text points to an
    application, then any page link the model named in how_to_apply.

    :param page: A Page.
    :param how_to_apply: The model's how_to_apply text.
    :return: A list of unique URLs.
    """
    named = [link for link, _ in page.links if link in (how_to_apply or "")]
    links = [link for link, _ in page.form_links] + [link for link, _ in page.call_links] + named
    return list(dict.fromkeys(link for link in links if normalize_url(link) != normalize_url(page.url)))


class Page:
    """
    A fetched page and the signals extracted from it.
    """

    def __init__(self, url, html, rendered_with_js, headers=None):
        self.url = url
        self.rendered_with_js = rendered_with_js
        soup = BeautifulSoup(html, "lxml")
        self.title = soup.title.get_text(strip=True) if soup.title else ""
        # (source, date) tuples for when the page was published or modified; read before scripts are removed
        self.dates = html_page_dates(soup) + http_last_modified(headers)
        self.too_old = None
        self.links = []
        self.form_links = []
        self.call_links = []
        self.emails = set()

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"].strip()
            if href.lower().startswith("mailto:"):
                # mailto links are URL-encoded and may hold several addresses, e.g. "mailto:%20info@site.ch"
                self.emails.update(EMAIL_IN_TEXT_RE.findall(unquote(href[7:].split("?")[0])))
            elif not href.lower().startswith(("javascript:", "tel:", "#")):
                self.links.append((urljoin(url, href), anchor.get_text(" ", strip=True)))
                link_text = anchor.get_text(" ", strip=True)
                if FORM_URL_RE.search(href):
                    self.form_links.append((urljoin(url, href), link_text))
                elif CALL_LINK_TEXT_RE.search(link_text) or STRONG_RE.search(link_text):
                    self.call_links.append((urljoin(url, href), link_text))

        for element in soup(["script", "style", "noscript", "template", "svg", "iframe"]):
            element.decompose()
        self.text = re.sub(r"\n\s*\n+", "\n", soup.get_text("\n", strip=True))
        self.emails.update(EMAIL_IN_TEXT_RE.findall(self.text))
        self.set_content_text(self.text)
        self.noscript_js_warning = bool(re.search(r"enable\s+javascript|javascript\s+(is\s+)?(required|disabled)",
                                                  html, re.IGNORECASE))

    def set_content_text(self, content_text):
        """
        Set the page's main content (text without site-wide menus and footers) and its keyword hits.

        :param content_text: The page text with boilerplate lines removed.
        """
        self.content_text = content_text
        self.keyword_hits = sorted({match.group(0).strip().lower() for match in STRONG_RE.finditer(content_text)})

    def looks_thin(self):
        """
        Whether the static HTML looks like an unrendered JavaScript app or a bot challenge.

        :return: True if the page should be re-rendered with a headless browser.
        """
        if BLOCKED_PAGE_RE.search(self.title) or (len(self.text) < 1500 and BLOCKED_PAGE_RE.search(self.text)):
            return True
        return len(self.text) < 300 or (self.noscript_js_warning and len(self.text) < 1500)


class SiteChecker:
    """
    Crawls one website and decides whether it has an open call.
    """

    def __init__(self, start_url, classifier=None, max_pages=40, max_llm_pages=12, ignore_robots=False,
                 max_crawl_seconds=MAX_CRAWL_SECONDS, min_page_date=MIN_PAGE_DATE):
        """
        :param start_url: The website to check.
        :param classifier: A loaded classifier, shared across sites; None for keyword matching only.
        :param max_pages: Maximum pages to fetch.
        :param max_llm_pages: Maximum pages to classify.
        :param ignore_robots: Fetch pages even if robots.txt disallows it.
        :param max_crawl_seconds: Stop crawling after this long; unvisited promising pages are reported.
        :param min_page_date: Don't process pages last modified or created before this date (pages without a date
            are processed). None processes every page.
        """
        if "://" not in start_url:
            start_url = f"https://{start_url}"
        self.start_url = normalize_url(start_url)
        self.domain = base_domain(urlparse(self.start_url).netloc)
        self.max_pages = max_pages
        self.max_crawl_seconds = max_crawl_seconds
        self.min_page_date = min_page_date
        self.max_llm_pages = max_llm_pages
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en,de;q=0.8,fr;q=0.7,it;q=0.6"})
        self.robots = None
        self.ignore_robots = ignore_robots
        self.render_all_with_js = False
        self.browser = None
        self.playwright = None

        self.pages = {}
        self.errors = []
        self.warnings = []
        self.classifier = classifier
        self.selected_pages = []
        self.skipped_by_robots = []
        self.skipped_by_date = []
        self.sitemap_lastmod = {}
        self.documents = {}
        self.sitemap_urls = set()
        self.discovery_method = None
        self.last_request_time = 0

    def is_internal(self, url):
        """
        Whether a URL belongs to the site being checked (including subdomains).

        :param url: The URL to check.
        :return: True if the URL is on the same site.
        """
        parsed = urlparse(url)
        host = base_domain(parsed.netloc)
        return parsed.scheme in ("http", "https") and matches_domain(host, [self.domain])

    def throttle(self):
        """
        Wait between requests so the site is not hammered.
        """
        elapsed = time.time() - self.last_request_time
        if elapsed < DELAY:
            time.sleep(DELAY - elapsed)
        self.last_request_time = time.time()

    def fetch_static(self, url):
        """
        Fetch a URL with requests.

        :param url: The URL to fetch.
        :return: The response, or None if the request failed.
        """
        self.throttle()
        try:
            return self.session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        except requests.exceptions.RequestException as error:
            self.errors.append({"url": url, "stage": "static_fetch", "error": str(error)})
            return None

    def fetch_rendered(self, url):
        """
        Fetch a URL with a headless Chromium browser so JavaScript-rendered content is included.

        :param url: The URL to fetch.
        :return: The rendered HTML, or None if rendering is unavailable or failed.
        """
        if self.browser is None:
            try:
                from playwright.sync_api import sync_playwright
                self.playwright = sync_playwright().start()
                self.browser = self.playwright.chromium.launch()
            except Exception as error:
                self.errors.append({"url": url, "stage": "js_render", "error": f"Headless browser unavailable: {error}"})
                return None

        self.throttle()
        page = self.browser.new_page(user_agent=USER_AGENT)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=RENDER_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass  # Sites with long-polling never reach network idle; use what has rendered so far
            return page.content()
        except Exception as error:
            try:
                # Sites with long-polling never reach network idle; take what has loaded so far
                return page.content() if page.content() else None
            except Exception:
                self.errors.append({"url": url, "stage": "js_render", "error": str(error)})
                return None
        finally:
            page.close()

    def load_robots(self):
        """
        Load robots.txt, returning any sitemap URLs it declares.

        :return: A list of sitemap URLs.
        """
        robots_url = urljoin(self.start_url, "/robots.txt")
        response = self.fetch_static(robots_url)
        if response is None or response.status_code != 200 or "html" in response.headers.get("Content-Type", ""):
            return []

        self.robots = RobotFileParser()
        self.robots.parse(response.text.splitlines())
        return re.findall(r"^\s*sitemap:\s*(\S+)", response.text, re.IGNORECASE | re.MULTILINE)

    def allowed_by_robots(self, url):
        """
        Whether robots.txt allows fetching a URL.

        :param url: The URL to check.
        :return: True if allowed (or no robots.txt).
        """
        return self.ignore_robots or self.robots is None or self.robots.can_fetch(USER_AGENT, url)

    def discover_sitemap_urls(self):
        """
        Collect page URLs from the site's sitemaps, following sitemap indexes.
        """
        queue = deque(self.load_robots() or [urljoin(self.start_url, path) for path in
                                              ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")])
        seen_sitemaps = set()

        while queue and len(seen_sitemaps) < 25:
            sitemap_url = queue.popleft()
            if sitemap_url in seen_sitemaps:
                continue
            seen_sitemaps.add(sitemap_url)

            response = self.fetch_static(sitemap_url)
            if response is None or response.status_code != 200:
                continue
            content = response.content
            if content[:2] == b"\x1f\x8b":
                content = gzip.decompress(content)
            text = content.decode("utf-8", errors="replace")
            if "<urlset" not in text and "<sitemapindex" not in text:
                continue

            for entry in re.findall(r"<(?:url|sitemap)>(.*?)</(?:url|sitemap)>", text, re.IGNORECASE | re.DOTALL) or [text]:
                lastmod = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", entry, re.IGNORECASE | re.DOTALL)
                for loc in re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", entry, re.IGNORECASE | re.DOTALL):
                    loc = loc.replace("&amp;", "&")
                    if "<sitemapindex" in text or re.search(r"\.xml(\.gz)?$", loc, re.IGNORECASE):
                        queue.append(loc)
                    elif self.is_internal(loc):
                        self.sitemap_urls.add(normalize_url(loc))
                        lastmod_date = parse_sitemap_date(lastmod.group(1)) if lastmod else None
                        if lastmod_date:
                            self.sitemap_lastmod[normalize_url(loc)] = lastmod_date

        # Some sitemaps stamp every URL with the time the sitemap was generated; that says nothing about the pages
        if len(self.sitemap_lastmod) >= 5 and len(set(self.sitemap_lastmod.values())) == 1:
            self.warnings.append("Sitemap lastmod dates are all identical and were ignored.")
            self.sitemap_lastmod = {}

    @staticmethod
    def priority(url, link_text=""):
        """
        Score a URL for crawl order: strong keyword hits first, then likely pages, then everything else.

        :param url: The URL.
        :param link_text: The anchor text of a link pointing to the URL.
        :return: A score, higher is more promising.
        """
        haystack = f"{urlparse(url).path.replace('-', ' ').replace('_', ' ')} {link_text}"
        if STRONG_RE.search(haystack) or re.search(r"open[-_ ]?call|ausschreibung|appel", url, re.IGNORECASE):
            return 3
        if LIKELY_PAGE_RE.search(haystack):
            return 2
        return 1 if urlparse(url).path.count("/") <= 2 else 0

    def fetch_page(self, url):
        """
        Fetch and parse a page, falling back to a headless browser for thin, JS-rendered or blocked pages.

        :param url: The URL to fetch.
        :return: A Page, or None if it could not be fetched.
        """
        if not self.render_all_with_js:
            response = self.fetch_static(url)
            if response is not None:
                content_type = response.headers.get("Content-Type", "")
                if response.status_code == 200 and "html" not in content_type:
                    return None
                if response.status_code == 200:
                    page = Page(normalize_url(response.url), response.text, rendered_with_js=False,
                                headers=response.headers)
                    if not page.looks_thin():
                        return page
                elif response.status_code == 404:
                    return None
                elif response.status_code not in (403, 429, 503):
                    self.errors.append({"url": url, "stage": "static_fetch", "error": f"HTTP {response.status_code}"})
                    return None

        html = self.fetch_rendered(url)
        if html is None:
            return None
        page = Page(url, html, rendered_with_js=True)
        if BLOCKED_PAGE_RE.search(page.title) or (len(page.text) < 300 and BLOCKED_PAGE_RE.search(page.text)):
            self.errors.append({"url": url, "stage": "fetch", "error": "Blocked by bot protection"})
            return None
        return page

    def crawl(self):
        """
        Crawl the site: sitemap URLs plus link discovery, visiting the most promising pages first.
        """
        # Resolve redirects (e.g. http -> https://www.) so robots.txt, sitemaps and the homepage use the final host
        response = self.fetch_static(self.start_url)
        if response is not None and self.is_internal(response.url):
            self.start_url = normalize_url(response.url)

        self.discover_sitemap_urls()
        self.discovery_method = "sitemap + links" if self.sitemap_urls else "link crawl (no sitemap found)"

        # Candidates are (priority, depth, url); the highest priority, then the shallowest, is fetched first
        candidates = {self.start_url: (4, 0)}
        for url in self.sitemap_urls:
            candidates.setdefault(url, (self.priority(url), 1))

        visited = set()
        crawl_started = time.time()
        while candidates and len(self.pages) < self.max_pages:
            if time.time() - crawl_started > self.max_crawl_seconds:
                self.warnings.append(f"Crawl time budget ({self.max_crawl_seconds}s) reached after {len(self.pages)} pages.")
                break
            url = max(candidates, key=lambda candidate: (candidates[candidate][0], -candidates[candidate][1]))
            score, depth = candidates.pop(url)
            visited.add(url)

            if not self.allowed_by_robots(url):
                self.skipped_by_robots.append(url)
                if url == self.start_url:
                    self.errors.append({"url": url, "stage": "homepage",
                                        "error": "robots.txt disallows crawling this site (use ignore_robots to override)"})
                continue

            # Skip pages the sitemap says haven't changed since the cutoff. The homepage is always fetched for its links.
            lastmod = self.sitemap_lastmod.get(url)
            if url != self.start_url and self.min_page_date and lastmod and lastmod < self.min_page_date:
                self.skipped_by_date.append({"url": url, "date": format_date(lastmod), "source": "sitemap lastmod"})
                continue

            page = self.fetch_page(url)
            if page is None:
                if url == self.start_url:
                    self.errors.append({"url": url, "stage": "homepage", "error": "Homepage could not be fetched"})
                continue
            if url == self.start_url and page.rendered_with_js:
                self.render_all_with_js = True
            if page.url in self.pages:
                continue
            self.pages[page.url] = page

            # Pages last modified or created before the cutoff are still used to find links, but not classified
            if lastmod:
                page.dates.append(("sitemap lastmod", lastmod))
            page.too_old = is_too_old(page.dates, self.min_page_date)
            if page.too_old:
                self.skipped_by_date.append({"url": page.url, "date": format_date(page.too_old[1]),
                                             "source": page.too_old[0]})

            for link, link_text in page.links:
                link = normalize_url(link)
                if not self.is_internal(link) or link in visited:
                    continue
                if DOCUMENT_EXTENSIONS.search(urlparse(link).path):
                    if STRONG_RE.search(f"{link} {link_text}"):
                        self.documents[link] = link_text
                    continue
                if SKIP_EXTENSIONS.search(urlparse(link).path) or depth >= 4:
                    continue
                link_score = self.priority(link, link_text)
                if link not in candidates or candidates[link][0] < link_score:
                    candidates[link] = (link_score, depth + 1)

        unvisited_priority = [url for url, (score, _) in candidates.items() if score >= 3]
        if unvisited_priority:
            self.warnings.append(f"Crawl stopped by its page or time budget with {len(unvisited_priority)} unvisited "
                                 f"pages whose URL or link text suggests an open call: {unvisited_priority[:5]}")
        if not self.sitemap_urls and len(self.pages) < 3:
            self.warnings.append("No sitemap and fewer than 3 pages found by crawling; coverage may be shallow.")

    def chunk_texts(self, page):
        """
        Split a page's main content (plus any form links) into chunks for the classifier.

        :param page: The page to classify.
        :return: A list of text chunks, at least one.
        """
        text = page.content_text
        if page.form_links:
            text += "\n\nForm links on this page:\n" + "\n".join(f"- {link_text}: {link}" for link, link_text in page.form_links)
        chunks = split_text(text)
        if len(chunks) > MAX_CHUNKS_PER_PAGE:
            self.warnings.append(f"{page.url}: only the first {MAX_CHUNKS_PER_PAGE} of {len(chunks)} text chunks were classified.")
            chunks = chunks[:MAX_CHUNKS_PER_PAGE]
        return chunks

    def classify_chunk(self, page, text):
        """
        Ask the classifier whether a piece of page text announces an open call.

        :param page: The page the text comes from.
        :param text: The page text (or a chunk of it).
        :return: The classification dict, or None if generation failed.
        """
        try:
            return self.classifier.classify(page.url, page.title, text)
        except Exception as error:  # Context overflow, invalid output, server error, ...
            self.errors.append({"url": page.url, "stage": "llm", "error": str(error)})
            return None

    def combine_chunk_results(self, page, chunk_results):
        """
        Check the model's evidence in code and combine a page's chunk results: quotes must appear verbatim on the
        page, and a parsed deadline in the past means the call is closed.

        :param page: The classified page.
        :param chunk_results: One classification dict (or None if it failed) per chunk.
        :return: The page's classification dict, or None if any chunk failed.
        """
        if not chunk_results or None in chunk_results:
            return None

        page_text = normalize_text(page.text)
        for result in chunk_results:
            verify(result, page_text)

        # Keep the most positive chunk result, merging evidence from chunks with the same status
        best = most_positive(chunk_results)
        same_status = [result for result in chunk_results if result["status"] == best["status"]]
        best["opportunity_types"] = sorted({kind for result in same_status for kind in result["opportunity_types"]})
        best["verified_quotes"] = list(dict.fromkeys(quote for result in same_status for quote in result["verified_quotes"]))
        # A 3B model can over-read loosely worded pages; without a keyword hit a positive needs human review
        best["confidence"] = "high" if page.keyword_hits or page.form_links else "low"
        best["form_links"] = [link for link, _ in page.form_links]
        best["open_call_urls"] = open_call_urls(page, best["how_to_apply"])
        best["deadline_text"] = " | ".join(dict.fromkeys(result["deadline_text"] for result in same_status
                                                         if result["deadline_text"]))
        best["deadline"] = " | ".join(sorted({result["deadline"] for result in same_status if result["deadline"]}))
        return best

    def classify_page(self, page):
        """
        Classify a page chunk by chunk.

        :param page: The page to classify.
        :return: The classification dict, or None if any chunk failed.
        """
        return self.combine_chunk_results(page, [self.classify_chunk(page, chunk) for chunk in self.chunk_texts(page)])

    def remove_boilerplate(self):
        """
        Remove lines repeated across most pages (menus, footers) so the model and keyword matching see the main content.
        A menu item labelled "Open Call" then no longer counts as a keyword hit on every page.
        """
        if len(self.pages) < 3:
            return
        threshold = max(3, len(self.pages) * 0.6)
        line_counts = {}
        link_counts = {}
        for page in self.pages.values():
            for line in set(page.text.splitlines()):
                line_counts[line] = line_counts.get(line, 0) + 1
            for link in {normalize_url(link) for link, _ in page.call_links}:
                link_counts[link] = link_counts.get(link, 0) + 1
        boilerplate = {line for line, count in line_counts.items() if count >= threshold}
        menu_links = {link for link, count in link_counts.items() if count >= threshold}
        for page in self.pages.values():
            page.set_content_text("\n".join(line for line in page.text.splitlines() if line not in boilerplate))
            page.call_links = [(link, text) for link, text in page.call_links if normalize_url(link) not in menu_links]

    def select_pages_for_llm(self):
        """
        Pick pages to classify: keyword hits first, then likely pages (About, Contact, ...), then the homepage.

        :return: A list of pages.
        """
        self.remove_boilerplate()
        current_pages = [page for page in self.pages.values() if not page.too_old]
        keyword_pages = sorted((page for page in current_pages if page.keyword_hits or page.form_links),
                               key=lambda page: -len(page.keyword_hits))
        likely_pages = [page for page in current_pages
                        if page not in keyword_pages and LIKELY_PAGE_RE.search(f"{urlparse(page.url).path} {page.title}")]
        homepage = [page for page in current_pages if page.url == self.start_url and page not in keyword_pages]

        selected = keyword_pages[:self.max_llm_pages]
        for page in homepage + likely_pages[:5]:
            if len(selected) >= self.max_llm_pages:
                break
            if page not in selected:
                selected.append(page)

        skipped_keyword_pages = keyword_pages[self.max_llm_pages:]
        if skipped_keyword_pages:
            self.warnings.append(f"{len(skipped_keyword_pages)} keyword-matching pages were not classified "
                                 f"(--max-llm-pages={self.max_llm_pages}).")
        return selected

    def prepare(self):
        """
        Crawl the site and pick the pages to classify. Network and browser work happens here, so sites can be
        prepared in parallel while classification runs elsewhere.

        :return: The pages to classify.
        """
        try:
            self.crawl()
            self.selected_pages = self.select_pages_for_llm()
            return self.selected_pages
        finally:
            self.close()

    def close(self):
        """
        Close the headless browser (if one was started) and the HTTP session.
        """
        if self.browser is not None:
            self.browser.close()
            self.playwright.stop()
            self.browser = self.playwright = None
        self.session.close()

    def finish(self, classifications=None):
        """
        Build the report from classification results.

        :param classifications: A dict of page URL -> classification dict (or None if it failed), or None when
            no classifier was used.
        :return: The report dict.
        """
        page_results = []
        for page in self.selected_pages:
            result = {"url": page.url, "title": page.title, "keyword_hits": page.keyword_hits,
                      "form_links": [link for link, _ in page.form_links]}
            if classifications is not None:
                result["classification"] = classifications.get(page.url)
            page_results.append(result)
        return self.build_report(page_results)

    def run(self):
        """
        Crawl and classify the site sequentially.

        :return: The report dict.
        """
        pages = self.prepare()
        if self.classifier is None:
            return self.finish()
        return self.finish({page.url: self.classify_page(page) for page in pages})

    def build_report(self, page_results):
        """
        Combine page results into a site status that separates "no" from "couldn't check":

        - open: a page announces a currently open call, with keyword or link evidence and a verified quote.
        - closed: calls were found, but all are closed.
        - needs_review: a possible call (not yet open, unclear, or open without keyword or link evidence), or the site
          couldn't be fully checked (blocked, errors, crawl budget). status_reason says which.
        - none: the site was checked and no call was found.

        :param page_results: Per-page keyword and classification results.
        :return: The report dict.
        """
        classified = [result for result in page_results if result.get("classification")]
        open_pages = [result for result in classified if result["classification"]["status"] == "open"
                      and result["classification"]["confidence"] == "high"]
        possible_pages = [result for result in classified
                          if result["classification"]["status"] in ("open", "not_yet_open", "unclear") and result not in open_pages]
        closed_pages = [result for result in classified if result["classification"]["status"] == "closed"]
        llm_failures = [result for result in page_results if self.classifier is not None and not result.get("classification")]
        homepage_errors = [error["error"] for error in self.errors if error["stage"] == "homepage"]
        incomplete_warnings = [warning for warning in self.warnings if "unvisited pages" in warning]

        if open_pages:
            status, reason, announcements = "open", f"Open call announced on {len(open_pages)} page(s)", open_pages
        elif homepage_errors or not self.pages:
            status, reason, announcements = "needs_review", f"Site could not be checked: {'; '.join(homepage_errors) or 'no pages fetched'}", []
        elif self.classifier is None:
            status, reason, announcements = "needs_review", "Not classified by a model; keyword hits are unverified", []
        elif possible_pages:
            kinds = sorted({result["classification"]["status"] if result["classification"]["status"] != "open"
                            else "open without keyword or link evidence" for result in possible_pages})
            status, reason, announcements = "needs_review", f"Possible call ({', '.join(kinds)})", possible_pages
        elif llm_failures or incomplete_warnings:
            problems = ([f"{len(llm_failures)} page(s) failed classification"] if llm_failures else []) + incomplete_warnings
            status, reason, announcements = "needs_review", f"Incomplete check: {'; '.join(problems)}", closed_pages
        elif closed_pages:
            status, reason, announcements = "closed", f"Only closed calls found on {len(closed_pages)} page(s)", closed_pages
        elif self.pages and all(page.too_old for page in self.pages.values()):
            status, reason, announcements = ("none", f"All {len(self.pages)} fetched pages were last modified or created "
                                             f"before {format_date(self.min_page_date)}", [])
        else:
            status, reason, announcements = "none", "No call found", []

        call_pages = [{"url": result["url"], **{key: result["classification"][key] for key in
                       ("status", "confidence", "opportunity_types", "verified_quotes", "deadline", "deadline_text", "how_to_apply",
                        "open_call_urls", "reasoning")}} for result in open_pages + possible_pages + closed_pages]
        return {
            "site": self.start_url,
            "checked_on": date.today().isoformat(),
            "status": status,
            "status_reason": reason,
            "opportunity_types": sorted({kind for result in announcements for kind in result["classification"]["opportunity_types"]}),
            "deadline": " | ".join(sorted({deadline for result in announcements
                                           for deadline in result["classification"]["deadline"].split(" | ") if deadline})),
            "deadline_text": " | ".join(dict.fromkeys(result["classification"]["deadline_text"] for result in announcements
                                                      if result["classification"]["deadline_text"])),
            "announcement_urls": [result["url"] for result in announcements],
            "open_call_urls": list(dict.fromkeys(url for result in announcements for url in result["classification"]["open_call_urls"])),
            "how_to_apply": " | ".join(dict.fromkeys(result["classification"]["how_to_apply"] for result in announcements
                                                     if result["classification"]["how_to_apply"])),
            "call_pages": call_pages,
            "coverage": {
                "discovery": self.discovery_method,
                "sitemap_urls_found": len(self.sitemap_urls),
                "pages_visited": len(self.pages),
                "js_rendered_pages": sum(page.rendered_with_js for page in self.pages.values()),
                "pages_classified": len(classified),
                "llm_used": self.classifier is not None,
                "skipped_by_robots": self.skipped_by_robots,
                "min_page_date": format_date(self.min_page_date),
                "skipped_by_date": self.skipped_by_date,
                "unchecked_documents": [{"url": url, "link_text": text} for url, text in self.documents.items()],
            },
            "contact_emails": sorted({email.lower() for page in self.pages.values() for email in page.emails}),
            "warnings": self.warnings,
            "errors": self.errors,
            "pages": page_results,
        }
