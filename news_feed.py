"""
Zero-cost, purely advisory news-awareness signal for the risk engine.

Polls Google News' public RSS search (https://news.google.com/rss/search
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

Read-only: this module only issues GET requests (to Google News and, via
rest_client.KalshiRestClient, to Kalshi's GET-only market endpoint) and
writes rows to SQLite via logger.py. No order-placing code anywhere.

HISTORY (2026-09-02): originally used GDELT's Doc 2.0 API. GDELT turned
out to be completely unreachable in practice -- CONFIRMED via curl
(connect timeout, read timeout, and SSL handshake timeout all observed
across repeated attempts, while Kalshi's own API and other HTTPS hosts
from the same machine worked fine, ruling out a general network issue)
-- so the signal had never actually worked. Replaced with Google News
RSS, verified live end-to-end (including through Python's `requests`,
not just curl) with a real multi-keyword OR query before shipping this.
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

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
    Rule-based keyword extraction (NO LLM, NO NLP library): splits
    title+subtitle on non-alphanumeric boundaries, drops a small
    stopword list and very short tokens, dedupes, and ranks CAPITALIZED
    words (a crude but effective proper-noun signal -- e.g. "Mamdani",
    "Jacksonville", "EverBank") ahead of everything else, falling back to
    longest-first among the rest to fill any remaining slots.

    CONFIRMED NECESSARY live 2026-09-02: the previous version lowercased
    the text FIRST, throwing away capitalization, then ranked purely by
    length -- for the market titled "Will Mamdani sign above 0 executive
    orders...", that picked "executive" (9 chars, generic) ahead of
    "mamdani" (7 chars, the market's actual distinctive subject). Length
    alone is not a reliable proxy for distinctiveness; case is a much
    better one for the common case of a title built around a proper
    noun. See is_relevant(), which depends on keyword ORDER (not just
    this function) to reject false positives.
    """
    max_keywords = max_keywords or config.NEWS_MAX_KEYWORDS
    text = f"{title} {subtitle}"
    words = _WORD_RE.findall(text)

    seen: set[str] = set()
    proper: list[str] = []
    generic: list[str] = []
    for w in words:
        lw = w.lower()
        if len(lw) < 4 or lw in _STOPWORDS or lw in seen:
            continue
        seen.add(lw)
        (proper if w[:1].isupper() else generic).append(lw)

    generic.sort(key=len, reverse=True)
    return (proper + generic)[:max_keywords]


def _build_news_query(keywords: list[str]) -> str:
    # Google's search syntax ORs terms together with an explicit " OR "
    # between them -- same unambiguous "any of these keywords" query
    # GDELT used, just against a different backend.
    return " OR ".join(keywords)


def fetch_recent_articles(keywords: list[str], lookback_hours: float, max_records: int = 20) -> list[dict]:
    """
    Queries Google News' public RSS search for articles matching any of
    `keywords`, filtered client-side to the last `lookback_hours` (Google
    News RSS has no documented, verifiable timespan parameter, so pubDate
    is parsed and compared instead of relying on an unverified search
    operator). Returns [] (logging a warning, never raising) on any
    network/parse failure -- a flaky free source must not be allowed to
    crash the paper-trading pipeline.
    """
    if not keywords:
        return []

    params = {
        "q": _build_news_query(keywords),
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    }
    try:
        resp = requests.get(config.NEWS_RSS_API_URL, params=params, timeout=10)
        resp.raise_for_status()
    except Exception:
        logger.warning("Google News RSS query failed for keywords=%s", keywords, exc_info=True)
        return []

    return _parse_articles(resp.text, lookback_hours, max_records)


def _parse_articles(xml_text: str, lookback_hours: float, max_records: int) -> list[dict]:
    """
    Normalizes Google News RSS's <item> elements into a flat list of
    {"headline", "source_url", "theme_tags"} dicts -- CONFIRMED live
    2026-09-02 against a real response (both via curl and Python's
    `requests`). theme_tags is always [] here: Google News RSS doesn't
    provide a GKG-style tag concept the way GDELT nominally did (which
    itself was rarely populated in practice -- see score_relevance,
    which already falls back to title-only matching when theme_tags is
    empty, so this is a non-regression). Malformed XML or an unparseable
    pubDate is handled defensively (skip/keep, never raise) -- articles
    with no parseable pubDate are kept rather than dropped, since a
    missing date is more likely a parsing edge case than a signal the
    article is actually stale.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("Google News RSS response was not valid XML")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    parsed = []
    for item in root.findall(".//item"):
        if len(parsed) >= max_records:
            break

        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_raw = item.findtext("pubDate")
        pub_date = None
        if pub_date_raw:
            try:
                pub_date = parsedate_to_datetime(pub_date_raw)
            except (TypeError, ValueError):
                pub_date = None

        if pub_date is not None and pub_date < cutoff:
            continue

        parsed.append({"headline": title, "source_url": link, "theme_tags": []})
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


def is_relevant(keywords: list[str], article: dict, threshold: float) -> bool:
    """
    The actual "flag this article" decision -- requires BOTH the overlap
    ratio (score_relevance) to clear `threshold` AND the single most
    distinctive keyword (keywords[0] -- derive_keywords now ranks
    proper nouns first) to be present.

    CONFIRMED NECESSARY live 2026-09-02: using score_relevance's ratio
    alone flooded news_events almost every poll -- with only 4 derived
    keywords for a short title, a SINGLE generic-word hit ("sign" in
    "Patriots Sign TE...", "orders" in "Justice department orders...")
    already cleared threshold=0.2 (1/4=0.25) on totally unrelated
    articles. Gating on the most distinctive keyword specifically kept
    genuine matches (articles actually containing "mamdani") while
    rejecting matches on filler words alone.
    """
    if not keywords:
        return False
    haystack = " ".join([article.get("headline", "")] + list(article.get("theme_tags", []))).lower()
    if keywords[0] not in haystack:
        return False
    return score_relevance(keywords, article) >= threshold


def _market_keywords(rest_client: KalshiRestClient, ticker: str) -> list[str]:
    """
    Fetches market metadata via the existing GET-only rest_client.get_market
    and derives search keywords from its title/subtitle -- CONFIRMED live
    2026-09-02 (e.g. KXNFLSTADIUM-28SEP01EVERBRENO-Y -> title "Will
    Jacksonville complete the renovation of EverBank Stadium before Sep 1,
    2028?", subtitle "Before Sep 1, 2028").
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
    Periodically polls Google News RSS for each ticker in `states` and,
    for any article scoring at/above config.NEWS_RELEVANCE_THRESHOLD,
    logs it to the news_events table and registers a decaying
    news_signal on that ticker's RiskEngine (states[ticker].risk_engine).
    No-ops entirely if config.NEWS_FEED_ENABLED is False.
    """
    if not config.NEWS_FEED_ENABLED:
        logger.info("News feed disabled (config.NEWS_FEED_ENABLED = False) -- news_refresh_loop is a no-op.")
        return

    while True:
        for ticker, state in states.items():
            try:
                # _market_keywords/fetch_recent_articles make blocking
                # `requests` calls (to Kalshi and Google News RSS
                # respectively); run them off the event loop so they
                # don't freeze the WebSocket connection / scoring_loop
                # while waiting on a slow response (each ticker can
                # block up to its 10s connect timeout otherwise).
                keywords = await asyncio.to_thread(_market_keywords, rest_client, ticker)
                articles = await asyncio.to_thread(fetch_recent_articles, keywords, config.NEWS_LOOKBACK_HOURS)
                for article in articles:
                    score = score_relevance(keywords, article)
                    if is_relevant(keywords, article, config.NEWS_RELEVANCE_THRESHOLD):
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
