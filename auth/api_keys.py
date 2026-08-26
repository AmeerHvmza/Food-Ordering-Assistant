"""API key generation, verification and tenant/key management.

Key format: ``fda_live_<43 url-safe chars>`` — 256 bits from
``secrets.token_urlsafe(32)``. The plaintext is returned once at creation and
never stored.

**Hashing is SHA-256, not bcrypt/argon2, and that is deliberate.** Slow KDFs
exist to make dictionary attacks on low-entropy human passwords expensive.
These keys are 256-bit random strings, where guessing is not a threat model,
and a slow hash would add tens of milliseconds to *every* API call while making
lookup impossible by index (you cannot query a bcrypt hash, so every request
would scan the key table). A single indexed SHA-256 lookup is both faster and
what token systems of this shape normally do.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from auth import store
from auth.tiers import Tier, get_tier

logger = logging.getLogger("api.auth")

KEY_PREFIX = "fda_live_"
PREFIX_DISPLAY_CHARS = 8

BEARER = "bearer"
HEADER_AUTHORIZATION = "Authorization"
HEADER_API_KEY = "X-API-Key"


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, resolved once per request."""

    key_id: int
    key_prefix: str
    tenant_id: int
    tenant_name: str
    tier: Tier

    @property
    def namespace(self) -> str:
        """Prefix that isolates this tenant's sessions from every other one."""
        return f"t{self.tenant_id}"


# ---------------------------------------------------------------------------
# key material
# ---------------------------------------------------------------------------


def generate_key() -> tuple[str, str, str]:
    """Return (plaintext, sha256_hash, display_prefix)."""
    secret = secrets.token_urlsafe(32)
    plaintext = f"{KEY_PREFIX}{secret}"
    return plaintext, hash_key(plaintext), secret[:PREFIX_DISPLAY_CHARS]


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.strip().encode("utf-8")).hexdigest()


def extract_key(request: Request) -> str | None:
    """Pull the key from Authorization: Bearer, then X-API-Key."""
    header = request.headers.get(HEADER_AUTHORIZATION) or ""
    if header:
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == BEARER and parts[1].strip():
            return parts[1].strip()
    direct = request.headers.get(HEADER_API_KEY)
    if direct and direct.strip():
        return direct.strip()
    return None


# ---------------------------------------------------------------------------
# management
# ---------------------------------------------------------------------------


def create_tenant(name: str, tier: str = "free") -> int:
    get_tier(tier)  # fail now if the tier does not exist
    store.init_db()
    with store.session() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO tenants (name, tier, created_at) VALUES (?, ?, ?)",
                (name, tier, store.utc_now()),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Tenant '{name}' already exists") from exc
        conn.commit()
        return int(cur.lastrowid)


def create_key(tenant_id: int, label: str | None = None) -> str:
    """Create a key and return the plaintext. This is the only time it exists."""
    store.init_db()
    plaintext, key_hash, prefix = generate_key()
    with store.session() as conn:
        tenant = conn.execute(
            "SELECT id FROM tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
        if tenant is None:
            raise ValueError(f"No such tenant: {tenant_id}")
        conn.execute(
            """
            INSERT INTO api_keys (tenant_id, key_hash, key_prefix, label, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (tenant_id, key_hash, prefix, label, store.utc_now()),
        )
        conn.commit()
    return plaintext


def revoke_key(key_id: int) -> bool:
    with store.session() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (store.utc_now(), key_id),
        )
        conn.commit()
        return cur.rowcount > 0


def list_tenants() -> list[dict[str, Any]]:
    store.init_db()
    with store.session() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM tenants ORDER BY id")]


def list_keys(tenant_id: int | None = None) -> list[dict[str, Any]]:
    store.init_db()
    sql = """
        SELECT k.id, k.tenant_id, t.name AS tenant_name, t.tier, k.key_prefix,
               k.label, k.created_at, k.last_used_at, k.revoked_at
        FROM api_keys k JOIN tenants t ON t.id = k.tenant_id
    """
    params: tuple[Any, ...] = ()
    if tenant_id is not None:
        sql += " WHERE k.tenant_id = ?"
        params = (tenant_id,)
    sql += " ORDER BY k.id"
    with store.session() as conn:
        return [dict(row) for row in conn.execute(sql, params)]


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def resolve(plaintext: str) -> Principal | None:
    """Look a key up. Returns None for unknown or revoked keys."""
    if not plaintext:
        return None
    digest = hash_key(plaintext)
    with store.session() as conn:
        row = conn.execute(
            """
            SELECT k.id AS key_id, k.key_hash, k.key_prefix, k.revoked_at,
                   t.id AS tenant_id, t.name AS tenant_name, t.tier,
                   t.disabled_at
            FROM api_keys k JOIN tenants t ON t.id = k.tenant_id
            WHERE k.key_hash = ?
            """,
            (digest,),
        ).fetchone()
        if row is None:
            return None
        # The lookup already matched on an indexed column; this constant-time
        # compare is belt and braces against a future non-exact lookup.
        if not hmac.compare_digest(row["key_hash"], digest):
            return None
        if row["revoked_at"]:
            return None
        if row["disabled_at"]:
            raise HTTPException(
                status_code=403,
                detail=f"Tenant '{row['tenant_name']}' is disabled.",
            )
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
            (store.utc_now(), row["key_id"]),
        )
        conn.commit()

    return Principal(
        key_id=int(row["key_id"]),
        key_prefix=row["key_prefix"],
        tenant_id=int(row["tenant_id"]),
        tenant_name=row["tenant_name"],
        tier=get_tier(row["tier"]),
    )


UNAUTHENTICATED = (
    "Missing or invalid API key. Send it as 'Authorization: Bearer <key>' or "
    "'X-API-Key: <key>'."
)


def require_api_key(request: Request) -> Principal:
    """Authenticate the caller. Never echoes the presented key back."""
    presented = extract_key(request)
    if not presented:
        raise HTTPException(status_code=401, detail=UNAUTHENTICATED)
    principal = resolve(presented)
    if principal is None:
        logger.warning(
            "auth rejected route=%s reason=unknown_or_revoked_key",
            request.url.path,
        )
        raise HTTPException(status_code=401, detail=UNAUTHENTICATED)
    # Stashed so the metering middleware can attribute the request even when a
    # later dependency (rate limiting) rejects it.
    request.state.principal = principal
    return principal
