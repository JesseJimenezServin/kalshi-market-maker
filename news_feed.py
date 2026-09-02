"""
Zero-cost, purely advisory news-awareness signal for the risk engine.

Polls GDELT's free Doc 2.0 API (https://api.gdeltproject.org/api/v2/doc/doc
-- no API key required, no cost) for recent articles matching keywords
derived from each watched market's title/subtitle, and flags "relevant"
articles using a coarse RULE-BASED keyword-overlap score -- there is no
LLM classification and no paid service anywhere in this module. This
deliberately trades precision for zero cost: keyword matching WILL have
false positives (an unrelated article happens to share words with the
market title) and false negatives (a genuinely relevant article uses none
of the derived keywords). The signal is only ever used downstream as a
"something might be happening, be more cautious" nudge into the risk
engine (see risk_engine.RiskEngine.register_news_event) -- never a
directional or high-confidence trading signal.

Read-only: this module only issues GET requests (to GDELT and, via
rest_client.KalshiRestClient, to Kalshi's GET-only market endpoint) and
writes rows to SQLite via logger.py. No order-placing code anywhere.

CAVEAT: this sandbox's network does not reach api.gdeltproject.org (same
restriction as Kalshi's own API -- see README), so the request/response
handling below has NOT been exercised against a live GDELT response.
GDELT's Doc 2.0 API is loosely documented and has been known to change
shape; verify _parse_articles()'s field names against a real response on
your own machine before trusting this signal, and adjust if needed.
"""

from __future__ import annotations

import asyncio
import logging
import re

import requests

import config
from logger import SimulationLogger
from rest_client import KalshiRestClient

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "and", "or",
    "is", "are", "was", "were", "be", "been", "will", "would", "should",
    "by", "with", "from", "this", "that", "these", "those", "its", "it",
    "as", "than", "then", "into", "over", "under", "up", "down", "out",
    "not", "no", "yes", "if", "so", "but", "about", "above", "after",
    "before", "between", "during", "vs", "will",
}

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9\-]+")


def derive_keywords(title: str, subtitle: str, max_keywords: int = None) -> list[str]:
    """
    Rule-based keyword extraction (NO LLM, NO NLP library): lowercases
    title+subtitle, splits on non-alphanumeric boundaries, drops a small
    stopword list and very short tokens, dedupes, and keeps the longest
    remaining words (a crude proxy for "distinctive" -- longer words tend
    to be proper nouns / specific terms rather than filler).
    """
    max_keywords = max_keywords or config.NEWS_MAX_KEYWORDS
    text = f"{title} {subtitle}".lower()
    words = _WORD_RE.findall(text)
    candidates = sorted(
        {w for w in words if len(w) >= 4 and w not in _STOPWORDS},
        key=len,
        reverse=True,
    )
    return candidates[:max_keywords]


def _build_gdelt_query(keywords: list[str]) -> str:
    # GDELT's query syntax ORs bare terms together when space-separated
    # inside no explicit boolean; using explicit " OR " is the documented,
    # unambiguous way to request "any of these keywords".
    return " OR ".join(keywords)


def fetch_recent_articles(keywords: list[str], lookback_hours: float, max_records: int = 20) -> list[dict]:
    """
    Queries GDELT's Doc 2.0 API (mode=ArtList) for articles matching any of
    `keywords`, published in the last `lookback_hours`. Returns [] (logging
    a warning, never raising) on any network/parse failure -- a flaky free
    API must not be allowed to crash the paper-trading pipeline.
    """
    if not keywords:
        return []

    params = {
        "query": _build_gdelt_query(keywords),
        "mode": "ArtList",
        "format": "json",
        "maxrecords": max_records,
        "timespan": f"{max(1, int(lookback_hours))}h",
    }
    try:
        resp = requests.get(config.GDELT_DOC_API_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.warning("GDELT query failed for keywords=%s", keywords, exc_info=True)
        return []

    return _parse_articles(data)


def _parse_articles(data: dict) -> list[dict]:
    """
    Normalizes GDELT's ArtList response into a flat list of
    {"headline", "source_url", "theme_tags"} dicts. GDELT's documented
    ArtList JSON shape is {"articles": [{"title":..., "url":..., ...}]} --
    theme tags are a Global Knowledge Graph (GKG) concept and are NOT
    reliably present on plain ArtList records, so theme_tags defensively
    checks a couple of possible key names and otherwise stays empty
    (relevance scoring then falls back to title-only matching).
    """
    articles = data.get("articles") or []
    parsed = []
    for a in articles:
        if not isinstance(a, dict):
            continue
        theme_tags = a.get("themes") or a.get("theme_tags") or []
        if isinstance(theme_tags, str):
            theme_tags = [t for t in theme_tags.split(";") if t]
        parsed.append({
            "headline": a.get("title", ""),
            "source_url": a.get("url", ""),
            "theme_tags": theme_tags,
        })
    return parsed


def score_relevance(keywords: list[str], article: dict) -> float:
    """
    Coarse RULE-BASED relevance score -- keyword overlap only, no NLP/LLM.
    Score = fraction of `keywords` that appear (as substrings) in the
    article's title or theme tags, combined. Range [0.0, 1.0].

    This is intentionally crude and will misfire in both directions
    relative to a true classifier:
      - False positive: an unrelated article happens to contain a generic
        keyword derived from the market title (e.g. a common city name).
      - False negative: a genuinely relevant article is phrased using none
        of our derived keywords (synonyms, different framing).
    Treat every "relevant" flag as a weak, non-directional signal only.
    """
    if not keywords:
        return 0.0

    haystack = " ".join([article.get("headline", "")] + list(article.get("theme_tags", []))).lower()
    hits = sum(1 for kw in keywords if kw in haystack)
    return hits / len(keywords)


def _market_keywords(rest_client: KalshiRestClient, ticker: str) -> list[str]:
    """
    Fetches market metadata via the existing GET-only rest_client.get_market
    and derives search keywords from its title/subtitle.

    CAVEAT: exact field names ("title"/"subtitle") follow Kalshi's
    documented market object shape but haven't been exercised against a
    live call in this sandbox (network-restricted, same as GDELT above) --
    verify on your own machine and adjust if Kalshi's response differs.
    """
    try:
        response = rest_client.get_market(ticker)
    except Exception:
        logger.warning("Failed to fetch market metadata for %s", ticker, exc_info=True)
        return []

    market = response.get("market", response)
    title = market.get("title", "") or ""
    subtitle = market.get("subtitle", "") or ""
    return derive_keywords(title, subtitle)


async def news_refresh_loop(
    rest_client: KalshiRestClient,
    states: dict,
    sim_logger: SimulationLogger,
    interval_sec: float,
) -> None:
    """
    Periodically polls GDELT for each ticker in `states` and, for any
    article scoring at/above config.NEWS_RELEVANCE_THRESHOLD, logs it to
    the news_events table and registers a decaying news_signal on that
    ticker's RiskEngine (states[ticker].risk_engine). No-ops entirely if
    config.NEWS_FEED_ENABLED is False.
    """
    if not config.NEWS_FEED_ENABLED:
        logger.info("News feed disabled (config.NEWS_FEED_ENABLED = False) -- news_refresh_loop is a no-op.")
        return

    while True:
        for ticker, state in states.items():
            try:
                # _market_keywords/fetch_recent_articles make blocking
                # `requests` calls (to Kalshi and GDELT respectively); run
                # them off the event loop so they don't freeze the
                # WebSocket connection / scoring_loop while waiting on a
                # slow or unreachable GDELT (each ticker can block up to
                # its 10s connect timeout otherwise).
                keywords = await asyncio.to_thread(_market_keywords, rest_client, ticker)
                articles = await asyncio.to_thread(fetch_recent_articles, keywords, config.NEWS_LOOKBACK_HOURS)
                for article in articles:
                    score = score_relevance(keywords, article)
                    if score >= config.NEWS_RELEVANCE_THRESHOLD:
                        sim_logger.log_news_event(
                            ticker=ticker,
                            headline=article.get("headline", ""),
                            source_url=article.get("source_url", ""),
                            relevance_score=score,
                            theme_tags=article.get("theme_tags", []),
                        )
                        state.risk_engine.register_news_event()
                        logger.info(
                            "%s: relevant news hit (score=%.2f): %s",
                            ticker, score, article.get("headline", ""),
                        )
            except Exception:
                logger.exception("News poll failed for %s", ticker)

        await asyncio.sleep(interval_sec)
