# Foodpanda Scraper + Local Browser

Scrape **Top rated** restaurant listings and full menus from [Foodpanda Pakistan](https://www.foodpanda.pk) into SQLite, then browse them in a simple Flask UI.

Uses Foodpanda's internal JSON APIs when available (fast path), with Playwright DOM scraping as a fallback. See [NOTES.md](NOTES.md) for discovered endpoints (including `sort=rating_desc` for Top rated).

## Requirements

- Python 3.11+
- Chromium for Playwright (only needed if the API path fails)

## Setup

```bash
cd foodpanda-scraper
pip install -r requirements.txt
playwright install chromium
```

## Scrape (Top rated, default)

Use `--fresh` only when you intentionally want to **wipe** the database first.
Without it, new restaurants are added and existing URLs are skipped (kept).

```bash
python main.py --top-rated=True --count 15
```

To replace everything:

```bash
python main.py --fresh --top-rated=True --count 15
```

Debug selectors visually:

```bash
python main.py --headless=False --count 3 --fresh
```

### CLI options

| Flag | Default | Description |
| --- | --- | --- |
| `--city` | `Karachi` | City label (logging) |
| `--lat` | `24.8607` | Search latitude |
| `--lng` | `67.0011` | Search longitude |
| `--count` | `15` | Number of restaurants |
| `--db-path` | `foodpanda.db` | SQLite output path |
| `--headless` | `True` | Playwright headless mode |
| `--top-rated` | `True` | Use `sort=rating_desc` (Top rated carousel) |
| `--fresh` | off | Clear DB before scraping |

Progress is logged to the console and to `scrape.log`. Each restaurant is committed immediately after scraping.

## Browse in the browser

```bash
python app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000):

- `/` — restaurant grid (name, rating, cuisine, image, item count)
- `/restaurant/<id>` — full menu by category
- `/api/restaurants` — JSON list
- `/api/restaurant/<id>` — JSON detail + menu

## Well-known Karachi expansion

Appends high-`review_count` restaurants from 11 area centers. Does **not**
change existing IDs. Skip if `pk.fd-api.com` is returning PerimeterX 403
(wait and re-run; the script stops after five 403s).

```bash
python discover_wellknown.py --dry-run
python discover_wellknown.py
```

Cutoff is 3,208 reviews (the original dataset median). See
[KARACHI_WELLKNOWN_PLAN.md](../plans/KARACHI_WELLKNOWN_PLAN.md).

## Query the database

```bash
python query_examples.py --db-path foodpanda.db
```

## Project layout

```
foodpanda-scraper/
├── main.py                 # scraper CLI
├── app.py                  # Flask browse UI
├── config.py
├── query_examples.py
├── NOTES.md
├── RESTAURANTS.md
├── templates/              # Jinja2 pages
├── static/style.css
├── scraper/
│   ├── api_client.py
│   ├── listing.py          # get_restaurants(..., top_rated=True)
│   └── menu.py
└── db/
    ├── schema.sql
    └── database.py
```

If DOM fallback is needed, update the selector constants at the top of `scraper/listing.py` and `scraper/menu.py` after inspecting the live page in DevTools.
