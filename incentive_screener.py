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
from dataclasses import dataclass
from datetime import datetime, timezone

from rest_client import KalshiRestClient

logger = logging.getLogger(__name__)


@dataclass
class ScreenedProgram:
    market_ticker: str
    period_reward: float
    target_size: float
    discount_factor_bps: int
    end_date: datetime
    days_remaining: float
    reward_density_score: float  # higher = more attractive, arbitrary units
    raw: dict


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


def screen_incentive_programs(
    client: KalshiRestClient,
    min_days_remaining: float = 0.5,
    top_n: int = 25,
) -> list[ScreenedProgram]:
    now = datetime.now(timezone.utc)
    programs = client.get_all_incentive_programs(status="active", program_type="liquidity")

    screened: list[ScreenedProgram] = []
    for p in programs:
        try:
            end_date = _parse_iso(p["end_date"])
            days_remaining = (end_date - now).total_seconds() / 86400
            if days_remaining < min_days_remaining:
                continue

            target_size = float(p.get("target_size_fp", 0) or 0)
            period_reward = float(p.get("period_reward", 0) or 0)
            discount_factor_bps = int(p.get("discount_factor_bps", 0) or 0)

            score = _reward_density(period_reward, target_size, days_remaining)

            screened.append(
                ScreenedProgram(
                    market_ticker=p["market_ticker"],
                    period_reward=period_reward,
                    target_size=target_size,
                    discount_factor_bps=discount_factor_bps,
                    end_date=end_date,
                    days_remaining=days_remaining,
                    reward_density_score=score,
                    raw=p,
                )
            )
        except (KeyError, ValueError) as e:
            logger.warning("Skipping malformed incentive program record: %s (%s)", p, e)

    screened.sort(key=lambda s: -s.reward_density_score)
    return screened[:top_n]


def print_report(screened: list[ScreenedProgram]) -> None:
    print(f"{'Ticker':<28} {'Reward':>8} {'TgtSize':>9} {'DF(bps)':>8} {'DaysLeft':>9} {'Score':>10}")
    print("-" * 78)
    for s in screened:
        print(
            f"{s.market_ticker:<28} {s.period_reward:>8.2f} {s.target_size:>9.1f} "
            f"{s.discount_factor_bps:>8d} {s.days_remaining:>9.2f} {s.reward_density_score:>10.4f}"
        )


if __name__ == "__main__":
    import config
    from auth import KalshiSigner

    logging.basicConfig(level=logging.INFO)

    signer = None
    if config.KALSHI_API_KEY_ID and config.KALSHI_PRIVATE_KEY_PATH:
        signer = KalshiSigner()
    else:
        logger.warning("No credentials configured -- attempting unauthenticated read.")

    client = KalshiRestClient(signer=signer)
    results = screen_incentive_programs(client)
    print_report(results)
