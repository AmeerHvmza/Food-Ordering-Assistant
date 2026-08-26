# Food Ordering Assistant

A conversational ordering assistant over scraped Foodpanda Pakistan data. It
chats with you until you know what to order, recommends specific dishes with
real prices and ratings, and prepares an order summary you confirm.

**Milestones 1, 2 and 5: done. Milestones 3–4 and 6: planned, not built** —
see the roadmap below and [PLAN.md](plans/PLAN.md). Voice is a layer on top of
Milestone 1 chat; text chat still works standalone.

---

## Scope reality-check

*(Preserved verbatim from the product spec. Read this before promising anything
to anyone.)*

- Order *status tracking* ("where's my order") requires the user's own
  authenticated Foodpanda session per order — it is NOT something this service
  can generically scrape for arbitrary users. Treat this as a "requires user
  session token, best-effort" feature, not a core guarantee.
- Order *placement* has no public Foodpanda API — this product prepares and
  confirms an order summary; actual checkout is a future partner-integration
  point, not something built by scraping.
- Multi-country/white-label API resale is a business/legal decision (ToS, data
  rights), not just an engineering task — architect for it, but don't market it
  as done until that's been reviewed.

---

## What Milestone 1 actually does

- Chats naturally to draw out craving, party size, budget and deal-sensitivity
- Recommends restaurants ranked by a **review-weighted** rating, not raw stars
- Searches one locked restaurant's menu, builds a cart, prepares a summary
- Answers platform-policy questions from real Foodpanda Terms text
- Exposes all of it over a stable HTTP contract

What it does **not** do: place orders, track deliveries, quote live deals, or
authenticate anyone. Daily scrape (Milestone 2) can refresh the snapshot;
it is off until you enable the schedule.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # then add ONE provider API key

# Every /v1 route needs an API key, so issue one first:
python -m auth.manage create-tenant "local" --tier unlimited
python -m auth.manage create-key 1 --label "dev"

uvicorn api.main:app --reload
```

Open <http://127.0.0.1:8000> for the test chat page, or
<http://127.0.0.1:8000/docs> for the API. Check
<http://127.0.0.1:8000/health> if something looks wrong — it reports the
resolved model and snapshot row counts, and needs no key.

The chat page asks for the key once and keeps it in `localStorage`. To skip
that on a laptop, put the key in `.env` as `DEMO_API_KEY` and set `DEMO_MODE=1`;
the page is then served with it embedded. Leave `DEMO_MODE` off anywhere
public — anyone who can load the page can read that key.

Verify the wiring without spending API credits:

```bash
python scripts/smoke_test.py   # scripted stub model, no key needed
```

### Model providers

Whichever key is present wins, in the order OpenAI, Anthropic, Groq. Force one
with `LLM_PROVIDER`, and override the model with `LLM_MODEL` (defaults are
`gpt-4o-mini`, `claude-sonnet-4-5`, `openai/gpt-oss-120b`).

## API contract

Milestone 6 wrapped this with auth, rate limiting and metering rather than
redesigning it. Every functional route now lives under `/v1`.

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/sessions` | issue an unguessable session id |
| `POST /v1/chat` | `{session_id, message}` -> `{reply, state}` |
| `POST /v1/chat/stream` | same turn as SSE token events |
| `POST /v1/sessions/{id}/location` | `{lat, lng}` -> `{location, state}` |
| `POST /v1/sessions/{id}/welcome` | opening line, no model call |
| `GET /v1/sessions/{id}/cart` | current cart, item count, subtotal |
| `POST /v1/sessions/{id}/confirm` | order summary object |
| `POST /v1/voice/transcribe` | multipart `audio` -> `{text, latency_ms}` |
| `POST /v1/voice/speak` | `{text}` -> `audio/wav` |
| `GET /v1/usage` | your own request and token totals |
| `GET /health` | model + snapshot readiness, **no key needed** |

`state` is an `OrderState` snapshot: location, restaurant_id, restaurant_name,
party_size, craving, budget, deal_sensitive, cart, cart_subtotal,
order_summary, and messages as `{role, content}`.

Sessions live in memory and are lost on restart.

## Authentication, limits and metering (Milestone 6)

Full design in [API_SERVICE_PLAN.md](plans/API_SERVICE_PLAN.md).

### Issue a key

```bash
python -m auth.manage create-tenant "Acme Foods" --tier free
python -m auth.manage create-key 1 --label "acme production"
```

The key is printed **once** and never stored — only its SHA-256 hash is kept,
so a lost key is reissued, not recovered. Everything lives in `data/tenants.db`
(override with `TENANT_DB_PATH`), deliberately separate from the scraped
snapshot, which the daily scrape republishes wholesale.

```bash
python -m auth.manage list-keys          # active/revoked, last used
python -m auth.manage revoke-key 3
python -m auth.manage usage 1            # per-day requests and tokens
python -m auth.manage tiers
```

### Make a request

```bash
curl -X POST http://127.0.0.1:8000/v1/chat \
  -H "Authorization: Bearer fda_live_..." \
  -H "Content-Type: application/json" \
  -d '{"session_id": "demo-1", "message": "2 parathas and 3 cup chai"}'
```

`X-API-Key: fda_live_...` works too. Without a key you get `401`:

```json
{"detail": "Missing or invalid API key. Send it as 'Authorization: Bearer <key>' or 'X-API-Key: <key>'."}
```

### Rate limits

Limits come from [`config/tiers.json`](config/tiers.json) — data, not code, so
they change without touching request handling.

| Tier | Per minute | Per day | Burst |
| --- | --- | --- | --- |
| `free` | 20 | 100 | 10 |
| `pro` | 120 | 10,000 | 60 |
| `unlimited` | 600 | 1,000,000 | 200 |

Every response carries `X-RateLimit-Limit`, `X-RateLimit-Remaining` and
`X-RateLimit-Reset`. Exceeding a limit gives `429` with `Retry-After` in
seconds:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 3
X-RateLimit-Scope: minute
{"detail": "per-minute rate limit exceeded for tier 'free' (20 requests per minute). Retry in 3s."}
```

The per-minute bucket is in memory; the daily quota is in SQLite so it is not
reset by restarting the server. Rejected requests are logged but **do not**
consume daily quota. Note that the minute bucket is per process, so
`uvicorn --workers N` multiplies the effective burst limit by N — that is the
reason to move it to Redis before running multi-worker.

### Metering

One row per authenticated request in `usage_events` (route, status, latency,
and prompt/completion tokens when a model ran), rolled up per key per UTC day
in `usage_daily`. Callers can read their own totals at `GET /v1/usage`. Nothing
bills anyone; this is the data a billing system would consume.

### Tenant isolation

Sessions are namespaced per tenant, so two customers using the same
`session_id` get two unrelated sessions and neither can read the other's cart
or history. This is enforced by construction rather than by an ownership check,
and is covered by `tests/test_api_auth.py::TenantIsolationTests`.

Check a running server end to end:

```bash
python scripts/check_live_api.py <api_key>
```

### Voice (Milestone 5)

Additive wrapping of `POST /chat`. The agent graph is unchanged.

Click **Mic** (next to Send) and speak — the transcript updates in the
text box as you go. Click it again to stop. It does **not** send. Edit and
hit Send as usual. After the reply is on screen, Orpheus
(`canopylabs/orpheus-v1-english`) speaks it unless you hit **Mute**. **Stop**
cancels playback. Replies are chunked at 200 characters on the server and
concatenated into one WAV.

Voice needs `GROQ_API_KEY` even if chat uses OpenAI or Anthropic. Accept
Orpheus English terms in the Groq console or TTS returns 403. Override the
voice with `TTS_VOICE` (default `troy`). Text chat without the mic still
works. Details in [VOICE_PLAN.md](plans/VOICE_PLAN.md).

## How it is put together

```
agent/graph.py     LangGraph state graph: agent <-> tools loop
agent/tools.py     the 8 tools; the only source of factual claims
agent/prompts.py   system prompt + policies.md loading
agent/state.py     OrderState and serialization
agent/llm.py       provider selection from env
api/main.py        FastAPI routes
api/sessions.py    session access over the LangGraph checkpointer
scheduler/daily_scrape.py  Milestone 2 sidecar refresh
voice/stt.py       Groq Whisper transcription
voice/tts.py       Orpheus TTS, 200-char chunks, WAV concat
voice/chunker.py   sentence/clause split (no I/O)
db/queries.py      read-only SQL over the scraped snapshot
db/ranking.py      review-weighted rating
```

The agent's tools are `remember_preferences`, `search_restaurants`,
`lock_restaurant`, `search_menu`, `add_to_cart`, `remove_from_cart`,
`view_cart`, `confirm_order`. Restaurant search and menu search are separate so
the menu and cart tools can hard-fail before a restaurant is locked.

### Why ratings are weighted

Sorting by rating alone is misleading: the snapshot has a 5.0 backed by 63
reviews sitting next to a 4.9 backed by 39,949. `db/ranking.py` applies
IMDB-style shrinkage toward the dataset mean:

```
weighted_rating = (v / (v + m)) * R + (m / (v + m)) * C
```

`m` is the dataset median review count and `C` the dataset mean rating, both
computed at query time (currently 3,208 and 4.807) so a later re-scrape needs
no code change. Shrinkage is symmetric, so it also lifts bad-with-few-reviews
restaurants; the agent therefore always cites the review count and hedges
anything flagged low confidence. Relevance to the craving decides the ordering
tier first, because the weighted scores across the top restaurants sit within
0.005 of each other. Details in
[foodpanda-scraper/NOTES.md](foodpanda-scraper/NOTES.md) section 6.

### policies.md

Generated by `scripts/fetch_policies.py` from Foodpanda's real Terms and FAQ
pages, never from model memory. **These policies go stale** — foodpanda revises
its Terms without notice, so re-run the script periodically and diff the result.

`www.foodpanda.pk` sits behind PerimeterX and answers plain HTTP clients with
403, so the script defaults to extracting from dated page captures in
`scripts/captures/`. Refresh a capture by opening the URL in a real browser and
saving the rendered text; `--online` attempts a direct fetch first.

## Data

Read-only against `foodpanda-scraper/foodpanda.db` (Karachi well-known set;
original 29 IDs 16–44 kept). Override with `FOODPANDA_DB_PATH`. Grow the set
with `python discover_wellknown.py` from `foodpanda-scraper/` (review_count
≥ 3,208, 11 area centers). See
[KARACHI_WELLKNOWN_PLAN.md](plans/KARACHI_WELLKNOWN_PLAN.md) and
[foodpanda-scraper/README.md](foodpanda-scraper/README.md).

### Daily scrape (Milestone 2)

Refreshes **every restaurant already in the DB** — it does not discover new
areas or vendors. The scrape writes to a sidecar copy, verifies it, and only
then swaps it onto `foodpanda.db`, so the agent never reads a half-written
table and a failed run simply leaves yesterday's data in place.

#### Enabling the schedule

The run time matters. Foodpanda's listing feed only returns restaurants that
are currently open, so a pre-dawn run silently under-collects; the schedule is
**21:00 Asia/Karachi**, inside Karachi's dinner peak. See
[SCRAPE_SCHEDULE_PLAN.md](plans/SCRAPE_SCHEDULE_PLAN.md) §1 for the measurements
behind that.

```bash
pip install -r requirements.txt              # includes APScheduler
python scheduler/daily_scrape.py --run-now   # one refresh, now
python scheduler/daily_scrape.py --daemon    # stay resident, fire at 21:00
```

For an always-on box, prefer the OS scheduler over a resident daemon — a
missed window then re-runs instead of silently never happening.

Windows (Task Scheduler):

```powershell
schtasks /Create /TN "FoodpandaDailyScrape" /SC DAILY /ST 21:00 ^
  /TR "cmd /c cd /d D:\UserFiles\Desktop\fooddata && python scheduler\daily_scrape.py --run-now --run-label dinner"
```

Linux (crontab, host clock in Asia/Karachi):

```
0 21 * * * cd /path/to/fooddata && python scheduler/daily_scrape.py --run-now --run-label dinner >> /var/log/foodpanda_scrape.log 2>&1
```

A second 09:30 pass for the breakfast/chai cohort is written but switched off;
see the commented job in `scheduler/daily_scrape.py` and plan §1.

#### Checking last-run status and logs

```bash
python scheduler/daily_scrape.py --status
```

reports the recent runs, how many restaurants were refreshed versus kept, the
broken-price count, how many 403s were hit, plus how many rows are flagged
`unlisted` and how many have never been refreshed. The raw history is in the
`scrape_runs` table (`SELECT * FROM scrape_runs ORDER BY id DESC LIMIT 5;`) and
the full log appends to `foodpanda-scraper/scrape.log`.

Run statuses: `ok`, `partial` (some restaurants kept yesterday), `blocked`
(PerimeterX ended the menu phase early), `price_regression` (**not published** —
the sidecar is kept as `foodpanda.db.rejected` for inspection), `aborted`.

#### Testing without waiting for the schedule

```bash
python scheduler/daily_scrape.py --run-now --only-id 96      # one restaurant
python scheduler/daily_scrape.py --run-now --limit 5         # first five
python scheduler/daily_scrape.py --run-now --limit 5 --no-swap  # never publishes
python scheduler/daily_scrape.py --run-now --db-path /tmp/copy.db
```

Safety behaviours worth knowing: a restaurant missing from a day's feed is
**never** deleted or blanked — it is counted, and only after 6 consecutive runs
with no evidence it exists at all is it flagged `availability='unlisted'` for a
human to look at. Rate limiting is the same 1.5–3.0 s as every manual scrape.

Known limits of this snapshot: `delivery_time` is 45 minutes for nearly every
restaurant, so ETAs are not meaningfully differentiated; there is no review
text; and there are no real discount amounts, only menu categories that happen
to be named like deals.

---

## Roadmap: milestones 3–4 (not built)

**2. Live daily scraping. Built, paused before its first live run.** Sidecar
refresh of every row in `foodpanda.db` at 21:00 PKT (or `--run-now`), crawling
16 pins because a single city-centre pin reaches only 8% of the dataset. Does
not re-discover areas. The schedule is **not enabled**: a full live trial is
the one step left, and the commands for it are under "Resume here" in
[SCRAPE_SCHEDULE_PLAN.md](plans/SCRAPE_SCHEDULE_PLAN.md). The disco feed also carries
`distance` and `minimum_order_amount`, still unused by the agent.

**3. Deals + reviews layer.** Extend scraping to active deals and review text,
and feed both into the agent's reasoning. Note `discounts` and `discounts_info`
already ride along in the listing response, but `review_with_comment_number` is
0 across the feed, so review *text* needs a different source.

**4. Order status (best-effort, session-based).** Only after the user supplies
their own session context. Not a generic guarantee — see the scope
reality-check above.

**5. Voice layer. Done.** STT/TTS wrapped around this same chat backend, with
no changes to core agent logic. See [VOICE_PLAN.md](plans/VOICE_PLAN.md).

**6. Generic API service layer. Done.** API-key auth, per-key rate limiting,
usage metering, tier config and `/v1` versioning wrapped around the existing
contract. See the section above and
[API_SERVICE_PLAN.md](plans/API_SERVICE_PLAN.md). No billing integration, by design.
Whether this data can lawfully be resold is a separate question and is not
answered by having built it.
