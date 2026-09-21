"""
Answering "which site is this?" the same way everywhere: the crawler deciding whether a link stays on the site, the
checker keying a website so it isn't checked twice, and the link and scraper code matching hosts against a list.
"""
from urllib.parse import urlparse


def base_domain(host):
    """
    Get the host without a port or a leading "www.", so subdomains can be treated as part of the same site.

    :param host: The host name.
    :return: The bare host.
    """
    return host.lower().split(":")[0].removeprefix("www.")


def matches_domain(host, domains):
    """
    :param host: The host name.
    :param domains: Domains to match, e.g. SOCIAL_MEDIA_DOMAINS.
    :return: True if the host is one of them or a subdomain of one.
    """
    host = host.lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def website_key(url):
    """
    Key a website URL so http/https, "www." and trailing slashes don't create duplicate checks.

    :param url: The website URL.
    :return: The key, e.g. "fogstudio.ch" or "example.com/festival".
    """
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return f"{base_domain(parsed.netloc)}{parsed.path.rstrip('/')}"
