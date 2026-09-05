"""Token metadata (website / socials / image) from DexScreener.

LOGGED AND DISPLAYED ONLY -- deliberately not part of the score or the gate.

Measured on 81 pools with 20 days of outcomes, tokens with a website showed a
median best-multiple of 2477x vs 4.06x without. That is almost certainly
reverse causality: DexScreener token info is submitted (and paid for) by teams
AFTER a token gets traction, and the 100% hit rate on n=4 is the giveaway. It
is a marker of success, not a cause of it. Recording it at signal time is the
only way to find out whether it has real forward value.
"""
import logging
from typing import Dict

from . import config
from .http import get_json

log = logging.getLogger("hlbot.meta")

DS_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/%s"


def fetch(token: str) -> Dict:
    out = {"websites": [], "socials": [], "image": False,
           "boosts": 0, "symbol": None, "name": None}
    d = get_json(DS_TOKEN % token, retries=2)
    if not d:
        return out
    pairs = d.get("pairs") or []
    for p in pairs:
        if (p.get("baseToken") or {}).get("address", "").lower() != token.lower():
            continue
        out["symbol"] = p["baseToken"].get("symbol") or out["symbol"]
        out["name"] = p["baseToken"].get("name") or out["name"]
        info = p.get("info") or {}
        for w in (info.get("websites") or []):
            u = (w.get("url") or "").strip()
            if u and u not in out["websites"]:
                out["websites"].append(u)
        if info.get("imageUrl"):
            out["image"] = True
        for soc in (info.get("socials") or []):
            u = (soc.get("url") or "").strip()
            if u and u not in out["socials"]:
                out["socials"].append(u)
        out["boosts"] = max(out["boosts"], (p.get("boosts") or {}).get("active", 0))
    return out


_native = {"px": config.NATIVE_USD_FALLBACK, "at": 0.0}


def native_usd(max_age_s: float = 900.0) -> float:
    """Native-token/USD inferred from pools containing wrapped native. Cached."""
    import time
    if time.time() - _native["at"] < max_age_s:
        return _native["px"]
    wrapped = config.WRAPPED_NATIVE.lower()
    d = get_json(DS_TOKEN % wrapped, retries=2)
    vals = []
    for p in ((d or {}).get("pairs") or []):
        try:
            base = ((p.get("baseToken") or {}).get("address") or "").lower()
            quote = ((p.get("quoteToken") or {}).get("address") or "").lower()
            if base == wrapped:
                pu = float(p["priceUsd"])
                if pu > 0:
                    vals.append(pu)
            elif quote == wrapped:
                pn, pu = float(p["priceNative"]), float(p["priceUsd"])
                if pn > 0:
                    vals.append(pu / pn)
        except (KeyError, TypeError, ValueError):
            continue
    if vals:
        vals.sort()
        _native["px"] = vals[len(vals) // 2]
        _native["at"] = time.time()
        log.info("%s/USD = %.2f", config.NATIVE_SYMBOL, _native["px"])
    return _native["px"]


def summary(m: Dict) -> str:
    """Clickable, showing the destination rather than a generic label."""
    from . import tg
    bits = []
    seen = set()
    for u in (m.get("websites") or []) + (m.get("socials") or []):
        k = u.lower()
        if k in seen:
            continue
        a = tg.link(u)
        if a:
            seen.add(k)
            bits.append(a)
    if m.get("boosts"):
        bits.append("boosted x%d" % m["boosts"])
    return " | ".join(bits) if bits else "none"
