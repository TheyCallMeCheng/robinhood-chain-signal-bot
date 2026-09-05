"""GeckoTerminal client.

Chosen over raw log indexing for enrichment because it returns unique
`buyers`/`sellers` counts alongside `buys`/`sells` -- the cheapest available
wash-trading discriminator, and expensive to derive from logs yourself.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List

from . import config
from .http import get_json

log = logging.getLogger("hlbot.gecko")

_last_call = [0.0]


def _throttle():
    """Free tier tolerates ~30 req/min; hold a floor between calls."""
    gap = time.time() - _last_call[0]
    if gap < config.GT_MIN_INTERVAL:
        time.sleep(config.GT_MIN_INTERVAL - gap)
    _last_call[0] = time.time()


def _get(path: str):
    _throttle()
    return get_json(f"{config.GT_BASE}/networks/{config.NETWORK}/{path}",
                    retries=config.GT_RETRIES)


def _f(x, default=0.0) -> float:
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return default


def parse_pool(raw: dict) -> Dict:
    """Flatten a GeckoTerminal pool object into the shape the engine uses."""
    a = raw.get("attributes", {}) or {}
    rel = raw.get("relationships", {}) or {}
    vol = a.get("volume_usd") or {}
    pc = a.get("price_change_percentage") or {}
    tx = a.get("transactions") or {}
    h1 = tx.get("h1") or {}

    created_raw = a.get("pool_created_at")
    created = None
    age_h = 0.0
    if created_raw:
        try:
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600.0
        except ValueError:
            pass

    dex = ((rel.get("dex") or {}).get("data") or {}).get("id", "")
    base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
    base_addr = base_id.split("_", 1)[1] if "_" in base_id else ""

    return {
        "address": a.get("address", ""),
        "name": a.get("name", ""),
        "dex": dex,
        "base_token": base_addr,
        "created_at": created_raw,
        "age_hours": age_h,
        "liq_usd": _f(a.get("reserve_in_usd")),
        "fdv_usd": _f(a.get("fdv_usd")),
        "price_usd": _f(a.get("base_token_price_usd")),
        "vol_5m": _f(vol.get("m5")),
        "vol_1h": _f(vol.get("h1")),
        "vol_6h": _f(vol.get("h6")),
        "vol_24h": _f(vol.get("h24")),
        "chg_5m": _f(pc.get("m5")),
        "chg_15m": _f(pc.get("m15")),
        "chg_30m": _f(pc.get("m30")),
        "chg_1h": _f(pc.get("h1")),
        "chg_6h": _f(pc.get("h6")),
        "chg_24h": _f(pc.get("h24")),
        "buys_1h": int(h1.get("buys") or 0),
        "sells_1h": int(h1.get("sells") or 0),
        "buyers_1h": int(h1.get("buyers") or 0),
        "sellers_1h": int(h1.get("sellers") or 0),
    }


def _collect(payload) -> List[Dict]:
    if not payload or "data" not in payload:
        return []
    data = payload["data"]
    if isinstance(data, dict):
        data = [data]
    return [parse_pool(x) for x in data if isinstance(x, dict)]


def new_pools(pages: int = None) -> List[Dict]:
    pages = pages or config.NEW_POOL_PAGES
    out = []
    for p in range(1, pages + 1):
        out.extend(_collect(_get(f"new_pools?page={p}")))
    return out


def trending_pools() -> List[Dict]:
    return _collect(_get("trending_pools"))


def top_pools() -> List[Dict]:
    return _collect(_get("pools?page=1"))


def pools_by_address(addresses: List[str]) -> List[Dict]:
    """The /multi endpoint takes up to 30 addresses and silently drops unknowns."""
    out = []
    uniq = [a for a in dict.fromkeys(addresses) if a]
    for i in range(0, len(uniq), 30):
        chunk = ",".join(uniq[i:i + 30])
        out.extend(_collect(_get(f"pools/multi/{chunk}")))
    return out
