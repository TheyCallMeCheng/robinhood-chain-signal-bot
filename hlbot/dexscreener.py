"""DexScreener enrichment for on-chain-discovered V2/V3/V4 pools.

This uses a separate public quota from GeckoTerminal, so the Robinhood bot
cannot consume the quota used by the existing HyperEVM process.
"""
import time
from datetime import datetime, timezone
from typing import Dict, List

from . import config
from .http import get_json

BASE = "https://api.dexscreener.com"


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_pair(raw: dict) -> Dict:
    base = raw.get("baseToken") or {}
    quote = raw.get("quoteToken") or {}
    volume = raw.get("volume") or {}
    changes = raw.get("priceChange") or {}
    txns = raw.get("txns") or {}
    h1 = txns.get("h1") or {}
    created_ms = raw.get("pairCreatedAt")
    created_at = None
    age_hours = 0.0
    if created_ms:
        created = datetime.fromtimestamp(float(created_ms) / 1000.0, timezone.utc)
        created_at = created.isoformat().replace("+00:00", "Z")
        age_hours = max(0.0, (time.time() - created.timestamp()) / 3600.0)
    buys, sells = int(h1.get("buys") or 0), int(h1.get("sells") or 0)
    return {
        "address": (raw.get("pairAddress") or "").lower(),
        "name": "{} / {}".format(base.get("symbol") or "?",
                                    quote.get("symbol") or "?"),
        "dex": raw.get("dexId") or "",
        "base_token": (base.get("address") or "").lower(),
        "created_at": created_at,
        "age_hours": age_hours,
        "liq_usd": _f((raw.get("liquidity") or {}).get("usd")),
        "fdv_usd": _f(raw.get("fdv") or raw.get("marketCap")),
        "price_usd": _f(raw.get("priceUsd")),
        "vol_5m": _f(volume.get("m5")),
        "vol_1h": _f(volume.get("h1")),
        "vol_6h": _f(volume.get("h6")),
        "vol_24h": _f(volume.get("h24")),
        "chg_5m": _f(changes.get("m5")),
        "chg_15m": _f(changes.get("m5")),
        "chg_30m": _f(changes.get("h1")),
        "chg_1h": _f(changes.get("h1")),
        "chg_6h": _f(changes.get("h6")),
        "chg_24h": _f(changes.get("h24")),
        # DexScreener exposes transaction counts, not unique-wallet counts.
        "buys_1h": buys, "sells_1h": sells,
        "buyers_1h": buys, "sellers_1h": sells,
        "source": "dexscreener",
    }


def pairs_by_address(addresses: List[str]) -> List[Dict]:
    out = []
    unique = [address for address in dict.fromkeys(addresses) if address]
    for offset in range(0, len(unique), 30):
        ids = ",".join(unique[offset:offset + 30])
        payload = get_json(
            "{}/latest/dex/pairs/{}/{}".format(
                BASE, config.DEXSCREENER_CHAIN, ids), retries=2)
        out.extend(parse_pair(pair) for pair in ((payload or {}).get("pairs") or []))
    return out


def token_prices(addresses: List[str]) -> Dict[str, float]:
    """Return token/USD prices, selecting the deepest returned pair."""
    wanted = {address.lower() for address in addresses if address}
    if not wanted:
        return {}
    best = {}
    unique = list(wanted)
    for offset in range(0, len(unique), 30):
        ids = ",".join(unique[offset:offset + 30])
        payload = get_json("{}/tokens/v1/{}/{}".format(
            BASE, config.DEXSCREENER_CHAIN, ids), retries=2) or []
        for pair in payload:
            base = ((pair.get("baseToken") or {}).get("address") or "").lower()
            quote = ((pair.get("quoteToken") or {}).get("address") or "").lower()
            liquidity = _f((pair.get("liquidity") or {}).get("usd"))
            price = 0.0
            target = None
            if base in wanted:
                target, price = base, _f(pair.get("priceUsd"))
            elif quote in wanted:
                native = _f(pair.get("priceNative"))
                base_usd = _f(pair.get("priceUsd"))
                target = quote
                price = base_usd / native if native > 0 else 0.0
            if target and price > 0 and liquidity >= best.get(target, (0, 0))[0]:
                best[target] = (liquidity, price)
    return {token: value[1] for token, value in best.items()}
