"""Keyword-Helfer für Hype-Erkennung im Chat."""

from __future__ import annotations

import re

#: Standardliste (kommagetrennt, case-insensitive), leer = diese Liste.
DEFAULT_HYPE_KEYWORDS = (
    "haha, hahaha, lol, lmao, xd, 😂, 🤣, geil, alter, digga, krass, lost, "
    "rip, gg, clutch, nein, wtf, omg, unfassbar, krank, böse, aufstehen, "
    "wallah, ehre, schwöre"
)

_ALNUM_TOKEN = re.compile(r"[a-z0-9]+")
_NEVER_MATCH = re.compile(r"(?!)")


def parse_keywords(raw: str | None) -> tuple[str, ...]:
    """Zerlegt eine kommagetrennte Liste; leer/None fällt auf den Standard zurück."""
    text = raw if raw and raw.strip() else DEFAULT_HYPE_KEYWORDS
    keywords: list[str] = []
    seen: set[str] = set()
    for part in text.split(","):
        keyword = part.strip().lower()
        if keyword and keyword not in seen:
            seen.add(keyword)
            keywords.append(keyword)
    return tuple(keywords)


def keyword_pattern(keywords: tuple[str, ...]) -> re.Pattern[str]:
    """Baut eine case-insensitive Alternation mit Wortgrenzen für Alnum-Tokens."""
    parts: list[str] = []
    for keyword in keywords:
        if _ALNUM_TOKEN.fullmatch(keyword):
            parts.append(rf"(?<!\w){re.escape(keyword)}(?!\w)")
        else:
            parts.append(re.escape(keyword))
    if not parts:
        return _NEVER_MATCH
    return re.compile("|".join(parts), re.IGNORECASE)


def has_hype_keyword(text: str, pattern: re.Pattern[str]) -> bool:
    return bool(pattern.search(text))
