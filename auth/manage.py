"""CLI for tenants and API keys — the way to bootstrap a key without a dashboard.

    python -m auth.manage create-tenant "Acme Foods" --tier pro
    python -m auth.manage create-key 1 --label "acme production"
    python -m auth.manage list-keys
    python -m auth.manage revoke-key 3
    python -m auth.manage usage 1
    python -m auth.manage tiers
"""

from __future__ import annotations

import argparse
import sys

from auth import api_keys, store, usage as usage_mod
from auth.tiers import TierConfigError, load_tiers


def _create_tenant(args: argparse.Namespace) -> int:
    try:
        tenant_id = api_keys.create_tenant(args.name, args.tier)
    except (ValueError, TierConfigError) as exc:
        print(f"error: {exc}")
        return 1
    print(f"tenant #{tenant_id}  name={args.name}  tier={args.tier}")
    print(f"next: python -m auth.manage create-key {tenant_id}")
    return 0


def _create_key(args: argparse.Namespace) -> int:
    try:
        plaintext = api_keys.create_key(args.tenant_id, args.label)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    print("API key created. This is the only time it is shown, copy it now:\n")
    print(f"    {plaintext}\n")
    print("Use it as:")
    print(f'    curl -H "Authorization: Bearer {plaintext}" ...')
    return 0


def _list_tenants(_: argparse.Namespace) -> int:
    rows = api_keys.list_tenants()
    if not rows:
        print("no tenants yet")
        return 0
    for row in rows:
        state = "disabled" if row["disabled_at"] else "active"
        print(f"#{row['id']:<4} {row['name']:<28} tier={row['tier']:<10} {state}")
    return 0


def _list_keys(args: argparse.Namespace) -> int:
    rows = api_keys.list_keys(args.tenant_id)
    if not rows:
        print("no keys yet")
        return 0
    for row in rows:
        state = "revoked" if row["revoked_at"] else "active"
        last = row["last_used_at"] or "never used"
        print(
            f"#{row['id']:<4} {api_keys.KEY_PREFIX}{row['key_prefix']}...  "
            f"tenant={row['tenant_name']} ({row['tier']})  {state}  "
            f"label={row['label'] or '-'}  last_used={last}"
        )
    return 0


def _revoke_key(args: argparse.Namespace) -> int:
    if api_keys.revoke_key(args.key_id):
        print(f"key #{args.key_id} revoked")
        return 0
    print(f"key #{args.key_id} not found or already revoked")
    return 1


def _usage(args: argparse.Namespace) -> int:
    daily = usage_mod.usage_summary(args.key_id, args.days)
    if not daily:
        print("no usage recorded for this key")
        return 0
    print(f"{'day':<12} {'requests':>9} {'tokens':>10}")
    for row in daily:
        print(f"{row['day']:<12} {row['requests']:>9} {row['total_tokens'] or 0:>10}")
    print("\nmost recent requests:")
    for row in usage_mod.recent_events(args.key_id, 10):
        print(
            f"  {row['created_at']}  {row['method']:<5} {row['route']:<34} "
            f"{row['status_code']}  {row['latency_ms']}ms  "
            f"tokens={row['total_tokens'] or 0}"
        )
    return 0


def _tiers(_: argparse.Namespace) -> int:
    for name, tier in sorted(load_tiers().items()):
        print(
            f"{name:<12} {tier.requests_per_minute:>5}/min "
            f"{tier.requests_per_day:>9}/day  burst={tier.burst:<4} "
            f"{tier.description}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m auth.manage",
        description="Manage API tenants, keys and usage.",
    )
    subs = parser.add_subparsers(dest="command", required=True)

    p = subs.add_parser("create-tenant", help="Create a tenant")
    p.add_argument("name")
    p.add_argument("--tier", default="free")
    p.set_defaults(func=_create_tenant)

    p = subs.add_parser("create-key", help="Issue a key for a tenant")
    p.add_argument("tenant_id", type=int)
    p.add_argument("--label", default=None)
    p.set_defaults(func=_create_key)

    p = subs.add_parser("list-tenants", help="List tenants")
    p.set_defaults(func=_list_tenants)

    p = subs.add_parser("list-keys", help="List keys")
    p.add_argument("--tenant-id", type=int, default=None)
    p.set_defaults(func=_list_keys)

    p = subs.add_parser("revoke-key", help="Revoke a key")
    p.add_argument("key_id", type=int)
    p.set_defaults(func=_revoke_key)

    p = subs.add_parser("usage", help="Show usage for a key")
    p.add_argument("key_id", type=int)
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=_usage)

    p = subs.add_parser("tiers", help="Show configured tiers")
    p.set_defaults(func=_tiers)

    return parser


def main(argv: list[str] | None = None) -> int:
    store.init_db()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
