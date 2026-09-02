"""
Polls Kalshi's incentive_programs endpoint and ranks currently-active
Liquidity Incentive Program markets by an estimated "reward density" --
a rough proxy for how much reward you might capture per contract of
capital committed.

This is intentionally a simple heuristic in Phase 0. It exists to narrow
a large market universe down to a shortlist worth feeding into the
reward_simulator (which does the real, mechanically-accurate scoring
against live book data). We are NOT trying to perfectly predict payouts
here -- just avoid wasting simulation effort on dead or tiny programs.

Fields returned by GET /incentive_programs (per Kalshi's API docs):
  id, market_id, market_ticker, incentive_description, start_date,
  end_date, period_reward, paid_out, discount_factor_bps, target_size_fp
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

import config
from orderbook import dollars_to_cents, fp_to_size
from rest_client import KalshiRestClient

logger = logging.getLogger(__name__)


@dataclass
class ScreenedProgram:
    market_ticker: str
    period_reward: float
    target_size: float
    discount_factor_bps: int
    start_date: datetime
    end_date: datetime
    days_remaining: float
    reward_density_score: float  # higher = more attractive, arbitrary units
    raw: dict
    # Populated only when screen_incentive_programs(crowding_aware=True)
    # live-checks this candidate (see that function). None otherwise.
    crowding_ratio: float | None = None
    volatility_cents: float | None = None
    adjusted_score: float | None = None

    @property
    def total_period_days(self) -> float:
        """Full program period length, e.g. 14.0 for a 2-week program."""
        return (self.end_date - self.start_date).total_seconds() / 86400.0


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _reward_density(period_reward: float, target_size: float, days_remaining: float) -> float:
    """
    Rough heuristic: reward per day, scaled down by how much capital
    (Target Size) is required to even qualify. A market with a big pool
    but a huge Target Size requirement is less attractive per-dollar than
    a smaller pool with a modest Target Size.

    This does NOT account for how many other liquidity providers are
    already quoting there -- that's exactly what the reward_simulator's
    live-book pass is for. Treat this purely as a first-pass filter.
    """
    if days_remaining <= 0 or target_size <= 0:
        return 0.0
    reward_per_day = period_reward / days_remaining
    return reward_per_day / target_size


def _estimate_crowding(client: KalshiRestClient, ticker: str, target_size: float) -> float | None:
    """
    Real resting depth (both sides) relative to target_size, from a
    single live get_orderbook call -- same computation and CONFIRMED-live
    response shape as market_switcher._crowding_ratio_from_raw_orderbook
    (2026-09-02): {"orderbook_fp": {"yes_dollars": [...], "no_dollars": [...]}}.
    Duplicated locally (not imported from market_switcher.py) to avoid a
    circular import -- market_switcher already imports this module for
    ScreenedProgram/screen_incentive_programs. None on any failure or
    non-positive target_size, so a transient fetch failure simply doesn't
    adjust this candidate's score rather than penalizing it.
    """
    if target_size <= 0:
        return None
    try:
        raw = client.get_orderbook(ticker)
    except Exception:
        logger.warning("Failed to fetch orderbook for %s while estimating crowding", ticker, exc_info=True)
        return None
    book_fp = raw.get("orderbook_fp", {}) or {}
    yes_total = sum(fp_to_size(size) for _, size in (book_fp.get("yes_dollars") or []))
    no_total = sum(fp_to_size(size) for _, size in (book_fp.get("no_dollars") or []))
    return (yes_total + no_total) / (2.0 * target_size)


def _estimate_recent_volatility(client: KalshiRestClient, ticker: str, lookback_trades: int) -> float | None:
    """
    Coarse one-shot volatility proxy: stdev (in cents) of the last
    `lookback_trades` real trade prices, from a single live get_trades
    call. Intentionally crude (no time-weighting, no session context) --
    it exists only to avoid ranking a genuinely choppy market above a
    quiet one that has a similar reward-density score, using data we can
    get without ever having watched the market. None on any failure, or
    if fewer than 2 trades are returned (can't estimate a spread).

    Per-trade field names ("yes_price_dollars"/"count_fp") are already
    CONFIRMED live via a real REST /markets/trades response (see main.py's
    trade handler, 2026-08-31/09-01) -- but the top-level "trades"
    response-wrapper key below follows Kalshi's documented schema and has
    not itself been separately exercised live. Verify on your own machine
    before trusting this signal.
    """
    try:
        raw = client.get_trades(ticker, limit=lookback_trades)
    except Exception:
        logger.warning("Failed to fetch trades for %s while estimating volatility", ticker, exc_info=True)
        return None

    prices = []
    for t in raw.get("trades", []) or []:
        raw_price = t.get("yes_price_dollars", t.get("yes_price"))
        if raw_price is None:
            continue
        prices.append(dollars_to_cents(raw_price) if isinstance(raw_price, str) else raw_price)

    if len(prices) < 2:
        return None
    return statistics.pstdev(prices)


def _adjusted_score(
    reward_density_score: float,
    crowding_ratio: float | None,
    volatility_cents: float | None,
) -> float:
    """
    Dampens (never zeroes out) the reward-density heuristic for real
    crowding (already-resting depth diminishes a new entrant's marginal
    share) and recent volatility (choppier markets risk more adverse
    selection per dollar of reward). Missing data (None) is treated as
    neutral -- no adjustment -- rather than penalizing a candidate for a
    transient fetch failure.
    """
    score = reward_density_score
    if crowding_ratio is not None:
        score /= (1.0 + crowding_ratio)
    if volatility_cents is not None:
        score /= (1.0 + volatility_cents)
    return score


def screen_incentive_programs(
    client: KalshiRestClient,
    min_days_remaining: float = 0.5,
    top_n: int = 25,
    crowding_aware: bool = False,
    live_check_pool_size: int | None = None,
) -> list[ScreenedProgram]:
    """
    crowding_aware=True live-checks (one get_orderbook + one get_trades
    call each) the top `live_check_pool_size` reward-density-ranked
    candidates for real crowding/volatility, and re-ranks that pool by
    the resulting adjusted_score before truncating to top_n -- see
    _adjusted_score. Defaults to False (existing callers -- main.py's
    incentive_refresh_loop, build_market_states at startup, and
    market_switcher.find_replacement -- are unaffected and issue no extra
    REST calls). Intended to be turned on explicitly for the manual
    "python3 incentive_screener.py" market-picking workflow (README Step
    1), not the always-running pipeline.
    """
    now = datetime.now(timezone.utc)
    programs = client.get_all_incentive_programs(status="active", program_type="liquidity")

    screened: list[ScreenedProgram] = []
    for p in programs:
        try:
            start_date = _parse_iso(p["start_date"])
            end_date = _parse_iso(p["end_date"])
            days_remaining = (end_date - now).total_seconds() / 86400
            if days_remaining < min_days_remaining:
                continue

            target_size = float(p.get("target_size_fp", 0) or 0)
            # period_reward comes back as an integer in CENTS (confirmed via
            # a live raw response: period_reward=1350000 for a market whose
            # actual pool is $13,500 -- matches Kalshi's price-in-cents
            # convention used everywhere else in this API), not dollars.
            period_reward = float(p.get("period_reward", 0) or 0) / 100.0
            discount_factor_bps = int(p.get("discount_factor_bps", 0) or 0)

            score = _reward_density(period_reward, target_size, days_remaining)

            screened.append(
                ScreenedProgram(
                    market_ticker=p["market_ticker"],
                    period_reward=period_reward,
                    target_size=target_size,
                    discount_factor_bps=discount_factor_bps,
                    start_date=start_date,
                    end_date=end_date,
                    days_remaining=days_remaining,
                    reward_density_score=score,
                    raw=p,
                )
            )
        except (KeyError, ValueError) as e:
            logger.warning("Skipping malformed incentive program record: %s (%s)", p, e)

    screened.sort(key=lambda s: -s.reward_density_score)

    if not crowding_aware:
        return screened[:top_n]

    pool_size = live_check_pool_size if live_check_pool_size is not None else config.SCREENER_LIVE_CHECK_POOL_SIZE
    pool = screened[:pool_size]
    for program in pool:
        program.crowding_ratio = _estimate_crowding(client, program.market_ticker, program.target_size)
        program.volatility_cents = _estimate_recent_volatility(
            client, program.market_ticker, config.SCREENER_VOLATILITY_LOOKBACK_TRADES
        )
        program.adjusted_score = _adjusted_score(
            program.reward_density_score, program.crowding_ratio, program.volatility_cents
        )

    pool.sort(key=lambda s: -(s.adjusted_score if s.adjusted_score is not None else s.reward_density_score))
    return pool[:top_n]


def print_report(screened: list[ScreenedProgram]) -> None:
    crowding_aware = any(s.adjusted_score is not None for s in screened)
    if crowding_aware:
        print(f"{'Ticker':<28} {'Reward':>8} {'TgtSize':>9} {'DF(bps)':>8} {'DaysLeft':>9} "
              f"{'Density':>9} {'Crowd':>7} {'VolC':>6} {'Adj':>9}")
        print("-" * 100)
        for s in screened:
            crowd = f"{s.crowding_ratio:.2f}" if s.crowding_ratio is not None else "n/a"
            vol = f"{s.volatility_cents:.2f}" if s.volatility_cents is not None else "n/a"
            adj = f"{s.adjusted_score:.4f}" if s.adjusted_score is not None else "n/a"
            print(
                f"{s.market_ticker:<28} {s.period_reward:>8.2f} {s.target_size:>9.1f} "
                f"{s.discount_factor_bps:>8d} {s.days_remaining:>9.2f} "
                f"{s.reward_density_score:>9.4f} {crowd:>7} {vol:>6} {adj:>9}"
            )
        return

    print(f"{'Ticker':<28} {'Reward':>8} {'TgtSize':>9} {'DF(bps)':>8} {'DaysLeft':>9} {'Score':>10}")
    print("-" * 78)
    for s in screened:
        print(
            f"{s.market_ticker:<28} {s.period_reward:>8.2f} {s.target_size:>9.1f} "
            f"{s.discount_factor_bps:>8d} {s.days_remaining:>9.2f} {s.reward_density_score:>10.4f}"
        )


if __name__ == "__main__":
    from auth import KalshiSigner

    logging.basicConfig(level=logging.INFO)

    signer = None
    if config.KALSHI_API_KEY_ID and config.KALSHI_PRIVATE_KEY_PATH:
        signer = KalshiSigner()
    else:
        logger.warning("No credentials configured -- attempting unauthenticated read.")

    client = KalshiRestClient(signer=signer)
    # crowding_aware=True: this manual "pick markets to watch" workflow
    # (README Step 1) can afford the extra live get_orderbook/get_trades
    # calls that the always-running pipeline deliberately skips (see
    # screen_incentive_programs' docstring).
    results = screen_incentive_programs(client, crowding_aware=True)
    print_report(results)
