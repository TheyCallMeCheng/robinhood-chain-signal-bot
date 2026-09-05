"""On-chain scanner: metrics for every tracked pool, straight from Swap logs.

Replaces GeckoTerminal as the primary metrics source. The reason is latency,
not cost: an old pool only reaches an indexer-driven feed once the indexer
itself calls it "trending", which by definition lags the move. CHAMELEON ran
for 24 minutes and 70% before its alert fired for exactly that reason.

`eth_getLogs` accepts an address array, so every tracked pool fits in one call
-- 351 pools over 2500 blocks measured at 0.46s. At a 60s poll that is ~60
blocks, so this is one cheap call per tick with no rate limit to fight.

It also exposes two things an indexer cannot: the largest single buy relative
to pool liquidity, and runs of consecutive buys with no sell between them.
Both are early-move tells that aggregate volume hides.
"""
import logging
import math
import time
from typing import Dict, List

from . import config
from .http import post_json

log = logging.getLogger("hlbot.scanner")

SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
BUCKET = 300
SEC_PER_BLOCK = 0.9837
MAX_ADDRS = 400


def _rpc(method, params, url=None):
    urls = [url] if url else config.SAFETY_RPCS
    for u in urls:
        r = post_json(u, {"jsonrpc": "2.0", "id": 1,
                          "method": method, "params": params}, retries=1)
        if r and "result" in r:
            return r["result"]
    return None


def _s256(h):
    v = int(h, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


def head() -> int:
    r = _rpc("eth_blockNumber", [])
    return int(r, 16) if r else None


def tvl_usd(liq, px_wrapped_per_token, wrapped_is_t0, native_usd) -> float:
    """V3 in-range liquidity -> USD. Assumes near-full-range LP, the norm for
    launchpad pools."""
    if not liq or not px_wrapped_per_token or px_wrapped_per_token <= 0:
        return 0.0
    P = (1.0 / px_wrapped_per_token) if wrapped_is_t0 else px_wrapped_per_token
    if P <= 0:
        return 0.0
    sp = math.sqrt(P)
    wrapped_native = (2.0 * liq / sp / 1e18) if wrapped_is_t0 else (2.0 * liq * sp / 1e18)
    return wrapped_native * native_usd


def scan(store, pool_map: Dict[str, bool], native_usd: float,
         max_replay_blocks: int = None) -> int:
    """Pull Swap logs since the last scan and fold them into 5m buckets."""
    tip = head()
    if tip is None:
        log.warning("scanner: no RPC head")
        return 0

    last = store.get_state("scan_block")
    last = int(last) if last else tip - 120
    if tip <= last:
        return 0
    # Cap catch-up so a long outage cannot stall a tick. A deliberate backfill
    # passes its own, larger, limit.
    cap = max_replay_blocks if max_replay_blocks else config.SCAN_MAX_REPLAY
    last = max(last, tip - cap)

    tip_blk = _rpc("eth_getBlockByNumber", [hex(tip), False])
    tip_ts = int(tip_blk["timestamp"], 16) if tip_blk else int(time.time())

    addrs = [a.lower() for a in pool_map][:MAX_ADDRS]
    if not addrs:
        return 0

    # per (pool, bucket) accumulator
    agg = {}
    # streak state per pool, ordered across the whole scanned range
    streak_n = {}
    streak_v = {}
    total = 0

    start = last + 1
    while start <= tip:
        end = min(start + config.SCAN_CHUNK - 1, tip)
        logs = _rpc("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end),
                                     "address": addrs, "topics": [SWAP_TOPIC]}])
        if logs is None:
            log.warning("scanner: getLogs failed %d-%d", start, end)
            start = end + 1
            continue
        logs.sort(key=lambda L: (int(L["blockNumber"], 16),
                                 int(L.get("logIndex", "0x0"), 16)))
        for lg in logs:
            pool = lg["address"].lower()
            # Token ordering decides buy vs sell. Guessing a default here would
            # invert the two for that pool, so an unknown pool is skipped.
            w0 = pool_map.get(pool)
            if w0 is None:
                continue
            d = lg["data"][2:]
            try:
                a0, a1 = _s256(d[0:64]), _s256(d[64:128])
                sq = int(d[128:192], 16)
                liq = int(d[192:256], 16)
            except ValueError:
                continue
            blk = int(lg["blockNumber"], 16)
            ts = tip_ts - int((tip - blk) * SEC_PER_BLOCK)
            bkt = (ts // BUCKET) * BUCKET

            wamt = a0 if w0 else a1
            vol = abs(wamt) / 1e18
            p = (sq / (1 << 96)) ** 2
            if p <= 0:
                continue
            px = (1.0 / p) if w0 else p
            who = "0x" + lg["topics"][2][-40:]

            k = (pool, bkt)
            a = agg.get(k)
            if a is None:
                a = agg[k] = {"vb": 0.0, "vs": 0.0, "nb": 0, "ns": 0,
                              "buyers": set(), "sellers": set(), "maxbuy": 0.0,
                              "streak": 0, "streakv": 0.0, "px": px, "liq": liq}
            if wamt > 0:
                a["vb"] += vol
                a["nb"] += 1
                a["buyers"].add(who)
                a["maxbuy"] = max(a["maxbuy"], vol)
                streak_n[pool] = streak_n.get(pool, 0) + 1
                streak_v[pool] = streak_v.get(pool, 0.0) + vol
                a["streak"] = max(a["streak"], streak_n[pool])
                a["streakv"] = max(a["streakv"], streak_v[pool])
            else:
                a["vs"] += vol
                a["ns"] += 1
                a["sellers"].add(who)
                streak_n[pool] = 0
                streak_v[pool] = 0.0
            a["px"] = px
            a["liq"] = liq
            total += 1
        start = end + 1

    if agg:
        # liquidity is a uint128 and overflows SQLite's 64-bit INTEGER, so it
        # is stored as REAL; precision loss there is irrelevant for a TVL proxy.
        rows = [(pool, bkt, a["vb"], a["vs"], a["nb"], a["ns"],
                 len(a["buyers"]), len(a["sellers"]), a["maxbuy"],
                 a["streak"], a["streakv"], float(a["px"]), float(a["liq"]))
                for (pool, bkt), a in agg.items()]
        store.merge_buckets(rows)

    store.set_state("scan_block", tip)
    if total:
        log.info("scanner: %d swaps across %d pools (%d blocks)",
                 total, len({p for p, _ in agg}), tip - last)
    return total


def backfill(store, pool_map: Dict[str, bool], native_usd: float, hours: int = 24):
    """Seed history so the first ticks have real 1h/24h context.

    Without this a fresh process reports chg_1h of 0 and a meaningless zvol
    until it has been running for a day.
    """
    tip = head()
    if tip is None:
        return 0
    have = store.get_state("scan_block")
    if have and int(have) > tip - int(1800 / SEC_PER_BLOCK):
        return 0                     # already current
    start = tip - int(hours * 3600 / SEC_PER_BLOCK)
    store.set_state("scan_block", start)
    old_chunk = config.SCAN_CHUNK
    try:
        config.SCAN_CHUNK = 2500     # bigger windows are fine for a bulk pass
        n = scan(store, pool_map, native_usd,
                 max_replay_blocks=int(hours * 3600 / SEC_PER_BLOCK) + 1000)
    finally:
        config.SCAN_CHUNK = old_chunk
    log.info("scanner backfill: %d swaps over %dh", n, hours)
    return n


def _sum(rows, key, since):
    return sum(r[key] for r in rows if r["ts"] >= since)


def build_metrics(pool: str, rows: List[Dict], native_usd: float,
                  wrapped_is_t0: bool, now: int = None) -> Dict:
    """Turn a pool's bucket history into the same shape the engine scores."""
    if not rows:
        return None
    now = now or int(time.time())
    last = rows[-1]
    px = last["px"]
    liq_usd = tvl_usd(last["liq"], px, wrapped_is_t0, native_usd)
    if not px or liq_usd <= 0:
        return None

    h1 = now - 3600
    h6 = now - 6 * 3600
    h24 = now - 24 * 3600
    m5 = now - 300

    vol_1h = (_sum(rows, "vbuy", h1) + _sum(rows, "vsell", h1)) * native_usd
    vol_6h = (_sum(rows, "vbuy", h6) + _sum(rows, "vsell", h6)) * native_usd
    vol_24h = (_sum(rows, "vbuy", h24) + _sum(rows, "vsell", h24)) * native_usd
    vol_5m = (_sum(rows, "vbuy", m5) + _sum(rows, "vsell", m5)) * native_usd

    oldest = rows[0]["ts"]

    def chg(cut):
        """None when we have no history that far back -- reporting 0.0% would
        read as 'flat' when it actually means 'unknown'."""
        if cut < oldest:
            return None
        prior = [r for r in rows if r["ts"] <= cut and r["px"]]
        if not prior:
            return None
        old = prior[-1]["px"]
        return ((px / old - 1) * 100) if old and old > 0 else None

    buys_1h = int(_sum(rows, "nb", h1))
    sells_1h = int(_sum(rows, "ns", h1))
    buyers_1h = int(_sum(rows, "nbu", h1))
    sellers_1h = int(_sum(rows, "nse", h1))

    recent = [r for r in rows if r["ts"] >= now - config.SURGE_WINDOW_S]
    max_buy = max([r["maxbuy"] for r in recent], default=0.0) * native_usd
    streak = max([r["streak"] for r in recent], default=0)
    streak_usd = max([r["streakv"] for r in recent], default=0.0) * native_usd

    span_h = max((rows[-1]["ts"] - rows[0]["ts"]) / 3600.0, 0.083)

    return {
        "address": pool,
        "name": "",
        "dex": config.V3_DEX_NAME,
        "base_token": "",
        "created_at": None,
        "age_hours": span_h,
        "liq_usd": liq_usd,
        "fdv_usd": 0.0,
        "price_usd": px * native_usd,
        "vol_5m": vol_5m,
        "vol_1h": vol_1h,
        "vol_6h": vol_6h,
        "vol_24h": vol_24h,
        # 5m/15m/1h feed the gates, so default those to 0 rather than None;
        # 6h/24h are display only and stay None when unknown.
        "chg_5m": chg(now - 300) or 0.0,
        "chg_15m": chg(now - 900) or 0.0,
        "chg_1h": chg(now - 3600) or 0.0,
        "chg_6h": chg(now - 6 * 3600),
        "chg_24h": chg(now - 24 * 3600),
        "buys_1h": buys_1h,
        "sells_1h": sells_1h,
        "buyers_1h": buyers_1h,
        "sellers_1h": sellers_1h,
        # early-move tells, unavailable from an aggregate feed
        "whale_pct": (max_buy / liq_usd * 100.0) if liq_usd else 0.0,
        "whale_usd": max_buy,
        "buy_streak": streak,
        "streak_usd": streak_usd,
        "streak_pct": (streak_usd / liq_usd * 100.0) if liq_usd else 0.0,
        "source": "chain",
    }
