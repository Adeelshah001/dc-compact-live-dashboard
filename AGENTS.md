# AGENTS.md — DC Compact Comics Live Dashboard

Shared working notes for any agent (human or AI) working in this repo.
Read this file first. The decisions below are settled by the owner — do not
re-decide them, and do not change the site design to "improve" it without
being asked.

## What this is

A live dashboard cataloging every **DC Compact Comics** edition: 65 titles
(44 Released / 21 Upcoming as of Nov 2026), each with ISBN, release date,
page count, collected issues, official cover art, and buy links.

- Live site: https://dc-compact-live-dashboard.vercel.app
- Credits on the site: "Made by Rohail & Adeel"
- Stack: HTML5 + Tailwind CSS + vanilla JavaScript (zero build step),
  Vercel serverless function (`/api/comics`), GitHub Actions daily workflow
  (`.github/workflows/update-solicitations.yml`), Python 3 stdlib only for the
  catalog and link checks (`update_compact_comics.py`, `check_buy_links.py`,
  `discover_compact_comics.py`).

## How to work here

- The owner decides. Ask one question at a time and let him choose — never
  make a design or data decision for him. Bring open questions as yes/no
  choices, not as silent fixes.
- Never state something as verified unless you checked it against a primary
  source or the live site. Flag uncertainty plainly; do not guess.
- Do not build anything unprompted. Research and answers are fine; new
  features, redesigns, and catalog additions are not.
- Verify every push: compare the remote blob SHA against your local file
  (`git hash-object`) before calling it done.
- If a push fails, do not blind-retry. Send a zip for manual upload instead.
- The GitHub connector cannot write `.github/workflows/*`. Workflow edits
  are the owner's, in the GitHub web editor.
- The owner uploads image files to the repo himself; agents push code.
- Every progress update to the owner includes a progress bar with the
  percentage completed.

## Sourcing policy (settled)

- DC official sources first (dc.com, Penguin Random House product pages).
  Provider and retailer pages are secondary.
- Wikipedia, fandom wikis, and press are finding aids only — never authority.
- Where no source states a fact (contents, page count), leave it flagged as
  open. Do not guess.

## Link policy (settled)

- Store links point to the exact product page when one exists; otherwise a
  "<title> DC Compact Comics" search — by title name, never by ISBN.
- Amazon: amazon.ca for Canadian visitors, amazon.com otherwise.
- Four storefronts: Amazon, Penguin Random House, Indigo, Bookshop.org.
  Bookshop pages are unverifiable (Cloudflare 403s) — fail-open.
- Exact PRH pages exist for 62 titles; exact Bookshop pages for 61 of 62
  (DC: The New Frontier has no Compact edition in Bookshop's US catalogue —
  keep the search link).
- No prices anywhere on the site.

## Daily checker (06:00 UTC)

- Fail-open: a link is hidden only when confirmed unavailable. If a check
  cannot confirm, the link stays.
- Indigo is queried three times per title (ISBN, title, title +
  " DC Compact"); the suggest limit is 10. Title matching: >=60% token
  overlap OR full containment of the distinctive tokens (>=2 shared tokens),
  with volume numerals enforced.
- "Unavailable" requires BOTH title-shaped Indigo queries to return
  candidates with no match.
- Pinned exact product pages are kept; `hideLinks` pins are honored on
  every run.
- Discovery scans the PRH sitemap for new editions. New ones are added
  flagged for the owner's review (e.g. Batman '89 and The Nice House on the
  Lake, added Nov 2026). Upcoming titles get date/page-count updates only.
  Box sets are held for the owner's decision. The Superboy's Legion card is
  never touched by automation.

## Pinned catalog decisions (do not re-decide)

- Superboy's Legion is Spanish-language-only: no PRH page exists. Keep the
  card and its cover; hide PRH/Indigo/Bookshop links via the `hideLinks`
  pin; Amazon search link only.
- Green Lantern: Rebirth and JLA: Tower of Babel are Upcoming (June 2027,
  expanded contents).
- Hellblazer contents: #41–50.
- DCeased: 232pp, per the Dec 4, 2025 dc.com blog (overrides PRH's 216pp).
- Justice League: Heroes United stays at DC's 208pp (`DC_PINNED` guard).
- "Book One" vs arc titles: use PRH's arc titles (e.g. Y: The Unmanned,
  The Authority: Relentless, Superman/Batman).
- Open flags — no source states these, do not guess: Wonder Woman: Earth
  One contents vs 376pp; page-vs-contents tensions for Flash: Rebirth
  (352pp), Batgirl of Burnside (312pp), Injustice (416pp), Blood and Guts
  (280pp), The Authority (360pp).

## Design decisions (settled)

- Grid-only Card Matrix. The Chronological Stream view and header toggle
  were removed (Nov 2026) — do not re-add them.
- Sort default is Compact release date, newest first; the first two sort
  options are Compact date newest, then oldest. Inside-issue year and title
  sorts remain as options.
- Cover art: official PRH hotlinked covers as card backgrounds, blur(4px).
  The detail modal uses the same background, and its image loads eagerly.
- Era filters: Modern, 90s, Copper/Bronze.

## On hold (do not start)

- A major website update is on standby: the owner will describe it first.
  Do not build or speculatively design it.
- Sharing on r/DCcomics is on hold pending that update. Do not draft the
  post or message the mods.
- Affiliate links (Amazon Associates, Indigo via CJ, PRH via Impact) were
  researched; signups are the owner's and not started. Do not add affiliate
  parameters to any link.
