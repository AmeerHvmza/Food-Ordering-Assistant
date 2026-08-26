"""Tier limits, loaded from config/tiers.json.

Kept as data so pricing can change without editing request-handling code. The
file is validated at import: a typo in a limit should stop the service at
startup, not hand somebody unlimited traffic at runtime.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config" / "tiers.json"

DEFAULT_TIER = "free"


class TierConfigError(RuntimeError):
    """Raised at startup when the tier file is missing or malformed."""


@dataclass(frozen=True)
class Tier:
    name: str
    description: str
    requests_per_minute: int
    requests_per_day: int
    burst: int


def config_path() -> Path:
    override = os.getenv("TIERS_CONFIG_PATH")
    return Path(override) if override else DEFAULT_CONFIG


def _parse(raw: dict, path: Path) -> dict[str, Tier]:
    tiers: dict[str, Tier] = {}
    for name, body in raw.items():
        if name.startswith("_"):  # keys like _comment are documentation
            continue
        if not isinstance(body, dict):
            raise TierConfigError(f"{path}: tier '{name}' is not an object")
        try:
            tier = Tier(
                name=name,
                description=str(body.get("description") or ""),
                requests_per_minute=int(body["requests_per_minute"]),
                requests_per_day=int(body["requests_per_day"]),
                burst=int(body.get("burst", body["requests_per_minute"])),
            )
        except KeyError as exc:
            raise TierConfigError(
                f"{path}: tier '{name}' is missing {exc}"
            ) from exc
        except (TypeError, ValueError) as exc:
            raise TierConfigError(
                f"{path}: tier '{name}' has a non-numeric limit: {exc}"
            ) from exc
        for field in ("requests_per_minute", "requests_per_day", "burst"):
            if getattr(tier, field) <= 0:
                raise TierConfigError(
                    f"{path}: tier '{name}'.{field} must be greater than 0"
                )
        tiers[name] = tier

    if not tiers:
        raise TierConfigError(f"{path}: no tiers defined")
    if DEFAULT_TIER not in tiers:
        raise TierConfigError(f"{path}: the default tier '{DEFAULT_TIER}' is missing")
    return tiers


@lru_cache(maxsize=1)
def load_tiers() -> dict[str, Tier]:
    path = config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TierConfigError(f"Tier config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TierConfigError(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise TierConfigError(f"{path}: top level must be an object")
    return _parse(raw, path)


def get_tier(name: str | None) -> Tier:
    """Limits for a tier name.

    An unknown name is an error rather than a silent fallback: a tenant whose
    tier was renamed in config should fail visibly, not quietly inherit the
    free allowance (or worse, an unlimited one).
    """
    tiers = load_tiers()
    key = name or DEFAULT_TIER
    if key not in tiers:
        raise TierConfigError(
            f"Unknown tier '{key}'. Known tiers: {', '.join(sorted(tiers))}"
        )
    return tiers[key]


def tier_names() -> list[str]:
    return sorted(load_tiers())


def reload_tiers() -> None:
    """Drop the cache. Used by tests that write a temp config."""
    load_tiers.cache_clear()
