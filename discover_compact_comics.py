#!/usr/bin/env python3
"""
DC Compact Comics - New-Release Discovery.

Enumerates every Penguin Random House book page in the DC Compact Comics
line from PRH's own works sitemaps, diffs by PRH book id against the
catalog, and appends genuinely new editions (flagged for human review).
Also refreshes date/status/page-count on Upcoming entries from their live
PRH pages, so slips and releases are caught the day PRH reflects them.

Runs in the daily GitHub Action BEFORE check_buy_links.py, so new entries
get link verification the same day. Stdlib only; no pip installs.

Fail-open throughout: any sitemap fetch failure, parse failure, or
validation failure aborts with the catalog untouched (exit 0, so the link
checker still runs). A run that finds more than 5 new ids holds them for
human review instead of writing them.

What this script NEVER does:
  * Delete or rename any entry (cancellations and phantoms are human calls).
  * Touch curated fields on existing entries: description, genre, era,
    originalIssueYear, originalIssueSpan, collectedIssues, creators.
  * Touch the Superboy's Legion entry in any way.
  * Write ISBNs for existing entries (the catalog-wide ISBN-correction
    pass is a separate, user-approved decision). ISBN diffs are logged
    as report-only lines.
  * Touch links/linksCheckedAt, except seeding a discovered canonical PRH
    URL for an entry that has none (the checker verifies it minutes later
    in the same Action run).
  * Add box sets automatically: new box-set ids are reported and held.

New entries cannot be complete from PRH alone: PRH pages carry no
original-issue year, genre, or era. Such entries are written with
originalIssueYear 0, era/genre "Unknown" (Adventures get genre
"All-Ages"), a description drafted from the PRH "Collects" sentence, and
a "discovery" block with needsReview=true. The frontend renders a 0 year
as an em dash. A human clears needsReview after filling in the gaps.

Status transitions are strict: an Upcoming entry flips only when its live
PRH page was actually read in this run (availability + date from the
page). A failed fetch never flips anything. Released entries are never
re-checked by this script; a full metadata audit is a separate job.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data", "compact_comics.json")
REVIEW_FILE = os.path.join(BASE_DIR, "data", "_discovery_review.json")

PRH = "https://www.penguinrandomhouse.com"
SITEMAP_INDEX = PRH + "/sitemap.xml"
BOOK_RE = re.compile(r"https://www\.penguinrandomhouse\.com/books/(\d+)/([a-z0-9\-\.]+)/")
COMPACT_MARKER = "dc-compact-comics"
USER_AGENT = {
    "User-Agent": "dc-compact-dashboard-discovery/1.0 (+https://github.com/Adeelshah001/dc-compact-live-dashboard)"
}
ANOMALY_LIMIT = 5
NEVER_TOUCH_IDS = {"superboys-legion"}

ERA_BUCKETS = [
    (1970, 1983, "Bronze Age (1970–1983)"),
    (1984, 1989, "Copper Age (1984–1989)"),
    (1990, 1999, "Dark Age / 90s (1990–1999)"),
    (2000, 2010, "Modern (2000–2010)"),
    (2011, 2017, "Modern (2011–2017)"),
    (2018, 9999, "Modern (2018–Present)"),
]

REQUIRED_FIELDS = [
    "id", "title", "collectedIssues", "compactReleaseDate", "status",
    "creators", "genre", "pageCount", "isbn", "description", "era",
]


class AbortDiscovery(Exception):
    """Fail-open abort: catalog stays untouched."""


class RateLimited(AbortDiscovery):
    pass


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(f"HTTP 429 from {url}")
        return e.code, ""
    except Exception as e:
        raise AbortDiscovery(f"fetch failed for {url}: {e}")


# ---------------------------------------------------------------------------
# Sitemap enumeration
# ---------------------------------------------------------------------------

def sitemap_years():
    y = date.today().year
    return [y - 1, y, y + 1]


def works_sitemaps_from_index():
    """Best-effort: read the sitemap index. Returns a list of works-sitemap
    URLs, or None when the index cannot be used (caller falls back)."""
    try:
        status, body = fetch(SITEMAP_INDEX)
    except AbortDiscovery:
        return None
    if status != 200:
        return None
    urls = re.findall(
        r"<loc>\s*(https://www\.penguinrandomhouse\.com/sitemap-works-\d+\.xml)\s*</loc>",
        body,
    )
    return sorted(set(urls)) or None


def enumerate_sitemap_ids(delay):
    """Exhaustive enumeration: every compact slug in the works sitemaps.
    Returns {prh_id: sitemap_url}. Any failure aborts (fail open)."""
    sitemaps = works_sitemaps_from_index()
    if not sitemaps:
        sitemaps = [f"{PRH}/sitemap-works-{y}.xml" for y in sitemap_years()]
    found = {}
    for sitemap_url in sitemaps:
        status, body = fetch(sitemap_url)
        if status != 200:
            raise AbortDiscovery(f"sitemap {sitemap_url} -> HTTP {status}")
        for m in BOOK_RE.finditer(body):
            if COMPACT_MARKER in m.group(2):
                found[m.group(1)] = m.group(0)
        time.sleep(max(delay, 0.5))
    if not found:
        raise AbortDiscovery("zero dc-compact-comics matches in sitemaps; format changed?")
    return found


# ---------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------

def load_catalog():
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def known_prh_ids(catalog):
    known = {}
    for item in catalog:
        url = (item.get("links") or {}).get("prh", {}).get("url") or ""
        m = BOOK_RE.search(url)
        if m:
            known[m.group(1)] = item["id"]
    return known


def normalize_title(title):
    t = title.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def slugify(title):
    # Same rules as update_compact_comics.py add_title().
    return (
        title.lower()
        .replace(":", "")
        .replace("'", "")
        .replace("&", "and")
        .replace(" ", "-")
    )


def title_map_without_prh(catalog, known):
    """Normalized title -> entry, for entries with no canonical PRH URL.
    Used to attach a newly published PRH page to an existing entry instead
    of appending a duplicate."""
    known_ids = set(known.values())
    out = {}
    for item in catalog:
        if item["id"] in NEVER_TOUCH_IDS or item["id"] in known_ids:
            continue
        out[normalize_title(item["title"])] = item
    return out


# ---------------------------------------------------------------------------
# Book-page parsing (patterns verified live against PRH pages, 2026-11-14)
# ---------------------------------------------------------------------------

def parse_book_page(body, url):
    """Extract structured metadata from a PRH book page. Returns a dict or
    None when any critical field cannot be parsed (caller skips the id)."""
    text = html.unescape(body)

    title_m = re.search(r"<title>(.*?)</title>", body, re.S)
    if not title_m:
        return None
    raw_title = re.sub(r"\s+", " ", html.unescape(title_m.group(1))).strip()
    head = raw_title.split("|")[0].strip()
    if COMPACT_MARKER not in head and "DC Compact Comics" not in head:
        return None
    if head.startswith("DC Compact Comics"):
        # The line name IS the title prefix, e.g. "DC Compact Comics Box Set".
        title = head.split(" by ")[0].strip()
    else:
        title = head.split("DC Compact Comics")[0].rstrip(" :").strip()
    if not title:
        return None

    isbn_m = re.search(r":\s*(97[89]\d{10})\b", head)
    if not isbn_m:
        isbn_m = re.search(r'"isbn"\s*:\s*"(97[89]\d{10})"', text)
    if not isbn_m:
        return None
    isbn = isbn_m.group(1)

    date_m = re.search(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})"', text)
    if not date_m:
        return None
    pub_date = date_m.group(1)

    pages_m = re.search(r'"numberOfPages"\s*:\s*"?(\d+)"?', text)
    if not pages_m:
        pages_m = re.search(r"(\d+)\s+Pages", text)
    if not pages_m:
        return None
    pages = int(pages_m.group(1))

    avail_m = re.search(
        r'<meta[^>]+property="og:availability"[^>]+content="([^"]+)"', body
    )
    availability = avail_m.group(1).lower() if avail_m else ""
    if not availability:
        avail_m = re.search(r'"availability"\s*:\s*"([^"]+)"', text)
        availability = avail_m.group(1).lower() if avail_m else ""

    author_m = re.search(r"\bby\s+(.+?)\s*:\s*97[89]\d{10}\b", head)
    creators = []
    if author_m:
        creators.append(author_m.group(1).strip())
    ill_m = re.search(r"Illustrated by\s+([^\"<\n]{3,150})", text)
    if ill_m:
        for part in re.split(r",\s*|\s+and\s+", ill_m.group(1)):
            name = part.strip().rstrip(".")
            if name and "<" not in name and "http" not in name:
                creators.append(name)
    if not creators:
        return None

    # Best-effort: not every PRH page carries a "Collects" sentence (e.g.
    # Fables has none in its raw page). Missing issues never fail the
    # parse; the entry is flagged needsReview and a human fills them in.
    collects = None
    idx = text.find("Collects")
    if idx != -1:
        chunk = text[idx:idx + 700]
        # The sentence lives inside a JSON blob: cut at the blob's closing
        # quote (","), falling back to the first quote.
        cut = chunk.find('","')
        chunk = chunk[:cut] if cut != -1 else chunk.split('"')[0]
        chunk = html.unescape(chunk)  # second pass: blob is double-escaped
        chunk = re.sub(r"<[^>]+>", "", chunk)
        chunk = chunk.replace("\\/", "/").replace('\\"', '"')
        chunk = re.sub(r"\s+", " ", chunk).strip()
        if chunk.lower().startswith("collects ") and len(chunk) > 20:
            collects = chunk.rstrip(".").strip()
    if collects:
        issues = collects[len("Collects "):].strip()
        issues = re.sub(r"(\d)-(\d)", r"\1–\2", issues)
        collects_sentence = collects + "."
    else:
        issues = ""
        collects_sentence = None

    return {
        "title": title,
        "isbn": isbn,
        "date": pub_date,
        "pages": pages,
        "availability": availability,
        "creators": ", ".join(creators),
        "collects_sentence": collects_sentence,
        "collectedIssues": issues,
        "is_box_set": "box-set" in url or "Boxed Set" in text,
        "is_adventures": "adventures" in url,
    }


def status_from(availability, pub_date, today):
    if availability == "preorder":
        return "Upcoming"
    if availability == "instock":
        return "Released" if pub_date <= today else "Upcoming"
    return "Upcoming" if pub_date > today else "Released"


def enrich(prh_id, sitemap_url, delay):
    """Fetch one new book page and build a catalog entry. Returns
    (entry, meta), or (None, None) when the page cannot be parsed (the id
    retries tomorrow). A 429 propagates so the whole run backs off."""
    time.sleep(delay)
    try:
        status, body = fetch(sitemap_url)
    except RateLimited:
        raise
    except AbortDiscovery as e:
        print(f"  enrich {prh_id}: {e}; skipping this run")
        return None, None
    if status != 200:
        print(f"  enrich {prh_id}: HTTP {status}; skipping this run")
        return None, None
    meta = parse_book_page(body, sitemap_url)
    if not meta:
        print(f"  enrich {prh_id}: unparseable page; skipping this run")
        return None, None

    slug = BOOK_RE.search(sitemap_url).group(2)
    canonical = f"{PRH}/books/{prh_id}/{slug}/{meta['isbn']}/"
    prh_url = canonical
    time.sleep(delay)
    try:
        v_status, v_body = fetch(canonical)
        if v_status != 200 or meta["isbn"] not in v_body:
            prh_url = sitemap_url  # checker re-verifies the base URL
            print(f"  enrich {prh_id}: canonical ISBN URL unverified; keeping base URL")
    except RateLimited:
        raise
    except AbortDiscovery:
        prh_url = sitemap_url
        print(f"  enrich {prh_id}: canonical check failed; keeping base URL")

    today = date.today().isoformat()
    entry = {
        "id": slugify(meta["title"]),
        "title": meta["title"],
        "originalIssueYear": 0,
        "originalIssueSpan": "",
        "collectedIssues": meta["collectedIssues"],
        "compactReleaseDate": meta["date"],
        "status": status_from(meta["availability"], meta["date"], today),
        "creators": meta["creators"],
        "genre": "All-Ages" if meta["is_adventures"] else "Unknown",
        "pageCount": meta["pages"],
        "isbn": meta["isbn"],
        "description": meta["collects_sentence"] or (
            "DC Compact Comics edition — full details pending review."
        ),
        "era": "Unknown",
        "discovery": {
            "source": "prh-sitemap",
            "firstSeen": today,
            "needsReview": True,
        },
        "links": {
            "prh": {"url": prh_url, "status": "unknown", "reason": "discovered"}
        },
    }
    return entry, meta


# ---------------------------------------------------------------------------
# Transitions for Upcoming entries
# ---------------------------------------------------------------------------

def refresh_upcoming(catalog, known, delay, dry_run):
    """Re-read live PRH pages for Upcoming entries with canonical URLs.
    Updates date/status/page-count from PRH-verified values; reports ISBN
    diffs without writing them. Returns a list of change descriptions."""
    by_prh = {v: k for k, v in known.items()}
    changes = []
    today = date.today().isoformat()
    for item in catalog:
        if item.get("status") != "Upcoming" or item["id"] in NEVER_TOUCH_IDS:
            continue
        prh_id = by_prh.get(item["id"])
        if not prh_id:
            continue
        url = (item.get("links") or {}).get("prh", {}).get("url") or ""
        time.sleep(delay)
        try:
            status, body = fetch(url)
        except RateLimited:
            raise
        except AbortDiscovery as e:
            print(f"  refresh {item['id']}: {e}; skipping")
            continue
        if status != 200:
            print(f"  refresh {item['id']}: HTTP {status}; skipping")
            continue
        meta = parse_book_page(body, url)
        if not meta:
            print(f"  refresh {item['id']}: unparseable page; skipping")
            continue
        if meta["isbn"] != item.get("isbn"):
            changes.append(
                f"ISBN-DIFF {item['id']}: catalog {item.get('isbn')} vs PRH "
                f"{meta['isbn']} (report-only; correction pass pending)"
            )
        new_status = status_from(meta["availability"], meta["date"], today)
        updates = {}
        if meta["date"] != item.get("compactReleaseDate"):
            updates["compactReleaseDate"] = meta["date"]
        if meta["pages"] != item.get("pageCount"):
            updates["pageCount"] = meta["pages"]
        if new_status != item.get("status"):
            updates["status"] = new_status
        if updates:
            desc = ", ".join(f"{k}: {item.get(k)} -> {v}" for k, v in updates.items())
            changes.append(f"UPDATE {item['id']}: {desc}")
            if not dry_run:
                item.update(updates)
    return changes


# ---------------------------------------------------------------------------
# Validation + write
# ---------------------------------------------------------------------------

def validate(old_ids, catalog, added_ids):
    problems = []
    try:
        json.dumps(catalog)
    except (TypeError, ValueError) as e:
        problems.append(f"catalog not JSON-serializable: {e}")
    ids = [x.get("id") for x in catalog]
    if len(ids) != len(set(ids)):
        problems.append("duplicate ids present")
    missing = old_ids - set(ids)
    if missing:
        problems.append(f"entries removed: {sorted(missing)}")
    for item in catalog:
        for field in REQUIRED_FIELDS:
            if field == "collectedIssues" and item.get("discovery", {}).get("needsReview"):
                continue  # PRH pages do not always carry a Collects sentence
            if item.get(field) in (None, ""):
                problems.append(f"{item.get('id')}: empty required field {field}")
        if not isinstance(item.get("originalIssueYear"), int):
            problems.append(f"{item.get('id')}: originalIssueYear not an int")
        if not isinstance(item.get("originalIssueSpan"), str):
            problems.append(f"{item.get('id')}: originalIssueSpan not a string")
    for item in catalog:
        if item["id"] in added_ids and not item.get("discovery", {}).get("needsReview"):
            problems.append(f"{item['id']}: new entry missing needsReview flag")
    return problems


def sort_catalog(catalog):
    catalog.sort(
        key=lambda x: (int(x.get("originalIssueYear", 0)),
                       str(x.get("compactReleaseDate", ""))),
        reverse=True,
    )


def atomic_write(catalog):
    tmp = DATA_FILE + ".tmp-discovery"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Discover new DC Compact Comics editions from PRH sitemaps.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen; write nothing.")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between PRH page fetches.")
    args = parser.parse_args()

    catalog = load_catalog()
    old_ids = {x["id"] for x in catalog}
    old_count = len(catalog)

    try:
        sitemap_ids = enumerate_sitemap_ids(args.delay)
    except AbortDiscovery as e:
        print(f"discovery aborted (catalog untouched): {e}")
        return 0
    print(f"sitemap enumeration: {len(sitemap_ids)} compact pages")

    known = known_prh_ids(catalog)
    new_ids = sorted(set(sitemap_ids) - set(known))
    print(f"known PRH ids in catalog: {len(known)}; new ids: {len(new_ids)}")

    if len(new_ids) > ANOMALY_LIMIT:
        report = {
            "date": date.today().isoformat(),
            "anomaly": "more than 5 new ids; held for human review",
            "new_ids": {i: sitemap_ids[i] for i in new_ids},
        }
        print(f"ANOMALY GUARD: {len(new_ids)} new ids held for review, catalog untouched")
        if not args.dry_run:
            with open(REVIEW_FILE, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
        return 0

    title_map = title_map_without_prh(catalog, known)
    added, attached, held = [], [], []
    try:
        for prh_id in new_ids:
            sitemap_url = sitemap_ids[prh_id]
            if "box-set" in sitemap_url:
                held.append(prh_id)
                print(f"  held for box-set decision: {prh_id} {sitemap_url}")
                continue
            entry, meta = enrich(prh_id, sitemap_url, args.delay)
            if entry is None:
                continue
            if meta["is_box_set"]:
                held.append(prh_id)
                print(f"  held for box-set decision: {prh_id} {entry['title']}")
                continue
            # Attach first: an existing entry without a PRH page (e.g. an
            # announced title whose page just appeared) must match by
            # title even when its slug equals the enriched entry's id.
            existing = title_map.get(normalize_title(entry["title"]))
            if entry["id"] in old_ids and existing is None:
                print(f"  id collision for {entry['id']}; skipping (human review needed)")
                continue
            if existing is not None:
                # The book is already catalogued without a PRH page (e.g. an
                # announced title whose page just appeared): attach the URL
                # instead of appending a duplicate. ISBN diffs stay report-only.
                if entry["isbn"] != existing.get("isbn"):
                    print(f"  ISBN-DIFF {existing['id']}: catalog {existing.get('isbn')} "
                          f"vs PRH {entry['isbn']} (report-only; correction pass pending)")
                if not args.dry_run:
                    links = existing.setdefault("links", {})
                    links["prh"] = {"url": entry["links"]["prh"]["url"],
                                    "status": "unknown", "reason": "discovered"}
                attached.append(existing["id"])
                print(f"  attached PRH page to existing entry: {existing['id']}")
                continue
            if not args.dry_run:
                catalog.append(entry)
            added.append(entry["id"])
            print(f"  {'would add' if args.dry_run else 'added'}: {entry['title']} "
                  f"({entry['compactReleaseDate']}, {entry['status']})")

        changes = refresh_upcoming(catalog, known, args.delay, args.dry_run)
    except AbortDiscovery as e:
        print(f"discovery aborted mid-run (catalog untouched): {e}")
        return 0
    for line in changes:
        print(f"  {line}")

    if args.dry_run:
        print(f"dry run: +{len(added)} entries, {len(attached)} attached, "
              f"{len(held)} held, {len(changes)} updates reported; nothing written")
        return 0

    if added or attached or changes:
        problems = validate(old_ids, catalog, set(added))
        if problems:
            print("VALIDATION FAILED; catalog untouched:")
            for p in problems:
                print(f"  - {p}")
            return 0
        sort_catalog(catalog)
        atomic_write(catalog)
    print(f"discovery: +{len(added)} entries, {len(attached)} attached, "
          f"{len(held)} held, {len(changes)} updates; {len(catalog)} titles total "
          f"(was {old_count})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
