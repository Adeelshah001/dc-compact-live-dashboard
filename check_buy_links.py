#!/usr/bin/env python3
"""
DC Compact Comics - Buy-Link Verifier.

For every title in data/compact_comics.json, checks whether its buy links
actually resolve to the book on each storefront, and records the result:

    "links": {
      "amazon": {"url": ..., "status": "available|unavailable|unknown", "reason": ...},
      "prh":    {...},
      "indigo": {...}
    },
    "linksCheckedAt": "<ISO-8601 UTC timestamp>"

The frontend (index.html) renders only links whose status is "available" or
"unknown", and hides "unavailable" ones.

Design principles (verified by probing, 2026-11-14):

* Fail open. A link is hidden ONLY on a confirmed negative signal. Any
  inconclusive outcome (bot challenge, timeout, proxy error, unrecognized
  page) keeps the link visible with status "unknown", so a failed check can
  never nuke a good link.
* Penguin Random House: the /isbn/{isbn} URL format returns 404 for every
  title tested (even with a verified-correct ISBN), so it cannot be used as
  a negative signal on its own. PRH is checked via the Random House
  reststop title API plus the /isbn/ page; "unavailable" requires BOTH to
  404. The stored PRH URL is the working search URL unless a canonical
  book page is discovered.
* Indigo: indigo.ca is a Shopify storefront. Its public suggest API
  (/search/suggest.json) is queried three times per title: by ISBN, by
  title, and by title + "DC Compact". The ISBN query often returns zero
  candidates (the index does not reliably match on ISBN), and the plain
  title query can drown in fuzzy junk (e.g. "V for Vendetta" returns only
  "Dr. Nikola's Vendetta" books); the "DC Compact" query reliably
  surfaces the compact edition when Indigo carries it. Candidates
  fuzzy-match, so they are verified by normalized title match (overlap
  or subset containment, with volume numerals enforced), preferring
  candidates labelled "Compact". A title match in any query =
  "available". "Unavailable" requires BOTH title-shaped queries to
  return candidates with no match. Empty/failed queries = "unknown".
* Amazon: amazon.com serves an interstitial bot challenge to datacenter
  IPs, so plain requests cannot verify availability. Amazon checks record
  "unknown" when the challenge page is detected. The supported upgrade
  path is the Amazon Product Advertising API once an Associates account
  is approved (requires 3 qualifying sales).

Stdlib only (no pip dependencies), so it runs unchanged in GitHub Actions.

Usage:
    python check_buy_links.py [--limit N] [--delay SECONDS]
"""

import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "compact_comics.json")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------

def fetch(url, timeout=15, accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"):
    """GET a URL, following redirects. Returns (status, final_url, body_text).

    HTTP error statuses (404, 403, ...) are returned, not raised, so callers
    can use the status as a signal. Network-level failures raise URLError.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, resp.geturl(), raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, e.geturl(), body


def fetch_with_retry(url, timeout=15, accept=None, retries=1, backoff=2.0):
    """fetch() with one retry on transient network errors."""
    kwargs = {"timeout": timeout}
    if accept is not None:
        kwargs["accept"] = accept
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return fetch(url, **kwargs)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_exc = e
            if attempt < retries:
                time.sleep(backoff)
    raise last_exc


# ---------------------------------------------------------------------------
# Title matching (Indigo)
# ---------------------------------------------------------------------------

STOPWORDS = {
    "the", "and", "of", "a", "an", "vol", "volume", "book", "part",
    "dc", "compact", "comics", "edition", "with", "in", "to", "for",
}
ROMAN_NUMERALS = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"}


def title_tokens(title):
    """Distinctive lowercase tokens: long words plus volume numerals."""
    cleaned = re.sub(r"[^a-z0-9 ]", " ", (title or "").lower())
    out = []
    for tok in cleaned.split():
        if tok in STOPWORDS:
            continue
        if len(tok) > 2 or tok.isdigit() or tok in ROMAN_NUMERALS:
            out.append(tok)
    return out


def titles_match(catalog_title, product_title):
    """True when a storefront product title is plausibly the same book.

    Every volume numeral in the catalog title (e.g. "2" in "Vol. 2", "I"
    in "Act I") must appear in the product title, so Vol. 1 never matches
    Vol. 2. Beyond that, a match needs either >=60% of the catalog
    title's distinctive tokens present, or one title's distinctive
    tokens fully contained in the other's (a store title is often a
    shortened form, e.g. "Batman: Arkham Asylum" for the catalog's
    "... A Serious House on Serious Earth"). Containment requires at
    least 2 shared tokens, so a single shared word can never match.
    """
    ctoks = set(title_tokens(catalog_title))
    ptoks = set(title_tokens(product_title))
    if not ctoks or not ptoks:
        return False
    numerals = [t for t in ctoks if t.isdigit() or t in ROMAN_NUMERALS]
    if numerals and any(n not in ptoks for n in numerals):
        return False
    shared = ctoks & ptoks
    if len(shared) / len(ctoks) >= 0.6:
        return True
    return len(shared) >= 2 and (ctoks <= ptoks or ptoks <= ctoks)


# ---------------------------------------------------------------------------
# Storefront checks. Each returns (status, url, reason).
# ---------------------------------------------------------------------------

# Circuit breaker: if reststop is unreachable from this runner, it will be
# unreachable for every title. After two consecutive network failures we
# stop trying it for the rest of the run instead of paying a timeout per
# title. A clean HTTP response (including 404) resets the counter.
_reststop_dead = False
_reststop_failures = 0


def check_prh(isbn, title):
    global _reststop_dead, _reststop_failures
    search_url = f"https://www.penguinrandomhouse.com/search/{isbn}"
    reststop_404 = False

    # 1. Random House reststop title API (authoritative when reachable).
    if not _reststop_dead:
        try:
            status, _, body = fetch(
                f"https://reststop.randomhouse.com/resources/titles/{isbn}",
                accept="application/json",
                timeout=6,
            )
            _reststop_failures = 0
            if status == 200 and body:
                head = title_tokens(title)[:1]
                if isbn in body or (head and head[0] in body.lower()):
                    m = re.search(
                        r"https://www\.penguinrandomhouse\.com/books/\d+/[^\"'\s<]+",
                        body,
                    )
                    return "available", m.group(0) if m else search_url, "reststop"
            elif status == 404:
                reststop_404 = True
        except Exception:
            # Unreachable from this runner: inconclusive, not a negative.
            _reststop_failures += 1
            if _reststop_failures >= 2:
                _reststop_dead = True

    # 2. The /isbn/ page. (404s for everything as of 2026-11-14, but cheap
    #    to keep checking in case PRH fixes the route.)
    isbn_404 = False
    try:
        status, final_url, body = fetch_with_retry(
            f"https://www.penguinrandomhouse.com/isbn/{isbn}", timeout=15
        )
        title_tag = re.search(r"<title>(.*?)</title>", body or "", re.S)
        page_title = re.sub(r"\s+", " ", title_tag.group(1)).strip() if title_tag else ""
        if status == 200 and "Page Not Found" not in page_title:
            return "available", final_url, "isbn-page"
        isbn_404 = status == 404
    except Exception:
        pass

    # Only two independent negative signals may hide the link.
    if reststop_404 and isbn_404:
        return "unavailable", search_url, "reststop-404+isbn-404"
    return "unknown", search_url, "unverifiable"


def _indigo_suggest(query):
    """Shopify suggest API. Returns a product list, or None on any failure."""
    url = (
        "https://www.indigo.ca/search/suggest.json?q="
        + urllib.parse.quote(query)
        + "&resources[type]=product&resources[limit]=10"
    )
    try:
        status, _, body = fetch_with_retry(url, accept="application/json", timeout=15)
    except Exception:
        return None
    if status != 200:
        return None
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    return data.get("resources", {}).get("results", {}).get("products", [])


def _best_indigo_match(title, products):
    """First title-matching product, preferring ones labelled Compact."""
    matches = [p for p in products if titles_match(title, p.get("title", ""))]
    if not matches:
        return None
    for product in matches:
        if "compact" in (product.get("title") or "").lower():
            return product
    return matches[0]


def check_indigo(isbn, title):
    search_url = f"https://www.indigo.ca/en-ca/search?q={isbn}"
    isbn_products = _indigo_suggest(isbn)
    time.sleep(0.4)
    title_products = _indigo_suggest(title)
    time.sleep(0.4)
    compact_products = _indigo_suggest(title + " DC Compact")

    all_products = []
    for products in (isbn_products, title_products, compact_products):
        if products:
            all_products.extend(products)
    match = _best_indigo_match(title, all_products)
    if match:
        path = (match.get("url") or "").split("?")[0]
        url = f"https://www.indigo.ca{path}" if path.startswith("/") else search_url
        return "available", url, "title-match"

    # Both title-shaped queries returned candidates but none matched: the
    # book genuinely is not surfacing. An empty/failed query on either
    # side is inconclusive, not a negative.
    if title_products and compact_products:
        return "unavailable", search_url, "no-title-match"
    return "unknown", search_url, "no-candidates"


AMAZON_CHALLENGE_MARKERS = (
    "triggerInterstitialChallenge",
    "Enter the characters",
    "automated access",
    "api-services-support@amazon",
)


def check_amazon(isbn):
    url = f"https://www.amazon.com/s?k={isbn}"
    try:
        status, _, body = fetch_with_retry(url, timeout=15)
    except Exception:
        return "unknown", url, "request-error"
    lowered = (body or "").lower()
    if any(m.lower() in lowered for m in AMAZON_CHALLENGE_MARKERS):
        return "unknown", url, "bot-challenge"
    if "no results for" in lowered or "no results found" in lowered:
        return "unavailable", url, "no-results"
    if "s-search-result" in body:
        return "available", url, "results-found"
    return "unknown", url, "unrecognized-page"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_data():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data):
    # Preserve file order (update_compact_comics.py owns sorting).
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description="Verify buy links for DC Compact Comics.")
    parser.add_argument("--limit", type=int, default=0, help="Check only the first N titles (0 = all).")
    parser.add_argument("--delay", type=float, default=0.7, help="Seconds to wait between requests.")
    args = parser.parse_args()

    data = load_data()
    items = data[: args.limit] if args.limit else data
    checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    counts = {"available": 0, "unavailable": 0, "unknown": 0}
    hidden = []

    for idx, item in enumerate(items, 1):
        isbn = (item.get("isbn") or "").strip()
        title = item.get("title", "")
        if isbn:
            prh_status, prh_url, prh_reason = check_prh(isbn, title)
            time.sleep(args.delay)
            indigo_status, indigo_url, indigo_reason = check_indigo(isbn, title)
            time.sleep(args.delay)
            amazon_status, amazon_url, amazon_reason = check_amazon(isbn)
            time.sleep(args.delay)
        else:
            prh_status, prh_url, prh_reason = "unknown", "", "no-isbn"
            indigo_status, indigo_url, indigo_reason = "unknown", "", "no-isbn"
            amazon_status, amazon_url, amazon_reason = "unknown", "", "no-isbn"

        item["links"] = {
            "amazon": {"url": amazon_url, "status": amazon_status, "reason": amazon_reason},
            "prh": {"url": prh_url, "status": prh_status, "reason": prh_reason},
            "indigo": {"url": indigo_url, "status": indigo_status, "reason": indigo_reason},
        }
        item["linksCheckedAt"] = checked_at

        for st in (amazon_status, prh_status, indigo_status):
            counts[st] += 1
        for store, st in (("amazon", amazon_status), ("prh", prh_status), ("indigo", indigo_status)):
            if st == "unavailable":
                hidden.append(f"{title} [{store}]")
        print(
            f"[{idx}/{len(items)}] {title[:52]:<52} "
            f"amazon={amazon_status} prh={prh_status} indigo={indigo_status}",
            flush=True,
        )

    save_data(data)
    total = sum(counts.values())
    print(f"\nChecked {len(items)} titles ({total} store links) at {checked_at}")
    print(f"  available: {counts['available']}  unavailable (hidden): {counts['unavailable']}  unknown (kept): {counts['unknown']}")
    if hidden:
        print("\nLinks that will be HIDDEN (confirmed unavailable):")
        for h in hidden:
            print(f"  - {h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
