"""The sites to search: watchlist.json "sites" (or the SITES environment variable).

Each entry is a site's address, e.g. "https://www.noon.com/egypt-en/", or an object like
{"url": "https://btech.com/en/", "enabled": false, "currency": "EGP", "key": "btech"}.
Noon, Amazon (any country) and Jumia Egypt have dedicated search readers. Any other site is
tried as a Shopify or WooCommerce store, then through its normal search page.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from .prices import parse_bool

DEFAULT_SITES = [
    "https://www.noon.com/egypt-en/",
    "https://www.amazon.eg/",
    "https://www.jumia.com.eg/",
]
AMAZON = {
    "amazon.eg": ("amazon_eg", "EGP"), "amazon.com": ("amazon_com", "USD"), "amazon.ae": ("amazon_ae", "AED"),
    "amazon.sa": ("amazon_sa", "SAR"), "amazon.co.uk": ("amazon_uk", "GBP"), "amazon.de": ("amazon_de", "EUR"),
}
NOON = {"egypt": ("noon", "EGP"), "uae": ("noon_uae", "AED"), "saudi": ("noon_saudi", "SAR")}
JUMIA = {"jumia.com.eg": ("jumia", "EGP")}


@dataclass
class Site:
    key: str        # name used in alerts, history and STORES / EXCLUDE_STORES: noon, amazon_eg, btech.com
    url: str        # base address, always ending in "/"
    host: str       # without www.
    netloc: str     # e.g. www.amazon.eg
    kind: str       # amazon | noon | jumia | generic
    currency: str
    enabled: bool = True

    @property
    def root(self) -> str:
        return f"https://{self.netloc}"


def parse_site(entry) -> tuple[Site | None, str | None]:
    data = {"url": entry} if isinstance(entry, str) else dict(entry) if isinstance(entry, dict) else None
    if data is None:
        return None, f"site entry {entry!r} is not an address"
    raw = str(data.get("url") or "").strip()
    if not raw:
        return None, "a site entry has no url"
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    netloc = parts.netloc.lower()
    if "." not in netloc:
        return None, f"{raw} is not a site address"
    host = netloc[4:] if netloc.startswith("www.") else netloc
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    kind, key, currency = "generic", host, ""
    for domain, (amazon_key, amazon_currency) in AMAZON.items():
        if host == domain or host.endswith("." + domain):
            kind, key, currency, path, netloc = "amazon", amazon_key, amazon_currency, "/", "www." + host
    if host == "noon.com":
        segments = [p for p in path.split("/") if p]
        country = (segments[0] if segments else "egypt-en").split("-")[0]
        key, currency = NOON.get(country, (f"noon_{country}", ""))
        kind, path, netloc = "noon", f"/{country}-en/", "www.noon.com"  # English titles match English names
    if host in JUMIA:
        kind, (key, currency), path, netloc = "jumia", JUMIA[host], "/", "www." + host
    site = Site(
        key=str(data.get("key") or key).strip().lower(),
        url=f"https://{netloc}{path}",
        host=host,
        netloc=netloc,
        kind=kind,
        currency=str(data.get("currency") or currency or "EGP").strip().upper(),
        enabled=parse_bool(data.get("enabled"), True),
    )
    return site, None


def apply_site_rows(configured: list[Site], rows) -> list[Site]:
    """Your Notion Sites rows switch the configured sites on or off (and may set a currency).

    The configured list (watchlist.json "sites" or SITES) decides which sites exist: a configured
    site without a row stays as configured, and a row for a site that isn't configured is ignored.
    A row matches by its Key (or Name) or by its URL, so a renamed row still works."""
    by_key: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        keys = [str(row.get("key") or "").strip().lower()]
        if row.get("url"):
            site, _ = parse_site({"url": row.get("url")})
            keys.append(site.key if site is not None else "")
        for key in keys:
            if key:
                by_key.setdefault(key, row)
    out = []
    for site in configured:
        row = by_key.get(site.key)
        if row is not None:
            currency = str(row.get("currency") or "").strip().upper() or site.currency
            site = replace(site, enabled=site.enabled and parse_bool(row.get("enabled"), True), currency=currency)
        out.append(site)
    return out


def load_sites(entries) -> tuple[list[Site], list[str]]:
    sites: list[Site] = []
    problems: list[str] = []
    for entry in entries or []:
        site, problem = parse_site(entry)
        if problem:
            problems.append(problem)
        elif any(s.key == site.key for s in sites):
            problems.append(f"{site.url}: site {site.key} is listed twice, kept the first")
        else:
            sites.append(site)
    return sites, problems
