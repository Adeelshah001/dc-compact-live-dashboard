# DC Compact Comics — Live Dashboard

A clean, live dashboard cataloging all released and upcoming **DC Compact Comics**, sorted from newest to oldest based on the original publication year of their inside issues (1982–2021).

🔗 **Live Website:** [https://dc-compact-live-dashboard.vercel.app](https://dc-compact-live-dashboard.vercel.app)

---

## Features

- **Inside Issue Chronology:** Strictly sorted from newest (2021) to oldest (1982) by the original publication year of the inside material.
- **Where to Buy:** Purchase links for every book to **Amazon**, **Penguin Random House**, **Indigo (Chapters)**, and **Bookshop.org**, verified daily — a store's link is hidden when the book is confirmed unavailable there (see `check_buy_links.py`).
- **Cover Art Backgrounds:** Each card and the detail view show the book's official cover art, blurred behind the text.
- **Instant Search & Era Filters:** Real-time search across titles, creators, and collected issues, with era toggles (Modern, 90s, Copper/Bronze).
- **Daily Automated Updates:** Daily GitHub Actions workflow checks for new solicitations, verifies every buy link, and automatically triggers Vercel redeployments.

---

## Tech Stack

- **Frontend:** HTML5, Tailwind CSS, Vanilla JavaScript (zero build step)
- **API:** Vercel Serverless Function (`/api/comics`)
- **Automation:** GitHub Actions cron (`.github/workflows/update-solicitations.yml`)
- **Catalog & Link Checks:** Python 3, stdlib only (`update_compact_comics.py`, `check_buy_links.py`)

---

<p align="center">
  <sub>Made by Rohail &amp; Adeel</sub>
</p>
