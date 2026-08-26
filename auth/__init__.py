"""Multi-tenant service layer: API keys, rate limits, usage metering.

Deliberately independent of `agent/` and `db/`. Tenant data lives in its own
SQLite file (see `auth.store`), never in the scraped snapshot.
"""
