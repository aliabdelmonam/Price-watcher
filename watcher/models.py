"""Small data types shared by every module."""
from __future__ import annotations

from dataclasses import dataclass

PRIORITIES = ("high", "normal", "low")
NOT_LISTED = "not listed"  # a search found no matching listing on that site: normal, not an error


@dataclass
class Reading:
    """What every adapter returns. ok=False means 'could not read', never 'price changed'."""

    price: float | None = None       # current price
    list_price: float | None = None  # the 'was' price the store shows
    currency: str = ""
    in_stock: bool = False
    seller: str | None = None        # catches cheap third-party sellers
    title: str = ""
    ok: bool = False                 # False if blocked / page changed
    error: str | None = None
    source: str = ""                 # which strategy found the price (jsonld, css, search:noon...)
    http_status: int | None = None
    suspicious: bool = False         # set by the sanity check

    @classmethod
    def failed(cls, error: str, http_status: int | None = None, title: str = "") -> "Reading":
        return cls(ok=False, error=error, http_status=http_status, title=title)


@dataclass
class Item:
    """A Watchlist row. With a url it tracks that page; without one it is searched on every site."""

    id: str
    name: str
    url: str
    store: str
    target_price: float | None = None
    currency: str = "EGP"
    priority: str = "normal"         # high | normal | low
    alert_on_restock: bool = True
    enabled: bool = True
    notes: str = ""
    only_seller: str = ""            # optional column: alert only when this seller has the offer
    row: str = ""                    # where the item came from, for messages
    named: bool = True               # False: no name given, the page title is used
    # search mode (rows without a url)
    search_query: str = ""           # optional 'query' column; default: the name
    sites: tuple = ()                # optional 'sites' column: only search these sites
    must_include: str = ""           # optional: words a listing title must contain
    exclude: str = ""                # optional: words that rule a listing out
    min_price: float | None = None   # optional: ignore cheaper listings (accessories)
    max_price: float | None = None
    # set on the per-site copies the crawler makes
    parent_id: str = ""
    listing_title: str = ""
    source_page: str = ""            # the Notion Watchlist row this item came from

    @property
    def is_search(self) -> bool:
        return not self.url and not self.parent_id

    @property
    def query(self) -> str:
        return self.search_query or self.name
