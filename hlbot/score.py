"""Momentum scoring.

Signals are liquidity-normalised so a $13k pool and a $6M pool compare directly,
and are combined multiplicatively so trade "quality" can only amplify genuine
velocity, never manufacture it. An additive form let deep major pairs
(a deep blue-chip pair at 0.03 turnover/hr) rank top-5 on breadth alone.

Direction is a separate multiplier rather than another additive term. Volume
alone is not a signal for going up: in live testing an earlier volume-only
version scored a flat pool at 87.07 and a pool that had gone to -100% at 28.19.
"""
import math
from dataclasses import dataclass, asdict
from typing import Optional

from . import config


@dataclass
class Features:
    velocity: float      # vol_1h / liquidity
    zvol: float          # vol_1h vs the pool's own hourly baseline
    accel: float         # 5m run-rate vs the 1h average
    breadth: float       # unique buyers / buy txns
    imbalance: float     # (buyers - sellers) / (buyers + sellers)
    trend: float         # blended log price change, short windows
    direction: float     # sigmoid(trend); 0.5 flat, ->1 rising, ->0 falling
    thrust: float
    quality: float
    score: float

    def as_dict(self):
        return asdict(self)


def _num(x, default=0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _logret(pct) -> float:
    """Percent change -> log return, floored so -100% does not explode."""
    return math.log1p(max(_num(pct) / 100.0, -0.99))


def compute(liq_usd: float, vol_1h: float, vol_24h: float, vol_5m: float,
            buys_1h: int, buyers_1h: int, sellers_1h: int,
            age_hours: float,
            chg_5m: float = 0.0, chg_15m: float = 0.0,
            chg_1h: float = 0.0) -> Optional[Features]:
    """Return the feature vector, or None if the pool cannot be scored."""
    liq = _num(liq_usd)
    if liq <= 0:
        return None

    v1, v24, v5 = _num(vol_1h), _num(vol_24h), _num(vol_5m)

    velocity = v1 / liq

    # Divide by the hours actually observed. Using a flat 24 would hand every
    # pool younger than a day a zvol of exactly 24.0 -- pure arithmetic, not
    # signal. It inflated one test pool from 4.38 to 7.22.
    observed = max(min(age_hours, 24.0), 1.0)
    zvol = v1 / (v24 / observed) if v24 > 0 else 0.0

    accel = (v5 * 12.0) / v1 if v1 > 0 else 0.0

    breadth = (buyers_1h / buys_1h) if buys_1h > 0 else 0.0
    denom = buyers_1h + sellers_1h
    imbalance = ((buyers_1h - sellers_1h) / denom) if denom > 0 else 0.0

    thrust = (config.W_VELOCITY * math.log1p(max(velocity, 0.0))
              + config.W_ZVOL * math.log1p(max(zvol, 0.0))
              + config.W_ACCEL * math.log1p(max(accel, 0.0)))

    quality = ((config.Q_BREADTH_BASE + config.Q_BREADTH_GAIN * breadth)
               * (1.0 + config.Q_IMBALANCE_GAIN * imbalance))

    # Direction: weighted log-return across short windows, squashed to (0, 1).
    # Falling price drives this toward 0 and collapses the whole score.
    trend = (config.D_W_5M * _logret(chg_5m)
             + config.D_W_15M * _logret(chg_15m)
             + config.D_W_1H * _logret(chg_1h))
    direction = 1.0 / (1.0 + math.exp(-config.D_STEEPNESS * trend))

    # x2 so a clean uptrend (direction ~0.9) lands near the old scale.
    score = thrust * quality * direction * 2.0

    return Features(velocity, zvol, accel, breadth, imbalance,
                    trend, direction, thrust, quality, score)


def base_symbol(pool_name: str) -> str:
    """'TOKEN / WETH 0.01%' -> 'TOKEN'."""
    return (pool_name or "").split("/")[0].strip().upper()


def is_excluded(pool_name: str) -> bool:
    """Stablecoin arb pairs and blue chips are not shitcoin signals."""
    return base_symbol(pool_name) in config.EXCLUDE_BASE


def direction_ok(chg_1h: float, chg_5m: float) -> bool:
    """Hard directional gate. We only want things going up, right now."""
    c1, c5 = _num(chg_1h), _num(chg_5m)
    if c1 < config.MIN_CHG_1H:
        return False
    if c1 > config.MAX_CHG_1H:      # already vertical: late, not early
        return False
    if c5 < config.MAX_DUMP_5M:     # rolling over as we look at it
        return False
    return True


def passes_gate(liq_usd: float, vol_1h: float, pool_name: str = "",
                chg_1h: float = None, chg_5m: float = 0.0) -> bool:
    liq, vol = _num(liq_usd), _num(vol_1h)
    if liq < config.MIN_LIQ_USD or vol < config.MIN_VOL_1H_USD:
        return False
    if liq > 0 and (vol / liq) < config.MIN_VELOCITY:
        return False
    if is_excluded(pool_name):
        return False
    if chg_1h is not None and not direction_ok(chg_1h, chg_5m):
        return False
    return True
