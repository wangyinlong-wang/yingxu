"""Cross-source topic clustering and time-aware heat ranking, kept free of network and model calls."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime

# fmt: off
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "in", "into", "is",
    "it", "its", "new", "now", "of", "on", "or", "our", "the", "this", "to", "today", "we", "with",
    "you", "your", "introducing", "announcing", "launch", "launches", "released", "release", "releases",
}
# fmt: on
_ASCII_TOKEN = re.compile(r"[a-z0-9][a-z0-9.+-]*")
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")
_VERSION = re.compile(r"\d+(?:\.\d+)*")
CLUSTER_WINDOW_HOURS = 72
MAX_RELATED = 20


def title_tokens(title: str) -> set[str]:
    text = (title or "").casefold()
    tokens = {
        token.rstrip(".-+")
        for token in _ASCII_TOKEN.findall(text)
        if len(token.rstrip(".-+")) >= 2 and token.rstrip(".-+") not in _STOPWORDS
    }
    for run in _CJK_RUN.findall(text):
        tokens.update(run[index : index + 2] for index in range(max(1, len(run) - 1)))
    return tokens


def versions_compatible(left_title: str, right_title: str) -> bool:
    """ "Gemini 3.8" and "Gemini 3.6" share words but are different releases."""
    left, right = set(_VERSION.findall(left_title or "")), set(_VERSION.findall(right_title or ""))
    return not left or not right or left == right


def same_event(left: set[str], right: set[str]) -> bool:
    """Conservative match: a false merge hides a story, so short or loosely related titles never merge."""
    shared = len(left & right)
    if shared < 3:
        return False
    return shared / min(len(left), len(right)) >= 0.75 and shared / len(left | right) >= 0.4


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def heat_score(topic, instant: datetime | None = None) -> float:
    """Relevance, freshness, engagement and cross-source confirmation; recomputed on every read."""
    instant = instant or datetime.now(UTC)
    stamp = _parse(topic.published_at) or _parse(topic.created_at) or instant
    age_hours = max(0.0, (instant - stamp).total_seconds() / 3600)
    relevance = float(topic.relevance_score or 0) * 0.6
    freshness = 30 * math.exp(-age_hours / 36)
    engagement = min(15.0, 3 * math.log10(1 + max(0.0, float(topic.score or 0))))
    confirmation = min(20.0, 8 * max(0, int(topic.cluster_size or 1) - 1))
    return round(relevance + freshness + engagement + confirmation, 1)
