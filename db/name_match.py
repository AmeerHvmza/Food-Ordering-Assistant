"""Shared restaurant-name matching.

One scoring function (SequenceMatcher + Jaccard) with per-use-case
thresholds. Location filtering is applied by the caller as a candidate
pre-filter, not as a substitute for scoring.

See plans/NAME_MATCH_UNIFY_PLAN.md.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Literal


def normalize_name(text: str) -> str:
    s = (text or "").lower().replace("'", "").replace("\u2019", "")
    s = s.replace("johar", "jauhar")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def combo_score(query: str, candidate_name: str) -> float:
    nq = normalize_name(query)
    nn = normalize_name(candidate_name)
    ratio = SequenceMatcher(None, nq, nn).ratio()
    tq, tn = set(nq.split()), set(nn.split())
    jacc = len(tq & tn) / len(tq | tn) if (tq | tn) else 0.0
    return 0.65 * ratio + 0.35 * jacc


def query_tokens(query: str, *, min_len: int = 2) -> list[str]:
    return [tok for tok in normalize_name(query).split() if len(tok) >= min_len]


def tokens_contained_in_name(
    query: str,
    candidate_name: str,
    *,
    min_len: int = 2,
) -> bool:
    tokens = query_tokens(query, min_len=min_len)
    if not tokens:
        return False
    padded = f" {normalize_name(candidate_name)} "
    return all(f" {tok} " in padded for tok in tokens)


@dataclass(frozen=True)
class MatchProfile:
    min_combo: float
    min_gap: float
    min_query_tokens: int
    require_token_containment: bool = True


IMPORT = MatchProfile(
    min_combo=0.88,
    min_gap=0.12,
    min_query_tokens=1,
    require_token_containment=False,
)
CONVERSATIONAL = MatchProfile(
    min_combo=0.58,
    min_gap=0.12,
    min_query_tokens=2,
    require_token_containment=True,
)
SEARCH_PROMOTE = MatchProfile(
    min_combo=0.58,
    min_gap=0.05,
    min_query_tokens=2,
    require_token_containment=True,
)


@dataclass(frozen=True)
class RankedNameMatch:
    score: float
    index: int
    name: str


@dataclass
class NameMatchDecision:
    kind: Literal["match", "ambiguous", "no_match"]
    best_index: int | None
    best_score: float | None
    second_index: int | None
    second_score: float | None
    reason: str | None
    ranked: list[RankedNameMatch]
    eligible_ranked: list[RankedNameMatch]


def rank_name_matches(
    query: str,
    candidate_names: Sequence[str],
) -> list[RankedNameMatch]:
    ranked = [
        RankedNameMatch(score=combo_score(query, name), index=i, name=name)
        for i, name in enumerate(candidate_names)
    ]
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked


def eligible_indices(
    query: str,
    candidate_names: Sequence[str],
    profile: MatchProfile,
    *,
    allowed: Sequence[int] | None = None,
) -> list[int]:
    tokens = query_tokens(query)
    if len(tokens) < profile.min_query_tokens:
        return []
    pool = list(allowed) if allowed is not None else list(range(len(candidate_names)))
    if not profile.require_token_containment:
        return pool
    return [
        i
        for i in pool
        if tokens_contained_in_name(query, candidate_names[i])
    ]


def decide_name_match(
    query: str,
    candidate_names: Sequence[str],
    profile: MatchProfile,
    *,
    allowed: Sequence[int] | None = None,
) -> NameMatchDecision:
    ranked = rank_name_matches(query, candidate_names)
    empty = NameMatchDecision(
        kind="no_match",
        best_index=None,
        best_score=None,
        second_index=None,
        second_score=None,
        reason=None,
        ranked=ranked,
        eligible_ranked=[],
    )
    tokens = query_tokens(query)
    if len(tokens) < profile.min_query_tokens:
        empty.reason = (
            f"Query {query!r} has {len(tokens)} token(s); "
            f"need at least {profile.min_query_tokens}."
        )
        return empty

    eligible = eligible_indices(
        query, candidate_names, profile, allowed=allowed
    )
    eligible_ranked = [item for item in ranked if item.index in set(eligible)]
    if not eligible_ranked:
        best = ranked[0] if ranked else None
        empty.best_index = best.index if best else None
        empty.best_score = best.score if best else None
        empty.reason = f"No eligible name match for {query!r}."
        return empty

    best = eligible_ranked[0]
    second = eligible_ranked[1] if len(eligible_ranked) > 1 else None
    gap = best.score - (second.score if second else 0.0)

    if best.score < profile.min_combo:
        return NameMatchDecision(
            kind="no_match",
            best_index=best.index,
            best_score=best.score,
            second_index=second.index if second else None,
            second_score=second.score if second else None,
            reason=(
                f"Best combo {best.score:.3f} for {best.name!r} "
                f"is below {profile.min_combo}."
            ),
            ranked=ranked,
            eligible_ranked=eligible_ranked,
        )
    if second is not None and (gap < profile.min_gap or second.score == best.score):
        return NameMatchDecision(
            kind="ambiguous",
            best_index=best.index,
            best_score=best.score,
            second_index=second.index,
            second_score=second.score,
            reason=(
                f"Ambiguous name {query!r}: {best.name!r} "
                f"({best.score:.3f}) vs {second.name!r} "
                f"({second.score:.3f}), gap={gap:.3f}."
            ),
            ranked=ranked,
            eligible_ranked=eligible_ranked,
        )
    return NameMatchDecision(
        kind="match",
        best_index=best.index,
        best_score=best.score,
        second_index=second.index if second else None,
        second_score=second.score if second else None,
        reason=None,
        ranked=ranked,
        eligible_ranked=eligible_ranked,
    )


def resolve_restaurant_row(
    query: str,
    rows: Sequence[dict[str, Any]],
    profile: MatchProfile,
    location: str | None = None,
    in_location: Callable[[dict[str, Any], str], bool] | None = None,
) -> NameMatchDecision:
    names = [str(row.get("name") or "") for row in rows]
    allowed: list[int] | None = None
    if location and in_location is not None:
        allowed = [i for i, row in enumerate(rows) if in_location(row, location)]
        if not allowed:
            allowed = list(range(len(rows)))
    return decide_name_match(query, names, profile, allowed=allowed)
