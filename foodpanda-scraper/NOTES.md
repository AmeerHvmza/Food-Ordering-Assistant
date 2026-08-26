# API discovery findings — Foodpanda Pakistan (foodpanda.pk)
# Discovered / verified: 2026-08-10

## Summary

Foodpanda Pakistan serves restaurant listings and menus through internal JSON
APIs. Direct `requests` calls work for both **without** a browser session when
the correct hosts and headers are used.

Playwright navigation to `www.foodpanda.pk` pages is currently blocked by
PerimeterX / reCAPTCHA (`Access to this page has been denied`). Prefer the API
route; keep Playwright DOM scraping as a fallback for local debugging when the
block is not active (e.g. `--headless=False` from a residential IP).

---

## 1. Restaurant listings (by lat/lng)

**Endpoint**

```
GET https://disco.deliveryhero.io/listing/api/v1/pandora/vendors
```

**Required / useful query params**

| Param | Example | Notes |
| --- | --- | --- |
| `latitude` | `24.8607` | Required |
| `longitude` | `67.0011` | Required |
| `country` | `pk` | Required |
| `vertical` | `restaurants` | Required for restaurant feed |
| `limit` | `48` | Page size (tested up to 1000 historically) |
| `offset` | `0` | Pagination |
| `language_id` | `1` | English |
| `include` | `characteristics` | Extra fields |
| `dynamic_pricing` | `0` | |
| `configuration` | `Variant3` | Listing experiment variant |
| `customer_type` | `regular` | |
| `sort` | `rating_desc` | **Top rated** — see section 5 |

**Headers**

```
User-Agent: Mozilla/5.0 ...
Accept: application/json
x-disco-client-id: web
```

**Response shape (usable fields)**

```
data.items[]:
  code, name, url_key, web_path / redirection_url,
  rating, review_number, address, address_line2,
  cuisines[].name, minimum_delivery_time, latitude, longitude,
  hero_listing_image / hero_image
```

Restaurant page URL pattern:

```
https://www.foodpanda.pk/restaurant/{code}/{url_key}
```

(Also seen as `https://foodpanda.pk/restaurant/...` without `www`.)

**Verified:** HTTP 200 with JSON via plain `requests` (no cookies required).

---

## 2. Individual restaurant menu

### Preferred (works with requests)

```
GET https://pk.fd-api.com/api/v5/vendors/{vendor_code}
    ?latitude={lat}&longitude={lng}&language_id=1&include=menus
```

**Headers (required)**

```
User-Agent: Mozilla/5.0 ...
Accept: application/json, text/plain, */*
X-FP-API-KEY: volo
Referer: https://www.foodpanda.pk/
Origin: https://www.foodpanda.pk
perseus-client-id: <any pseudo id, e.g. {ms}.{random}.web>
perseus-session-id: <any pseudo id, e.g. {ms}.{random}.sess>
x-pd-language-id: 1
```

Without `perseus-client-id` / `perseus-session-id`, the API returns:

```
HTTP 400 {"error":"perseus headers are absent"}
```

**Response shape**

```
data.menus[].menu_categories[].products[]:
  name, description, file_path (image URL, may contain %s width placeholder),
  display_price (often "" or a concatenated UI string — do not store raw),
  product_variations[].price              (numeric, what the customer pays),
  product_variations[].price_before_discount  (numeric strikethrough / original)

```

**Verified:** HTTP 200, full nested menus for vendor `w4ad` (Karachi).

### Alternate (often blocked)

```
GET https://www.foodpanda.pk/api/v5/vendors/{vendor_code}/menu
```

Returns PerimeterX captcha JSON (`appId`, `captcha.js`) for bare `requests`
from this environment. The scraper still tries it after fd-api.

---

## 3. Playwright discovery notes

Attempted `page.goto` on listing and restaurant URLs with Chromium headless:

- Title: `Access to this page has been denied`
- Network: reCAPTCHA assets only; no disco / fd-api XHRs fired
- Implication: DOM fallback may fail under bot protection; API path is primary

To re-run discovery locally when the site is accessible:

```python
page.on("response", lambda r: ...)
page.goto("https://www.foodpanda.pk/restaurants/new?lat=...&lng=...")
# Look for URLs containing: vendors, pandora, disco, /api/, menu
```

---

## 4. Scraper strategy

1. Listings → disco pandora vendors API (`sort=rating_desc` when `--top-rated`)
2. Menus → `pk.fd-api.com/api/v5/vendors/{code}?include=menus`
3. If either fails → Playwright DOM scrape using selector constants in
   `listing.py` / `menu.py` (swap after DevTools inspection)

---

## 5. "Top rated restaurants" homepage carousel

**Finding (2026-08-10):** There is no separate homepage-widget host. The carousel
uses the **same** disco listing endpoint with an explicit sort param:

```
GET https://disco.deliveryhero.io/listing/api/v1/pandora/vendors
    ?latitude=24.8607
    &longitude=67.0011
    &country=pk
    &vertical=restaurants
    &limit=15
    &offset=0
    &customer_type=regular
    &sort=rating_desc
    &language_id=1
    &include=characteristics
    &dynamic_pricing=0
    &configuration=Variant3
```

| Param tried | Result |
| --- | --- |
| (no sort) | Default relevance / distance-ish mix — **not** top rated |
| `sort=rating_desc` | Highest ratings first (5.0 → 4.9…) — **matches Top rated intent** |
| `sort=top_rated` | Did **not** reorder like a rating sort |
| `filter_type=top_rated` | Did **not** apply a dedicated top-rated filter |
| dedicated swimlane URL | `404` |

**Approach used in the scraper:** pass `sort=rating_desc` to disco, then also
sort locally by `(rating, review_number)` descending and take the top N. This
covers the homepage carousel case and remains correct if a region ignores `sort`.

CLI: `--top-rated=True` (default). Use `--fresh` when replacing an older
non-top-rated database so URLs are not skipped by `INSERT OR IGNORE`.

---

## 6. Review counts and rating trust

**Verified: 2026-08-13** (re-probe of the disco listing endpoint, HTTP 200, 48
items, no cookies/auth).

### Does a review-count field exist?

**Yes.** The field is **`review_number`**, an integer on each `data.items[]`
vendor. It is already extracted by `normalize_vendor` in
`scraper/api_client.py` — it was simply never persisted, because the
`restaurants` table has no column for it. This is a persistence gap, not an API
gap, so no fallback heuristic is needed.

A second field, **`review_with_comment_number`, is `0` for every vendor
observed.** Review *text* is therefore **not** available from this listing feed
and will need a different source (vendor detail page or a reviews endpoint).

### Distribution across the 29 scraped restaurants

Matched by vendor `code` parsed from `restaurants.url`:

- **26 of 29** appear in the current Karachi feed (`latitude=24.8607`,
  `longitude=67.0011`), across `sort=rating_desc` and default sort, 179 distinct
  vendors seen while paginating `limit=48` to `offset=480`.
- **3 of 29** are absent from the feed: `w4ad` (Nihari Inn), `wqku` (Mamu
  Biryani), `j9ub` (Buland PakCuisine). All three return HTTP 200 with
  `rating` + `review_number` from the per-vendor endpoint
  `pk.fd-api.com/api/v5/vendors/{code}`, and all report `is_active=True`. Use
  the per-vendor endpoint as the backfill fallback.

Spread is roughly three orders of magnitude — **18 to 39,949 reviews**:

| Percentile (26 matched) | review_number |
| --- | --- |
| p10 | 134 |
| p25 | 1,211 |
| **p50 (median)** | **4,367** |
| p75 | 8,714 |
| p90 | 12,559 |

Including all 29 rows (adding 1,515 / 18 / 60 from the fd-api fallback):

- **median = 3,208**
- **mean rating C ≈ 4.807**
- 6 of 29 restaurants have fewer than 500 reviews; 2 have fewer than 100.

Highest volume: Foods Inn 39,949 · McDonald's 19,935 · Delizia 14,579 ·
Kababjees 10,539 · Ginsoy 10,488.
Lowest volume: Mamu Biryani **18** (rating 4.1) · Buland **60** (4.6) ·
Al Maedat **63** (5.0) · Dehli Malik **94** (5.0) · Haji Mehfooz Nishtar
**116** (5.0) · KBC **152** (4.8).

### Note on the `sort=rating_desc` result

`rating DESC` is a misleading ordering, but **not** uniformly for the reason one
might assume. **Rehmat-e-Shereen (5.0) has 5,667 reviews** — more volume than
Agha Chinese, Hot N Spicy, or Red Apple — so its 5.0 is well-supported and it
ranks first even after weighting. The genuinely low-signal rows are the
sub-300-review entries listed above.

Also observed: **2 of 26 stored `rating` values already differ from live**, so
any backfill should refresh `rating` in the same pass as `review_count` to keep
the two values from the same observation.

### Weighted ranking (consumed by the agent, not by the scraper)

Raw `rating DESC` is not used for recommendations. IMDB-style shrinkage toward
the dataset mean:

```
weighted_rating = (v / (v + m)) * R + (m / (v + m)) * C
```

`R` = vendor rating, `v` = `review_count`, `m` = **dataset median review count,
computed at query time** (currently 3,208), `C` = dataset mean rating
(currently ≈4.807). `m` and `C` are recomputed per query rather than pinned, so
a daily re-scrape (Milestone 2) stays correct without a code change.

Caveat worth remembering: shrinkage is **symmetric**. It rescues
bad-with-few-reviews as much as it demotes good-with-few-reviews — Mamu
Biryani's 4.1-on-18-reviews rises to ≈4.803. Consumers should surface
`review_count` alongside the rating and hedge their language when `v < m/4`.

### Other listing fields worth capturing later

Present on every `data.items[]` vendor and currently unused:

`discounts`, `discounts_info` (Milestone 3 deals), `distance` (real "near me"
instead of address text matching), `minimum_order_amount` (249.0),
`minimum_delivery_fee` (99.0), `is_promoted`, `is_best_in_city`, `chain`,
`budget`, `loyalty_programs`, `vendor_points`, `score`, `tags`.

### Status

- [x] Field identified (`review_number`) and distribution measured.
- [x] `review_count INTEGER` column added to `restaurants` via `_ensure_column`
      in `db/database.py` (no drop/recreate; 1,957 menu items preserved).
- [x] All 29 rows backfilled, `SELECT COUNT(*) FROM restaurants WHERE
      review_count IS NULL` = **0**. Run:
      `python backfill_review_counts.py` from `foodpanda-scraper/`.

**Backfill result (2026-08-13):** 26 rows from disco listing, 3 from per-vendor
fd-api (`w4ad` Nihari Inn 1,515; `wqku` Mamu Biryani 18; `j9ub` Buland 60).
Live dataset stats after write: **m (median) = 3,208**, **C (mean rating) =
4.8069**. Future inserts persist `review_number` as `review_count`. Ranking
helper: `../db/ranking.py` (`python db/ranking.py` from the workspace root).

---

## 7. Well-known Karachi expansion (2026-08-18)

One-shot expander: `discover_wellknown.py`. Daily refresh is
`../scheduler/daily_scrape.py` and does **not** re-run area discovery.

**Areas used:** Saddar, Clifton, DHA Phase 5, PECHS/Tariq Road,
Gulshan-e-Iqbal, Nazimabad, North Nazimabad, Gulistan-e-Jauhar,
Bahadurabad, North Karachi, Shahrah-e-Faisal. Korangi / Malir / Landhi
dropped (empty or near-empty disco listings).

**Cutoff:** `review_number >= 3208` (original 29-row median / ranking `m`).
Not lowered to pad the count.

**Menu API:** `pk.fd-api.com` PerimeterX-blocks a desktop browser UA
(403 captcha JSON). The Foodpanda Android UA later blocked too during the
Gulshan/Jauhar scrape. `_fd_api_headers()` now uses `okhttp/4.12.0`.
Menus are also empty if the request lat/lng is outside the vendor's
delivery area — pass the area center that discovered the vendor, not a
single city-wide point. `www.foodpanda.pk` DOM remains unusable.

**First run:** original IDs **16–44** unchanged (29 rows, 1,957 items).
Inserted **45** Broadway Pizza - Gulshan Block 5 and **46** New Quetta
Abdul Malik Hotel, then fd-api flipped to 403 after repeated probes.
Script now stops after five consecutive 403s. Re-run
`python discover_wellknown.py` when the block lifts; expected remaining
inserts ~110–150 at the same cutoff.

**Timing (while the API was open):** discovery ~2 minutes; a full menu
pass at 1.5–3.0s delay would be ~10–20 minutes for ~120 restaurants.

---

## 8. Deal-price scraper bug (2026-08-18)

Root cause is the expansion-batch menu parser writing malformed `menu_items`
rows. Cart/add_to_cart was reading the DB correctly and was not changed.

### Step 1 — live fd-api structure (Pizza Yumm's Jauhar, vendor `u9lp`)

Endpoint: `GET https://pk.fd-api.com/api/v5/vendors/{code}?include=menus`
(disco listings do not return menu products).

Confirmed product `Chicken Fajita Pizza`:

| Field | Live value |
| --- | --- |
| `display_price` | `""` (empty string) |
| product-level `price` / `original_price` / `discounted_price` | absent / null |
| `product_variations[0].price` | `300` (numeric; payable / "from" amount) |
| `product_variations[0].price_before_discount` | `600` (strikethrough) |
| variation name | `"6 Inches"` (three size variations) |

Live feed had **one** product per name (no duplicate). The stored pair was:

- item 4998 `price` NULL
- item 5005 `price` `'from Rs. 300Rs. 600'`

**Why the concatenated string:** `normalize_menu` used
`variations[0].price`, then fell back to `str(display_price)`. At scrape
time the variation price was missing/null for many deal items, so it stored
the UI display string. That string is two price nodes concatenated with no
separator: `"from Rs. {discounted}"` + `"Rs. {original}"`.

**Why the empty duplicate:** a second product object in the same category
had empty `display_price` and no variation price, and the parser still
inserted it. Live API no longer returns that stub; the DOM fallback
(`inner_text` of the price node) would concatenate strikethrough + current
the same way.

Parser now reads `product_variations[].price` / `price_before_discount`
(cheapest current variation), never stores `display_price` raw, skips
unpriced stubs, and dedupes by name within a category. `price` is the
payable amount; `original_price` is optional strikethrough for Milestone 3.

### Step 3 — cleanup (no full re-scrape)

`python scripts/cleanup_deal_prices.py`

| | before | after |
| --- | --- | --- |
| `menu_items` | 4468 | 4241 |
| empty / NULL price | 210 | 0 |
| `%Rs.%Rs.%` | 1204 | 0 |
| contains `from` | 608 | 0 |
| broken-pattern total | 1799 | **0** |
| original 29 (ids 16–44) | 1957 | 1957 (unchanged) |
| ids 45+ | 2511 | 2284 |

Actions: deleted 210 empty-price duplicates (sibling already had a string);
re-parsed 2301 dirty expansion strings (first `Rs. N` = payable, second =
`original_price`); deleted 17 leftover same-name duplicates. No restaurants
were re-scraped.

After: Pizza Yumm's `Chicken Fajita Pizza` is `price=300`,
`original_price=600`. Broadway `Azaadi Deal Small Pizza` is `399` / `549`.

### Step 5 — `/chat` latency (measured, not guessed)

Instrumentation: `agent.timing` logs around every LLM round and tool round
plus `chat_turn_wall_ms`. System prompt size is logged per call.
`AIMessage.response_metadata` was inspected for Groq rate-limit headers
(none are forwarded by langchain-groq; the Groq SDK retry log is the
signal). No latency optimization was applied.

Probe: `python scripts/measure_chat_latency.py` — one turn,
"hi, I am in Saddar looking for pizza", model `groq:openai/gpt-oss-120b`.

| Step | Duration |
| --- | --- |
| Time to first LLM call | 37 ms |
| LLM round 1 (`remember_preferences`) | 1021 ms (Groq `total_time` 0.37 s + `queue_time` 0.24 s) |
| Gap, agent node → tools node | ~1.5 s (checkpointer / graph overhead) |
| Tool `remember_preferences` | 9.5 ms |
| LLM round 2 (`search_restaurants`) | 710 ms (`queue_time` 0.24 s) |
| Tool `search_restaurants` | 92 ms |
| LLM round 3 (final reply) | **25492 ms** — Groq SDK: `Retrying request to /openai/v1/chat/completions in 24.000000 seconds` |
| **Turn wall** | **28886 ms** |

Other facts from the same turn:

- System prompt actually sent: **13,473 characters** (~3,562 prompt tokens)
  on round 1, growing to 3,989 tokens by round 3. Rebuilt every round.
- **3 tool-call rounds** for a greeting that only needed to save location
  and ask party size (this probe's round 2 also searched restaurants).
- Groq `x-ratelimit-*` / `retry-after` headers: **not present** on the
  successful `AIMessage.response_metadata`. The retry is visible only as
  the SDK log line above. `service_tier=on_demand`. No 429 body captured.
- Tools are not the bottleneck (9–92 ms).

**Measured bottleneck:** Groq LLM rounds, amplified by (1) a **24 s SDK
retry** on the third call and (2) **multiple sequential LLM rounds per
user turn**. Secondary: ~13.5 k system prompt tokens every round, plus
~1.5 s graph/checkpointer gap after the first model call. Tool execution
is noise.

---

## 9. Chat latency cuts (2026-08-18)

Code changes shipped; live after-timing of both benchmark turns was blocked
by Groq **tokens-per-day** (see Fix 3). Prompt-size numbers below are
measured in-process. Round-count before is from the earlier probe plus a
fresh Turn-1 sample.

### Fix 1 — routing prompt on post-tool rounds

First LLM call of a turn still uses the full prompt (user-facing greeting
or first tool plan). After any tool result, the next call uses
`build_routing_prompt` (lock/search/cart/currency only; **no policies.md**).

| Prompt | Characters (Saddar + pizza state) |
| --- | --- |
| Full (policies + role + state) | **14,525** |
| Routing (intermediate) | **1,039** |
| Reduction on post-tool rounds | **~93%** (13.5k fewer chars / ~2.5k fewer prompt tokens each) |

### Fix 2 — fewer tool rounds

1. `add_to_cart` now returns full cart lines + subtotal/delivery/platform/total
   and tells the model not to call `view_cart` to confirm.
2. Prompt + tool docstring: do not `search_restaurants` when a restaurant is
   already locked unless the user asks to browse/switch.
3. **Parallel tool calls:** Groq's OpenAI-compatible API returns a
   `tool_calls[]` array; LangGraph `ToolNode` already runs that list in one
   tool round. `ChatGroq.bind_tools` does not disable this. Prompt now asks
   the model to emit independent tools together (e.g. remember + search).
   Live confirmation that gpt-oss-120b actually emits two calls in one
   response was not possible this session (TPD exhausted). Sequential
   add-then-view is still unnecessary because of (1).

### Benchmarks

**Before (same model `groq:openai/gpt-oss-120b`):**

Turn A — "I'm in Saddar looking for pizza" / earlier "hi, I am in Saddar looking for pizza":

| | Earlier NOTES probe | Fresh sample this session |
| --- | --- | --- |
| LLM rounds | 3 (remember → search → reply) | 1 completed (remember), then **429 TPD** on round 2 |
| Full prompt chars | 13,473 | 14,449 |
| Round 1 Groq | 1021 ms | 956 ms |
| Wall | 28,886 ms (incl. 24 s retry on round 3) | failed mid-turn |

Turn B — "Add that pizza and show the cart": not completed live (TPD). Previously
this pattern was 5–7 sequential Groq calls (lock/menu/add/view/reply).

**After:** live Turn A/B re-run hit 429 immediately (TPD 200,000 used). Cannot
honestly claim a wall-clock win until the quota resets. Expected effect when
it does:

- Post-tool rounds send ~1k-char prompts instead of ~14.5k
- Turn A should drop the reflexive search if location+craving only need
  remember + a question, or pack remember+search in one parallel round
- Turn B should drop the extra `view_cart` round after `add_to_cart`

Re-run: `python scripts/measure_chat_latency.py` after TPD resets.

### Fix 3 — the 24s retry is a Groq 429 TPD limit (capacity, not a code bug)

Caught on the fresh before-run, HTTP **429**:

```
Rate limit reached for model openai/gpt-oss-120b
service tier on_demand
tokens per day (TPD): Limit 200000, Used ~199973, Requested ~3812
Please try again in ~27 minutes
code: rate_limit_exceeded
```

That matches the earlier 24 s SDK sleep (`Retrying request ... in 24 seconds`):
the client was backing off a 429. Not a timeout and not a 5xx.

`agent.timing` now logs `groq_retry_cause` (status, retry-after, rate-limit
headers, body) before the SDK sleeps, and `llm_call_failed` with
`RateLimitError` / status / body when Groq raises instead of retrying.

This is a **free-tier quota** issue. Code cannot invent more TPD. Options:
wait for reset, upgrade Groq Dev tier, or send fewer/smaller prompts (Fix 1
helps the latter).

### Fix 4 — Gemini primary, Groq fallback (capacity, not graph changes)

`get_llm()` now returns `RoutingChatModel` (`agent/llm_router.py`). Gemini
`gemini-3.1-flash-lite` is primary; Groq `openai/gpt-oss-20b` is the 429
fallback. **Both** clients use `max_retries=0` so a 429 fails immediately
instead of the SDK sleeping (~24s Groq backoff, up to 6 Gemini retries).

- Gemini 429 → process-wide cooldown (RPM/TPM ~75s, or until Pacific midnight
  for RPD) and the same turn retries Groq.
- Groq 429 (or no Groq key) → friendly `AIMessage`, not a raw 429 in `/chat`.
- `LLM_PROVIDER=groq` still skips Gemini (debug). Logs: `llm_route` /
  `llm_route_429` on `agent.timing`.

Live RPM-burst demo (needs real keys, unset `LLM_PROVIDER`):
`python scripts/demo_gemini_rpm_fallback.py`

### Fix 5 — streaming (perceived latency only)

`POST /chat/stream` (SSE). The chat page consumes it: "thinking…" stays
through tool rounds, then tokens of the final sentence appear as they
generate. Backend wall time is unchanged. `POST /chat` remains for
non-streaming clients.

---

## 9. Gulshan + Jauhar additive scrape (2026-08-21)

Pending insert list trimmed **before** menus: 247 → **146**. Only the
Gulshan NIPA / Block 5 pin was cut (124 → top **23** by `review_count`
desc). Other pins unchanged. Untrimmed 247 kept in
`gulshan_jauhar_dry_run_247.txt`. Scrape used `--from-dry-run` (no re-crawl).

| Pin | Pending |
| --- | ---: |
| Gulshan-e-Iqbal Hasan Square | 64 |
| Gulistan-e-Jauhar Chowk | 35 |
| Gulshan-e-Iqbal NIPA / Block 5 | 23 |
| Gulistan-e-Jauhar north / Blocks 1–4 | 19 |
| Gulistan-e-Jauhar Johar Mor / Block 19 | 3 |
| Gulistan-e-Jauhar east / Block 18–19 | 2 |
| **Total** | **146** |

**Before:** 64 restaurants, 4241 menu items (max id 84).
**After:** 209 restaurants, 12224 menu items.
**New (id > 84):** 145 restaurants, 7983 menu items.

Original 64 rows and 4241 items unchanged (IDs 16–44 still 29).

**Step 4** (id > 84, null/empty/`%Rs%Rs%`/`%from%` prices): **0 rows**.

One pending vendor was not inserted: **Meltistry** (`yki7`, 0 reviews).
fd-api returned no menu categories; not inserted rather than an empty
restaurant. 145 / 146 of the trimmed list have menus.

Resume path: `python discover_gulshan_jauhar.py --from-dry-run gulshan_jauhar_dry_run.txt`
(already-in-DB URLs are skipped).

### Follow-up bug — the 145 new rows were invisible to area search

Symptom: "I'm in Jauhar and want chai" returned **2** restaurants (Pizza Yumm's
and The MAFIA 360), neither of which sells chai, while the snapshot actually
held dozens of Jauhar chai shops.

Three causes, all introduced or exposed by the scrape:

1. **Sparse inserts.** `--from-dry-run` rebuilt vendors from the dry-run text
   file, which only carries name, code, url, reviews and pin. All 145 new rows
   landed with `address`, `rating`, `cuisine`, `delivery_time` and `image_url`
   **NULL**. `insert_restaurant_with_menu` wrote exactly what it was given.
2. **Area search was address-only.** `search_restaurants` filtered on
   `address LIKE '%area%' OR name LIKE '%area%'`. With NULL addresses, the only
   rows that could match "Jauhar" were the two with "Jauhar" in their *name* —
   exactly the two the agent reported.
3. **Spelling.** fd-api addresses are owner-typed: "Gulistan e **jauhar** block
   13", "gulistan e **Johar** block 12", "Gulistan-e-**Johar**". Searching the
   user's spelling alone drops the rest even once addresses exist.

Fixes:

- `discover_gulshan_jauhar.hydrate_listing_fields()` now fills listing columns
  from `fetch_vendor_meta` before insert on the `--from-dry-run` path, so this
  cannot recur.
- New `restaurants.delivery_areas` column records the **discovery pin area**.
  A street address is where the kitchen sits, not its delivery zone — Harmain
  Sharifain is addressed in Bahadurabad but delivers to the Gulshan pin.
- `backfill_listing_meta.py` repairs existing rows: fills only NULL/empty
  columns (never overwrites), sets `delivery_areas`, idempotent and resumable.
- `db/geo.py` gains `AREA_ALIASES` + `area_search_terms()`; `db/queries.py`
  gains `location_match_sql()`, matching `delivery_areas`, `address` and `name`
  across every known spelling.
- `agent/tools.py` no longer emits the "No address literally contains 'X'"
  hint (which told the model to *deny coverage*) when the area is recorded in
  `delivery_areas` or spelled differently.

Result for "chai" in Jauhar: **2 → 15** restaurants; area coverage **2 → 62**.
Gulshan chai: **27**. Original 64 rows unaffected (Tariq Road still 2, 4241
items, IDs 16–44 still 29). Step 4 price check still **0 rows**.
Regression test: `tests/test_area_search.py`.

`fetch_vendor_meta` now records its HTTP status in `LAST_MENU_STATUS`, so the
stop-after-5-403s guard actually works for listing-only passes.

**Backfill state:** 96 / 145 rows have full listing metadata; all 145 have
`delivery_areas`, which is what area search depends on. The remaining 49 are
vendors that were closed at the time of the run, so they are absent from the
live disco feed (disco only returns currently-listed vendors) and fd-api was
403ing. They are still searchable and orderable — menus and prices are
complete — they just render without a rating or photo. Re-run at a different
hour to finish:

```
python backfill_listing_meta.py --from-disco   # preferred, disco is rarely blocked
python backfill_listing_meta.py                # per-vendor fd-api, needs the 403 to lift
```

Spelling aliases must stay data-driven: `python scripts/area_spellings.py`
prints the spellings actually present (johar 18, jauhar 5, jouhar 2). Note
"Jalandhari Road" is a nearby street, not the area — aliases are literal
substrings so it does not match.

### Follow-up bug — parallel tool calls crashed on unannotated state keys

Symptom: "2 parathas and 3 cup chai, do it for me from quetta mashallah" died
with

```
InvalidUpdateError: At key 'showcase': Can receive only one value per step.
```

A multi-item request makes the model emit **parallel tool calls**: one
`search_menu` for parathas and one for chai in a single graph step. Both write
`OrderState["showcase"]`, and an unannotated `TypedDict` key is a LastValue
channel, which rejects two writes in one step.

`showcase` was only the first key to blow up. `cart` had the same defect and
would have crashed on the very next step (`['add_to_cart', 'add_to_cart']`),
and `cart` was the more dangerous one: every parallel tool call reads the state
as it was *before* the step, so two tools each returning a whole cart would
have silently dropped one of the two items even with a last-write-wins reducer.

Fixes in `agent/state.py`, one per key according to what the key means:

- **`showcase`** (cards the chat UI paints) → `merge_showcase` reducer. Cards
  carry a step stamp (`showcase_step()` = message count, identical for every
  call in a round and larger in the next), so writes in the *same* round merge
  and a *later* round replaces. The user asked for parathas **and** chai, so
  seeing both card sets is the point. Deduped on `(kind, id)`, capped at 24.
  Tools that clear cards now write a stamped empty showcase instead of `None`,
  so a sibling search that found nothing cannot wipe cards a parallel search
  did find, whichever write lands last.
- **`cart`** → restructured to **deltas**. `add_to_cart` / `remove_from_cart`
  return `{"op": "add"/"remove", ...}` instead of the whole cart, and
  `apply_cart_write` applies them. Deltas compose, so parallel adds accumulate
  and two adds of the same item sum their quantities. A plain list still means
  "replace", which keeps graph seeding and the cart-clearing restaurant switch
  working.
- **scalars** (`location`, `restaurant_id`, `order_summary`, preferences) →
  `take_latest`, so parallel `remember_preferences` or `lock_restaurant` calls
  resolve to the later write instead of crashing.

`add_to_cart`'s reply now says its total covers the pre-turn cart plus that one
add, and to call `view_cart` once if several adds happened in the same turn —
otherwise the model could quote a total that omits its sibling's item.
`serialize_state` strips the internal step stamp and reports an empty showcase
as `None`, so the HTTP/UI contract is unchanged.

Parallel calling is **still on** — the point was to make it correct, not to
disable it. Live run of the exact failing request
(`python scripts/check_parallel_multi_item.py`): 6 tool rounds, **4 of them
parallel**, including `['search_menu', 'search_menu']` and
`['add_to_cart', 'add_to_cart']`. Final cart: Sada Paratha x2 + Special Doodh
Patti Chai x3, subtotal 477, total 584.99, and the showcase held both paratha
and chai cards. Regression test: `tests/test_parallel_tool_writes.py` (8 tests,
each failing with `InvalidUpdateError` before the fix).

Unrelated observation from that run: the card strip shows "Meetha Paratha" and
"Elaichi Chai" twice. Those are distinct `item_id`s with the same name in the
snapshot, so a single search already produced them — a scraper dedupe question,
not a reducer one.

---

## 10. Milestone 2 — daily live scraping (2026-08-21)

`scheduler/daily_scrape.py` existed before this milestone but had **never
run**: `scrape_runs` was empty and all 209 rows had `updated_at` NULL. Reading
it turned up four defects that would each have quietly damaged the snapshot, so
M2 became a rework rather than a switch-on.

### 10.1 The listing index was single-pin — the big one

`build_listing_index()` paginated disco around `config.DEFAULT_LAT/LNG`
(Saddar). Disco returns vendors that deliver **to the queried point**, and 145
of 209 rows were discovered from Gulshan/Jauhar pins 10–15 km away.

Measured with `python scripts/check_listing_coverage.py` (live API, ~12:40
Karachi):

| Index | Distinct vendors | Covers of our 209 |
| --- | ---: | ---: |
| Saddar alone | 133 | **16 (8%)** |
| All 16 pins | 1,954 | **127 (61%)** |

Per area, Saddar found **0 of 87** Gulshan rows and **0 of 58** Jauhar rows.
The job would have failed 69% of the dataset every night, then re-fetched those
menus at coordinates outside their delivery zones — which returns an empty menu
(§7), i.e. it would have looked like mass restaurant death.

Fix: crawl the union of the pins the data actually came from (11 well-known + 7
Gulshan/Jauhar, deduped by coordinate = **16**), now defined once in
`scraper/areas.py` and imported by both discovery scripts and the scheduler so
they cannot drift. Menu fetches use the vendor's own lat/lng (new `latitude` /
`longitude` columns), then the pin that saw it, then the city centre.

### 10.2 `replace_restaurant_menu` erased backfilled metadata

It wrote every listing column unconditionally, and the refresh path builds a
stub for vendors missing from the feed. So one closed evening would have set
`cuisine`, `address`, `delivery_time` and `image_url` back to NULL — undoing the
§9 backfill. Now every listing column is `COALESCE(NULLIF(?, ''), col)` in both
`replace_restaurant_menu` and `update_listing_only`: a field absent from
today's response means *no observation*, never *no value*.

### 10.3 The stale-lock check would wedge the scheduler permanently

`_pid_alive()` used `os.kill(pid, 0)`. On Windows `signal.CTRL_C_EVENT == 0`,
so that call is not a liveness probe at all. Verified with
`python scripts/check_pid_probe.py` (win32, Python 3.13.9):

- probing a **dead** pid **returns without raising**, so a dead pid reports as
  alive;
- the call delivers a console **Ctrl+C to the process group** — in some runs the
  probing process itself took an async `KeyboardInterrupt`.

Consequence: the first crash that left `daily_scrape.lock` behind would make
every later run exit with "Another daily scrape is running", forever, with
stale data as the only symptom. Replaced with `OpenProcess` +
`GetExitCodeProcess` on Windows, `os.kill` only on POSIX, plus a 2-hour
lock-age backstop that makes it self-healing either way.

Confirmed against a real killed run, not just the unit test
(`python scripts/check_stale_lock.py`): a trial scrape was hard-killed mid-run,
leaving a lock holding pid 17984. While that process was alive `_pid_alive`
returned True and `acquire_lock()` correctly refused (no double-run); after the
kill it returned False and the next run reclaimed and released the lock
cleanly. The old probe would have reported the dead pid as alive.

### 10.4 `os.replace` had no retry

On Windows the swap fails if anything holds the target open, and the agent
opens short read-only connections constantly. A collision is brief but would
have thrown away a 20-minute scrape. Now 10 retries 0.5 s apart, and if it
still fails the sidecar is kept rather than deleted.

### Schedule: 21:00 Asia/Karachi, one run

The old default was `CronTrigger(hour=6)` — inside the pre-dawn window §9
showed under-collects, because disco only lists vendors that are currently
open. `scrape.log` bears it out: the two pre-dawn batches (03:44–05:43,
04:29–09:38) produced the 48 rows that never got metadata, while the 15:19
afternoon batch produced none.

Chosen: a single **21:00** run, inside Karachi's dinner peak. A second 09:30
pass for the nashta/chai cohort (74 of 209 rows carry morning-trade signals:
nashta / halwa puri / chai / paratha / Quetta hotel) is written and commented
out in the daemon, deferred by decision.

### "Closed today" vs "actually gone"

Nothing is ever deleted or blanked for being absent. New columns
`last_seen_at`, `missing_streak`, `availability` track it instead:

- in any pin's feed → streak resets to 0;
- absent, but per-vendor fd-api still answers → **streak stays 0**, because that
  is proof the restaurant exists (§6: absent vendors returned HTTP 200 with
  `is_active=True`);
- absent **and** fd-api returns nothing → streak +1;
- a **403 never counts** — a block is evidence about our access, not about a
  restaurant;
- streak ≥ **6** → `availability='unlisted'` and a WARNING, for a human to judge.

With one run a day that is six days. This guard is load-bearing for the single
evening window: the 74 morning-only vendors are shut at 21:00 and would
otherwise all be flagged gone inside a week.

### Publishing is gated

Scrape into a sidecar copy, verify, then swap. The run is **not published** if
the broken-price diagnostic is non-zero, if the restaurant count dropped, or if
more restaurants have empty menus than before; a rejected sidecar is kept as
`foodpanda.db.rejected` and the live file is untouched. Five consecutive fd-api
403s end the menu phase early (status `blocked`) instead of hammering
PerimeterX; listing updates already gathered are kept and yesterday's menus
stay. Rate limiting is unchanged at 1.5–3.0 s.

Menus still go through `scrape_menu()` → `fetch_menu()` → `normalize_menu()` →
`extract_item_prices()` with `page=None`, so the §8 DOM concatenation bug cannot
return.

Measured run cost: listing index **8.3 min** (497 s, 16 pins); full run
~21–25 min.

### Test discovery collision, fixed

Two top-level `db` packages (root `db/` for the agent, `foodpanda-scraper/db/`
for the scraper) meant `tests/test_replace_menu.py` passed alone and failed
under `unittest discover`, depending on which bound `db` first. The scraper's
package is now **`scraperdb/`** (11 importers updated); the agent's `db/` was
left alone since `agent/`/`api/` are out of scope. `tests/test_daily_scrape.py`
adds 16 tests over a temp DB with the network faked — no live requests.

Details and rejected alternatives: `plans/SCRAPE_SCHEDULE_PLAN.md`.

### M2 paused before its first live run (2026-08-21)

Work stopped here to start Milestone 6. The live database was never modified:
still 209 / 1,671 / 12,224, 0 broken prices, `updated_at` NULL on every row, no
schedule enabled. Everything above is implemented and covered by tests, but a
full live trial has not completed — the trial run against `trial_copy.db` was
killed at pin 11 of 16. Resume instructions are in `plans/SCRAPE_SCHEDULE_PLAN.md`
under "Resume here".


## 11. Milestone 6 — API service layer (2026-08-21)

Not a scraper change, but it touches this file's assumptions once, so it is
recorded here.

Tenants, API keys and usage counters live in **`data/tenants.db`**, not in
`foodpanda.db`. The daily scrape copies the snapshot to a sidecar, scrapes for
~25 minutes, then `os.replace`s the original. Anything written to
`foodpanda.db` during that window is discarded by the swap, because the sidecar
predates it — so billing rows kept there would silently vanish for 25 minutes
every night. `db/queries.py` already opens the snapshot `mode=ro`, which is the
right posture for a file the scraper republishes; the API layer keeps it that
way.

Nothing in `scraperdb/`, `scheduler/` or the scraper scripts changed.


## 12. Consolidated project status (2026-08-21)

Written so a later session does not have to re-derive this from the plan
files. M2 scheduler code is paused and must not be touched until a live trial
is actually scheduled.

| Milestone | State |
| --- | --- |
| **1 — chat** | Done, stable. `POST /v1/chat`, tools, ranking, policies, in-memory sessions. |
| **2 — live scraping** | **Built, never run against the live DB.** `scheduler/daily_scrape.py` and the schema migration exist (`plans/SCRAPE_SCHEDULE_PLAN.md`). `updated_at` is NULL on all 209 restaurants, `scrape_runs` has zero rows, no schedule enabled. Deprioritized; not started on the live file. |
| **3 — deals / reviews** | Deals not started (`discounts` / `discounts_info` not in a table). Review **text** is not available from listing/fd-api APIs (`REVIEWS_FEASIBILITY.md`). A **manual sample** of written reviews for 23 Gulistan-e-Jauhar-area restaurants was imported into `reviews` via `scripts/import_manual_reviews.py` — see `plans/REVIEWS_IMPORT_PLAN.md`. Agent tool: `get_reviews`. Not city-wide coverage. |
| **4 — order status** | Not started. Needs a real user session capture first. |
| **5 — voice** | Done. Push-to-talk STT, server-side Orpheus TTS, mute/stop. |
| **6 — API service layer** | Done and tested: auth, tenant isolation, rate limiting, usage metering, `/v1` routes. Verified live: tenant isolation (automated + manual dual-key curl). Rate limiting confirmed live under concurrent load (21 parallel requests, 6× 429 as expected for a 20/min limit). |

### Two live bugs found in manual testing (open at time of this entry)

1. **Orphaned user turn on provider failure.** Observed: Gemini 503. The user message is appended to session/checkpointer state, then the LLM call fails, so no assistant reply is written. That user turn stays in history permanently.
2. **502 under concurrent `/v1/chat`.** 2 of 21 parallel requests returned 502 instead of 200 or the expected 429. Live server log for those two: `llm_call_failed type=ServerError … 503 UNAVAILABLE` / "This model is currently experiencing high demand", then FastAPI maps the raised exception to 502. Not a `showcase`/`cart` parallel-tool collision (those reducers already exist); the burst used distinct `session_id`s (`t2:burst-*`). Same-session concurrent turns remain a separate hazard (LangGraph `MemorySaver` is unlocked).

### 12.1 Both bugs fixed (same day)

Root cause of the 502s was the same Gemini 503: the router only failed over to Groq on **429**, so `UNAVAILABLE` / high-demand raised out of the agent node. LangGraph had already checkpointed the user message, so a 502 also left an orphaned user turn.

Fixes:

- Gemini (and Groq) **5xx / UNAVAILABLE** now fail over like a 429, without starting the quota cooldown.
- If both providers fail, the graph commits a paired assistant message (`Sorry, having trouble right now, try again`) instead of raising. `/v1/chat` returns **200**, history stays user+assistant.
- Per-session lock around `graph.invoke`, and a lock around `MemorySaver` writes, so concurrent turns on one `session_id` cannot interleave.

Verified: unit tests for 503→Groq, both-down fallback, 21-way concurrent TestClient (only 200/429), same-session pairing. Live retest `scripts/check_concurrent_chat.py`: **21 parallel `/v1/chat`, 10×200 + 11×429, 0×502**. Full suite **116 tests, OK**. M2 scheduler still untouched.

### 12.2 Manual review text sample (2026-08-25)

Foodpanda APIs still expose no public review bodies (`REVIEWS_FEASIBILITY.md`).
User-supplied copy from the Foodpanda app for **23 Gulistan-e-Jauhar-area**
restaurants lives in `data/manual_reviews_raw.txt` and was imported by
`scripts/import_manual_reviews.py` into `reviews` (`source=manual_sample`).

- **107** review rows across **23** restaurants (all headers matched on import).
- Reviewer first names are **not** stored.
- `liked_dishes` JSON holds `{name, item_id}` when a menu match exists (~87%
  of Liked lines on first import).
- Agent: `get_reviews(restaurant_id)` — see `plans/REVIEWS_IMPORT_PLAN.md`.
- Re-run import is idempotent (`INSERT OR IGNORE` on restaurant_id + text).
- `ON DELETE CASCADE` on `reviews.restaurant_id`.

### 12.3 Shared restaurant-name matcher (2026-08-25)

`db/name_match.py` is the only restaurant-name scorer: `0.65·SequenceMatcher
+ 0.35·Jaccard`, after folding punctuation and Johar/Jauhar. Profiles:

| Profile | min combo | min gap | min tokens |
| --- | --- | --- | --- |
| IMPORT (review headers) | 0.88 | 0.12 | 1 |
| CONVERSATIONAL (`get_reviews` / resolve) | 0.58 | 0.12 | 2 |
| SEARCH_PROMOTE (top-5 boost) | 0.58 | 0.05 | 2 |

Conversational resolve also requires every query token to appear in the
candidate name, then applies session location as a pre-filter. Without a
location, chain branches (Pizza Day Night Johar vs FB Area, The Big Pizza,
Broadway, Rehmat-e-Shereen) return **AMBIGUOUS** so the agent asks which
branch — not “couldn't find it”.

Pairwise scan of all 209 names: high similarity is almost all **same-brand
branches** or the **New Quetta / Quetta Alamgir** families already designed
for. Residual risks (documented, not threshold changes):

- Two-token names just under 0.58 after dropping a branch suffix (`Red Apple`
  0.578, `Domino's Pizza` 0.577) stay `no_match` until the user adds a word.
- Short shared prefixes (`new quetta`) can uniquely hit the **shortest**
  eligible name (`New Quetta Chai`) because combo favours length; users
  should name the hotel (Agha / Ajwa / …).
- Duplicate rows (`McChilli` twice, `Quetta Al Sadat` vs `Al-Sadat`) score
  1.0 and stay ambiguous.

Single distinctive tokens (`Jackson's`, `Dunkin'`, `Delizia`) are `no_match`
under the 2-token floor.

Live retest (`get_reviews`, 2026-08-25): `pizza day night` with location
Gulistan-e-Jauhar returns **4 reviews for id=174**. Same query with no
location returns **AMBIGUOUS** (Johar vs FB Area). `Jackson's` returns
**NO_MATCH** (1 token).



