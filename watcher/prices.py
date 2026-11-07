"""Parsing and formatting prices: Arabic digits, thousands separators, percentages."""
from __future__ import annotations

import re

_DIGIT_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_NUMBER = re.compile(r"\d[\d.,]*")
_CURRENCY_WORDS = re.compile(
    r"(egp|l\.e\.?|le|e£|£|usd|us\$|\$|sar|aed|eur|€|جنيه(?:ًا|ا)?|ج\.م\.?|ج\s?م|مصري)",
    re.IGNORECASE,
)
_TRUE = {"true", "yes", "y", "1", "on", "x", "✓", "✔", "نعم"}
_FALSE = {"false", "no", "n", "0", "off", "لا"}


def normalize_digits(text) -> str:
    """Arabic-Indic digits and separators to ASCII; drop invisible direction marks."""
    return (
        str(text)
        .translate(_DIGIT_MAP)
        .replace("\u066b", ".")  # Arabic decimal separator
        .replace("\u066c", ",")  # Arabic thousands separator
        .replace("\u060c", ",")  # Arabic comma
        .replace("\u00a0", " ")
        .replace("\u202f", " ")
        .replace("\u200e", "")
        .replace("\u200f", "")
        .replace("\u061c", "")
    )


def parse_price(value) -> float | None:
    """'EGP 5,999.00', '٥٬٩٩٩٫٠٠ جنيه', 5999, '5.999,00' -> 5999.0. None if there is no positive number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    match = _NUMBER.search(normalize_digits(value))
    if not match:
        return None
    token = match.group(0).rstrip(".,")
    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        head, _, tail = token.rpartition(",")
        if token.count(",") == 1 and len(tail) in (1, 2):
            token = f"{head}.{tail}"
        else:
            token = token.replace(",", "")
    elif token.count(".") > 1:
        token = token.replace(".", "")
    elif "." in token:
        head, _, tail = token.partition(".")
        if len(tail) == 3 and 1 <= len(head) <= 3 and head != "0":
            token = head + tail  # '5.999' is five thousand, not five
    try:
        number = float(token)
    except ValueError:
        return None
    return number if number > 0 else None


def parse_amount(value) -> float | None:
    """Strict number for user input such as target prices: '6500', '6,500', 'EGP 6,500'.

    Rejects free text like 'abc' or 'around 6k', so a typo never becomes a wrong target."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    text = normalize_digits(value).strip()
    if not text:
        return None
    rest = _CURRENCY_WORDS.sub(" ", text)
    if not re.fullmatch(r"\s*\d[\d.,\s]*", rest):
        return None
    return parse_price(rest.replace(" ", ""))


def parse_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def money(value) -> str:
    if value is None:
        return "—"
    if abs(value - round(value)) < 0.005:
        return f"{round(value):,}"
    return f"{value:,.2f}"


def pct_off(reference, price) -> float | None:
    """How many % `price` is below `reference` (negative when above)."""
    if not reference or price is None or reference <= 0:
        return None
    return (reference - price) / reference * 100


_CURRENCY_HINTS = (
    ("EGP", ("egp", "e£", "l.e", "جنيه", "ج.م")),
    ("AED", ("aed", "د.إ")),
    ("SAR", ("sar", "ر.س")),
    ("USD", ("us$", "usd", "$")),
    ("EUR", ("€", "eur")),
    ("GBP", ("£", "gbp")),
)


def detect_currency(text, default: str = "") -> str:
    """'EGP 1,394.95' -> 'EGP', '$25.99' -> 'USD'; `default` when the text names no currency."""
    lowered = normalize_digits(text or "").lower()
    for code, hints in _CURRENCY_HINTS:
        if any(h in lowered for h in hints):
            return code
    return default
