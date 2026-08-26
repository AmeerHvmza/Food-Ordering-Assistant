"""Split assistant replies into Orpheus-safe TTS chunks.

Orpheus (`canopylabs/orpheus-v1-english`) rejects input over 200 characters.
This module has no I/O so the split points can be tested without Groq.

Rules:
- Prefer sentence, then clause, then word boundaries.
- Do not split ``Rs.`` from ``750``, or ``4.9`` in the middle.
- Strip markdown/emoji so they neither consume budget nor get spoken.
"""

from __future__ import annotations

import re
import unicodedata

ORPHEUS_CHAR_LIMIT = 200

# Periods that are not sentence ends when they follow these tokens.
_ABBREV = {
    "rs", "pkr", "dr", "mr", "mrs", "ms", "st", "vs", "etc", "no",
    "vol", "approx", "est", "id", "item_id", "min", "hr", "hrs",
}

_MD_NOISE = re.compile(r"[*_`#]+")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MULTI_SPACE = re.compile(r"[ \t]+")


def strip_for_speech(text: str) -> str:
    """Drop markdown and symbols that should not be spoken."""
    cleaned = _MD_LINK.sub(r"\1", text or "")
    cleaned = _MD_NOISE.sub("", cleaned)
    cleaned = "".join(
        ch for ch in cleaned
        if unicodedata.category(ch) not in {"So", "Sk"}
    )
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _MULTI_SPACE.sub(" ", cleaned)
    return cleaned.strip()


def _is_sentence_dot(text: str, index: int) -> bool:
    """True if text[index] is a '.' that ends a sentence."""
    if text[index] != ".":
        return False
    after = text[index + 1:] if index + 1 < len(text) else ""
    ahead = after.lstrip()
    if ahead[:1].isdigit():
        return False
    start = index
    while start > 0 and text[start - 1].isalnum():
        start -= 1
    token = text[start:index].lower()
    if token in _ABBREV:
        return False
    if after[:1].isspace() and ahead[:1].isupper():
        return True
    if after[:1] in "\n":
        return True
    if not ahead:
        return True
    return False


def _split_sentences(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        buf.append(ch)
        if ch == "\n":
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
        elif ch in "?!" or (ch == "." and _is_sentence_dot(text, i)):
            if ch == "." or ch in "?!":
                j = i + 1
                while j < len(text) and text[j] in "\"')":
                    buf.append(text[j])
                    j += 1
                piece = "".join(buf).strip()
                if piece:
                    parts.append(piece)
                buf = []
                i = j - 1
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _split_clauses(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"(?<=[;:,])\s+", text) if p.strip()]


def _split_words(text: str) -> list[str]:
    return text.split()


def _pack(pieces: list[str], limit: int, joiner: str) -> list[str]:
    """Greedily join pieces without crossing `limit`."""
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = piece if not current else f"{current}{joiner}{piece}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(piece) <= limit:
            current = piece
        else:
            chunks.extend(_force_split(piece, limit))
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _force_split(text: str, limit: int) -> list[str]:
    """Last resort: cut at `limit`, trying to land on a space."""
    import logging

    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = rest.rfind(" ", 0, limit + 1)
        if cut < 1:
            logging.getLogger(__name__).warning(
                "TTS chunker hard-cut mid-token: %r", rest[:limit]
            )
            cut = limit
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    return [c for c in chunks if c]


def chunk_for_tts(text: str, limit: int = ORPHEUS_CHAR_LIMIT) -> list[str]:
    """Return non-empty chunks, each at most `limit` characters."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    cleaned = strip_for_speech(text)
    if not cleaned:
        return []

    chunks: list[str] = []
    for sentence in _split_sentences(cleaned):
        if len(sentence) <= limit:
            chunks.append(sentence)
            continue
        packed = _pack(_split_clauses(sentence), limit, " ")
        overflow: list[str] = []
        for part in packed:
            if len(part) <= limit:
                overflow.append(part)
            else:
                overflow.extend(_pack(_split_words(part), limit, " "))
        chunks.extend(overflow)
    return [c for c in chunks if c]
