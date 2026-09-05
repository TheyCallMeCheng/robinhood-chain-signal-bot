"""On-chain metrics for Pons v2 bonding curves."""
import logging
import time

from . import config, dexscreener, meta
from .chain import _rpc, erc20_name, erc20_symbol

log = logging.getLogger("hlbot.pons")

BUY_TOPIC = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
SELL_TOPIC = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
GET_RESERVES = "0x0902f1ac"
REAL_QUOTE_RESERVE = "0x4f1f58fd"
BUCKET_SECONDS = 60


def _uint(data, index):
    body = data[2:] if data.startswith("0x") else data
    return int(body[index * 64:(index + 1) * 64], 16)


def _batch_reserves(curves):
    from .http import post_json
    payload = []
    ids = {}
    request_id = 0
    for curve in curves:
        for kind, selector in (("reserves", GET_RESERVES),
                               ("real", REAL_QUOTE_RESERVE)):
            ids[request_id] = (curve, kind)
            payload.append({
                "jsonrpc": "2.0", "id": request_id, "method": "eth_call",
                "params": [{"to": curve, "data": selector}, "latest"],
            })
            request_id += 1
    if not payload:
        return {}
    response = post_json(config.RPC_URL, payload, retries=1)
    if not isinstance(response, list):
        raise RuntimeError("Pons reserve batch failed")
    raw = {}
    for item in response:
        curve_kind = ids.get(item.get("id"))
        result = item.get("result")
        if curve_kind and result and result != "0x":
            raw[curve_kind] = result
    out = {}
    for curve in curves:
        reserves = raw.get((curve, "reserves"))
        real = raw.get((curve, "real"))
        if reserves and real:
            out[curve] = (_uint(reserves, 0), _uint(reserves, 1),
                          _uint(real, 0))
    return out


def _trade_logs(start, end):
    result = _rpc("eth_getLogs", [{
        "fromBlock": hex(start), "toBlock": hex(end),
        "topics": [[BUY_TOPIC, SELL_TOPIC]],
    }])
    if result is None:
        raise RuntimeError("Pons trade log query failed")
    return result


def _sum(rows, key, since):
    return sum(row[key] for row in rows if row["ts"] >= since)


def _change(rows, current, cutoff):
    prior = [row["px"] for row in rows if row["ts"] <= cutoff and row["px"]]
    if not prior and len(rows) > 1:
        prior = [rows[0]["px"]]
    old = prior[-1] if prior else None
    return ((current / old - 1.0) * 100.0) if old and old > 0 else 0.0


def scan(store):
    """Scan global Pons trade topics and return metrics for active curves."""
    tip = _rpc("eth_blockNumber", [])
    if not tip:
        return {}
    tip = int(tip, 16) - config.CHAIN_CONFIRMATIONS
    # Never outrun discovery: a curve emitted in a newer block would not yet
    # be in `known`, and advancing past its first buys would lose them.
    discovery_tip = store.get_state("discovery:last_block")
    if discovery_tip is None:
        return {}
    tip = min(tip, int(discovery_tip))
    saved = store.get_state("pons:last_block")
    last = int(saved) if saved is not None else max(
        tip - config.PONS_INITIAL_LOOKBACK, 0)
    if tip <= last:
        return {}
    target = min(tip, last + config.CHAIN_MAX_REPLAY)

    known = store.discoveries_by_protocol("pons-v2", config.NEW_LAUNCH_MAX_AGE_H)
    aggregate = {}
    total = 0
    for start in range(last + 1, target + 1, config.CHAIN_LOG_CHUNK):
        end = min(start + config.CHAIN_LOG_CHUNK - 1, target)
        logs = _trade_logs(start, end)
        logs.sort(key=lambda row: (int(row["blockNumber"], 16),
                                   int(row.get("logIndex", "0x0"), 16)))
        for entry in logs:
            curve = entry["address"].lower()
            if curve not in known:
                continue
            is_buy = entry["topics"][0].lower() == BUY_TOPIC
            quote = _uint(entry["data"], 0 if is_buy else 1) / 1e18
            tokens = _uint(entry["data"], 1 if is_buy else 0) / 1e18
            trader = entry["topics"][1][-40:].lower()
            row = aggregate.setdefault(curve, {
                "vb": 0.0, "vs": 0.0, "nb": 0, "ns": 0,
                "buyers": set(), "sellers": set(), "maxbuy": 0.0,
                "streak": 0, "streakv": 0.0, "run": 0, "runv": 0.0,
                "execution_px": 0.0,
            })
            if is_buy:
                row["vb"] += quote
                row["nb"] += 1
                row["buyers"].add(trader)
                row["maxbuy"] = max(row["maxbuy"], quote)
                row["run"] += 1
                row["runv"] += quote
                row["streak"] = max(row["streak"], row["run"])
                row["streakv"] = max(row["streakv"], row["runv"])
            else:
                row["vs"] += quote
                row["ns"] += 1
                row["sellers"].add(trader)
                row["run"], row["runv"] = 0, 0.0
            if tokens > 0:
                row["execution_px"] = quote / tokens
            total += 1

    if len(aggregate) > config.PONS_MAX_RESERVE_CURVES:
        ranked = sorted(
            aggregate,
            key=lambda curve: aggregate[curve]["vb"] + aggregate[curve]["vs"],
            reverse=True,
        )
        aggregate = {curve: aggregate[curve]
                     for curve in ranked[:config.PONS_MAX_RESERVE_CURVES]}
        log.info("reserves: selected top %d active curves by trade volume",
                 len(aggregate))
    reserves = _batch_reserves(list(aggregate))
    now = int(time.time())
    bucket = (now // BUCKET_SECONDS) * BUCKET_SECONDS
    bucket_rows = []
    for curve, row in aggregate.items():
        reserve = reserves.get(curve)
        if not reserve or reserve[1] <= 0:
            continue
        quote_reserve, token_reserve, real_quote = reserve
        spot = quote_reserve / token_reserve
        bucket_rows.append((
            curve, bucket, row["vb"], row["vs"], row["nb"], row["ns"],
            len(row["buyers"]), len(row["sellers"]), row["maxbuy"],
            row["streak"], row["streakv"], spot, 2.0 * real_quote / 1e18,
        ))
    if bucket_rows:
        store.merge_buckets(bucket_rows)
    store.set_state("pons:last_block", target)
    if total:
        log.info("scanner: %d Pons trades, %d tracked active curves",
                 total, len(bucket_rows))

    if not bucket_rows:
        return {}
    custom_quotes = {
        known[curve]["token1"] for curve, *_ in bucket_rows
        if known[curve]["token1"] not in (config.ZERO_ADDRESS,
                                          config.WRAPPED_NATIVE)
    }
    quote_usd = dexscreener.token_prices(list(custom_quotes))
    native_usd = meta.native_usd()
    out = {}
    for curve, *_ in bucket_rows:
        discovery = known[curve]
        quote = discovery["token1"]
        usd = native_usd if quote in (config.ZERO_ADDRESS,
                                      config.WRAPPED_NATIVE) else quote_usd.get(quote, 0.0)
        if usd <= 0:
            continue
        rows = store.bucket_window(curve, now - 24 * 3600)
        if not rows:
            continue
        current = rows[-1]["px"]
        token = discovery["base_token"]
        identity = store.get_token(token)
        if not identity:
            store.put_token(token, erc20_symbol(token), erc20_name(token))
            identity = store.get_token(token) or {"symbol": "?", "name": "?"}
        quote_symbol = config.WRAPPED_NATIVE_SYMBOL
        if quote not in (config.ZERO_ADDRESS, config.WRAPPED_NATIVE):
            quote_identity = store.get_token(quote)
            if not quote_identity:
                store.put_token(quote, erc20_symbol(quote), erc20_name(quote))
                quote_identity = store.get_token(quote)
            quote_symbol = (quote_identity or {}).get("symbol") or "?"
        store.upsert_pool({
            "address": curve,
            "name": "{} / {}".format(identity.get("symbol") or "?", quote_symbol),
            "dex": "pons-v2", "base_token": token, "created_at": None,
        })
        if discovery.get("creator"):
            store.set_creator(curve, discovery["creator"])
        h1, h6, h24, m5 = now - 3600, now - 21600, now - 86400, now - 300
        vol1 = (_sum(rows, "vbuy", h1) + _sum(rows, "vsell", h1)) * usd
        buys = int(_sum(rows, "nb", h1)); sells = int(_sum(rows, "ns", h1))
        max_buy = max((row["maxbuy"] for row in rows if row["ts"] >= m5),
                      default=0.0) * usd
        streak = max((row["streak"] for row in rows if row["ts"] >= m5), default=0)
        streak_usd = max((row["streakv"] for row in rows if row["ts"] >= m5),
                         default=0.0) * usd
        liquidity = rows[-1]["liq"] * usd
        out[curve] = {
            "address": curve,
            "name": "{} / {}".format(identity.get("symbol") or "?", quote_symbol),
            "dex": "pons-v2", "base_token": token, "created_at": None,
            "age_hours": max(0.0, (time.time() - discovery["discovered_at"]) / 3600),
            "liq_usd": liquidity, "fdv_usd": 0.0,
            "price_usd": current * usd,
            "vol_5m": (_sum(rows, "vbuy", m5) + _sum(rows, "vsell", m5)) * usd,
            "vol_1h": vol1,
            "vol_6h": (_sum(rows, "vbuy", h6) + _sum(rows, "vsell", h6)) * usd,
            "vol_24h": (_sum(rows, "vbuy", h24) + _sum(rows, "vsell", h24)) * usd,
            "chg_5m": _change(rows, current, m5),
            "chg_15m": _change(rows, current, now - 900),
            "chg_30m": _change(rows, current, now - 1800),
            "chg_1h": _change(rows, current, h1),
            "chg_6h": _change(rows, current, h6),
            "chg_24h": _change(rows, current, h24),
            "buys_1h": buys, "sells_1h": sells,
            "buyers_1h": int(_sum(rows, "nbu", h1)),
            "sellers_1h": int(_sum(rows, "nse", h1)),
            "whale_pct": max_buy / liquidity * 100 if liquidity else 0.0,
            "whale_usd": max_buy, "buy_streak": streak,
            "streak_usd": streak_usd,
            "streak_pct": streak_usd / liquidity * 100 if liquidity else 0.0,
            "source": "chain",
        }
    return out
