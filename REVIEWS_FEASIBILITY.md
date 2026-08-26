# Reviews feasibility (investigation only)

**Date:** 2026-08-21  
**Scope:** Read-only. No schema, tools, prompts, or scraper changes.  
**Question:** Can we fetch **review text** (not star counts) for Karachi Foodpanda restaurants with the same class of unauthenticated APIs the scraper already uses?

**Verdict: not feasible.** Star counts already exist. Public written review bodies do not appear in any reachable listing or vendor-detail payload, the conventional `/reviews` REST paths 404, and the live restaurant HTML that would reveal a hidden XHR is PerimeterX-blocked from this environment. Treat this as a closed no for a reviews milestone unless a future HAR from a real browser session shows a different host.

---

## 1. Prior finding — confirmed, and scoped

NOTES.md §6 (2026-08-13) and the Milestone 3 line in NOTES §12 said:

> `review_with_comment_number` is `0` for every vendor observed. Review *text* is therefore **not** available from this listing feed and will need a different source (vendor detail page or a reviews endpoint).

That was **scoped to disco listing** `GET https://disco.deliveryhero.io/listing/api/v1/pandora/vendors` → `data.items[]`. It was **not** a full-API conclusion. The per-vendor fd-api payload and a dedicated reviews URL were the open questions.

**Re-check (2026-08-21), same disco endpoint, no cookies:**

| Probe | Result |
| --- | --- |
| Saddar pin `24.8607, 67.0011`, `sort=rating_desc`, `limit=48`, offsets `0 / 48 / 96` | **144 vendors**, `review_with_comment_number` unique set `{0}` |
| `review_number` on the same rows | non-zero; max **40236** |

The 2026-08-13 listing finding is still accurate. It does **not** by itself prove review text is absent everywhere — that required the checks below.

**Correction to the informal “0 reviews across the feed” phrasing:** the feed is full of **ratings**. `review_number` is populated (snapshot top: Foods Inn 39,949; live disco/fd-api for `s5nn` now **40,236**). What is zero is **`review_with_comment_number`** — the API’s own count of reviews that include a comment.

---

## 2. Every endpoint the scraper currently hits

Source: `foodpanda-scraper/config.py`, `scraper/api_client.py`, `scraper/listing.py`, `scraper/menu.py`, `scheduler/daily_scrape.py` (read only), `backfill_review_counts.py`, `scripts/fetch_policies.py`.

None of these persist review **text**. `normalize_vendor` keeps `review_number` only. The snapshot has no `reviews` table; `restaurants.review_count` is the listing count.

### 2.1 Disco listing (primary restaurant list)

```
GET https://disco.deliveryhero.io/listing/api/v1/pandora/vendors
```

- **Kind:** listing / search feed (lat/lng, `limit`/`offset`, optional `sort=rating_desc`).
- **Returns:** `data.items[]` vendor cards.
- **Auth:** none. Header `x-disco-client-id: web`. Same as production.
- **Review-related field names (verbatim):** `rating`, `review_number`, `review_with_comment_number`, `is_checkout_comment_enabled`.
- **Used by:** `api_client.fetch_vendors`, daily scrape listing phase, review-count backfill.

Example (one listing card, Saddar feed, 2026-08-21). Address truncated.

```json
{
  "code": "pv2x",
  "name": "(omitted in this excerpt; present on the live object)",
  "rating": 5.0,
  "review_number": 5788,
  "review_with_comment_number": 0,
  "is_checkout_comment_enabled": false
}
```

First item on that page also exposed `discounts`, `discounts_info`, `tags` (deals — out of scope here). No `reviews` array.

### 2.2 Disco single-vendor path (not used by the scraper; checked anyway)

```
GET https://disco.deliveryhero.io/listing/api/v1/pandora/vendors/{code}
    ?latitude=&longitude=&country=pk&language_id=1
```

- **Kind:** detail for one restaurant, listing-shaped (`item` not `data.items`).
- **Auth:** same `x-disco-client-id: web`. HTTP 200 without a session.
- **Review fields:** same four names. Still `review_with_comment_number: 0`.

| code | name (snapshot) | `review_number` | `review_with_comment_number` | `is_checkout_comment_enabled` |
| --- | --- | --- | --- | --- |
| `s5nn` | Foods Inn - SMCHS | 40236 | 0 | false |
| `s6pp` | McDonald's - Atrium Mall | 19984 | 0 | false |
| `u2vk` | Delizia - Garden | 14602 | 0 | false |

These three were chosen for **live high volume**, not because the DB had empty counts.

Appending `/reviews` to this path: **HTTP 404** (`Resource not found`) with disco headers.

### 2.3 fd-api vendor detail / menu (primary menu + meta fallback)

```
GET https://pk.fd-api.com/api/v5/vendors/{vendor_code}
    ?latitude=&longitude=&language_id=1
    [&include=menus]
```

- **Kind:** single-restaurant detail. With `include=menus`, full menu tree. Without it, `fetch_vendor_meta` (review-count backfill when a vendor is missing from disco).
- **Auth:** not a user login. Requires `X-FP-API-KEY: volo`, pseudo `perseus-client-id` / `perseus-session-id`. Missing Perseush headers → HTTP 400. PerimeterX **403** is a known risk on this host (NOTES; daily scrape stops after consecutive 403s).
- **Richer than disco:** extra keys include `deals`, `menus`, `metadata`, `customer_phone`, `topic_ratings`, `scoreCriteria`, `tags`, `is_checkout_comment_enabled`, etc.
- **Review-related field names (verbatim):** `rating`, `review_number`, `review_with_comment_number`, `is_checkout_comment_enabled`, plus unused-looking `topic_ratings` (null on `s5nn`), `scoreCriteria`.
- **No `reviews` key** even with `include=menus` or `include=menus,reviews,topic_ratings,characteristics`.

Example (`s5nn`, no menus, 2026-08-21). Phone and full address omitted.

```json
{
  "code": "s5nn",
  "rating": 4.9,
  "review_number": 40236,
  "review_with_comment_number": 0,
  "is_checkout_comment_enabled": true,
  "topic_ratings": null,
  "scoreCriteria": {
    "vendor_id": 0,
    "vendor_code": "",
    "coefficients": null,
    "vendor_values": null
  }
}
```

`is_checkout_comment_enabled: true` on fd-api vs `false` on disco for the same code is a **checkout / order-comment flag**, not a public review feed. It does not come with comment bodies.

### 2.4 www.foodpanda.pk menu alias (secondary, often blocked)

```
GET https://www.foodpanda.pk/api/v5/vendors/{vendor_code}/menu
```

Tried after fd-api in `fetch_menu`. NOTES: PerimeterX captcha JSON for bare `requests`. Not a reviews source.

### 2.5 Playwright DOM fallback (listing + restaurant pages)

```
https://www.foodpanda.pk/restaurants/new?lat=...&lng=...
https://www.foodpanda.pk/restaurant/{code}/{url_key}
```

HTML navigation. NOTES §3: often `Access to this page has been denied`. This investigation: restaurant URL returned **HTTP 403**, title **Access to this page has been denied**, PerimeterX captcha page. **No network XHR list could be captured** from this environment (see §3).

### 2.6 Policy pages (not restaurant data)

`scripts/fetch_policies.py` hits Terms / FAQ on `www.foodpanda.pk/contents/...`. Irrelevant to reviews.

---

## 3. Undiscovered / candidate endpoints

Live restaurant **pages** could not be instrumented (PerimeterX). Candidates were probed with the scraper’s disco/fd-api headers, plus GraphQL with `Origin: https://www.foodpanda.pk` and `Content-Type: application/json`.

Targets with visibly high live `review_number`: **Foods Inn `s5nn` (~40k)**, **McDonald's Atrium `s6pp` (~20k)**, **Delizia `u2vk` (~14.6k)**.

| URL / action | HTTP | Notes |
| --- | --- | --- |
| `pk.fd-api.com/api/v5/vendors/{code}/reviews` | **404** `page not found` | All three vendors. Blog posts that list this path are **wrong for PK today**. |
| `www.foodpanda.pk/api/v5/vendors/{code}/reviews` | 404 HTML / PX | Same as other www hosts. |
| `.../customer-reviews`, `.../ratings`, `.../feedback`, `.../comments`, `.../topic_ratings` | 404 | |
| `pk.fd-api.com/api/v6/vendors/{code}/reviews` | 404 JSON `not_found` | |
| `pk.fd-api.com/api/v5/reviews?vendor_code=` | 404 | |
| `pk.fd-api.com/api/v5/vendors/{code}?include=reviews` | 200 | Same vendor object; **no reviews list**. |
| `sg.fd-api.com` / `tw.fd-api.com` `.../reviews` | 404 | Sanity: not a hidden regional path we can copy. |
| `disco.../pandora/vendors/{code}/reviews` | 404 | With `x-disco-client-id: web`. |
| `disco.../listing/api/v1/pandora/reviews` | 404 | `Invalid or non-existent country: null` even with query params. |
| `disco.deliveryhero.io/reviews/...` | **530** Cloudflare 1016 | Origin DNS — host does not exist behind the CDN. |
| `reviews-api.eh.deliveryhero.io`, `reviews-api.deliveryhero.io`, `reviews.api.deliveryhero.io`, `api.deliveryhero.io` | DNS NXDOMAIN | Guessed hosts; not live. |
| `GET www.foodpanda.pk/restaurant/s5nn/...` | **403** PX | Cannot scroll a “reviews” UI or dump `__NEXT_DATA__`. |
| `POST pk.fd-api.com/api/v5/graphql` | 200 | GraphQL is real (see below). **No reviews field found.** |

### GraphQL (`pk.fd-api.com/api/v5/graphql`)

- Open enough for a typed query with browser-like Origin + JSON content-type (CSRF if those are missing).
- Introspection disabled.
- `Query.vendor(input: RequestParams!)` exists. Required: `vendorId: String!`, `globalEntityId: String!`. `FP_PK` + `s5nn` (or numeric disco `id` `3757`) returns `{ "__typename": "Vendor" }`.
- `Query.reviews` **does not exist**. `Vendor.reviews` / `customerReviews` / `reviewNumber` / `name` / `id` all **do not exist** on this `Vendor` type. The field is a stub, not a reviews API.

Without the web app’s persisted-query hashes (only available from a successful page load), GraphQL cannot be mined further from here.

### Text vs tags

No review bodies were returned anywhere. The only “tag-like” copy on the vendor object was **deal** `tags` (e.g. `"Up to 20% off"`), not canned review phrases. `topic_ratings` was **null**. There is no evidence of either free-text **or** templated public reviews on these APIs — only a numeric `review_number`.

### Freshness / history

N/A: no review-list endpoint. `review_number` itself is live (snapshot 39,949 vs live 40,236 on Foods Inn).

### Rate limiting / volume (why even a found endpoint would be a hard sell)

If a paginated list existed at 20 comments/page:

- Foods Inn alone ≈ 40k ratings → **~2,000 pages** if every rating had text (the API says **zero** have comments).
- 209 restaurants × thousands of ratings would be **hundreds of thousands of requests**, on a host that already 403s the menu scrape. That would be unviable even if the path existed.

The `review_with_comment_number: 0` on **both** listing and the richer fd-api detail, for vendors with tens of thousands of ratings, is stronger than a missing URL: **this market’s public vendor payload does not advertise any commented reviews to fetch.**

---

## 4. Reliability (for completeness)

| Criterion | Finding |
| --- | --- |
| Auth vs current scraper | Disco listing/detail: same open headers. fd-api: same Perseush + `volo` key. GraphQL: Origin header, still no reviews. No session cookie unlocked a reviews list. |
| Rate limits | Menu fd-api already PerimeterX-sensitive. A full-history comment crawl would be worse; moot given 404s and comment count 0. |
| Free text vs canned tags | Neither present. |
| Live-only vs backfill | No list to backfill. |

**Limitation (do not over-read):** this machine cannot complete a real-browser DevTools capture of `foodpanda.pk`. A logged-in mobile app might call a private reviews service we did not see. That is a **hypothesis**, not a working endpoint. It is not enough to call reviews **feasible** for this project.

---

## 5. Verdict

**Not feasible** to scrape review **text** for the assistant.

Reasons, in order:

1. Disco listing (the original NOTES claim): `review_with_comment_number` still **0** on every vendor in a 144-row paginated sample while `review_number` is large.
2. Per-vendor fd-api (previously unchecked for text): **same** `review_with_comment_number: 0`, **no** `reviews` array, richer payload otherwise.
3. The usual Delivery Hero `/vendors/{code}/reviews` path is **404** on `pk.fd-api.com` for three high-volume restaurants.
4. Guessed reviews hosts do not resolve or 404/530.
5. GraphQL `Vendor` does not expose reviews.
6. Restaurant HTML is bot-blocked, so no hidden XHR was observed.

**Already available (do not rebuild):** `restaurants.review_count` ← `review_number`, used by ranking. That is counts, not text.

Do **not** start a reviews milestone, schema, or tool. If someone later captures a HAR from a working Foodpanda session that shows a paginated comments API with non-empty bodies, reopen this as a **new** investigation (inspect real shape → propose schema → confirm), same gate as deals — not a build from this report.

---

## 6. How this was probed (not wired into production)

One-off `requests` from the repo, using `_disco_headers()` / `_fd_api_headers()` from `scraper/api_client.py`. Temporary probe scripts were not added to the scraper, scheduler, or agent. No `daily_scrape.py` edits.
