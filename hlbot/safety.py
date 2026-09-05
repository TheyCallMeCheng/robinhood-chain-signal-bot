"""Pre-alert safety checks.

These do not try to predict price. They remove positions that are structurally
untradeable or structurally unsafe, which the backtest could not do and which
needs no statistical validation to justify.

  can_sell        simulated sell through the router, via eth_call state override
  tax             round-trip loss at negligible size = pool fee + transfer tax
  price_impact    loss at your actual size, minus the above
  supply          how much of the float sits in the pool vs elsewhere / burned
  lp_pulls        recent liquidity removals on the pool
  creator         how the deployer's previous launches turned out
"""
import logging
from typing import Dict, Optional

from . import config
from .http import post_json
from .keccak import keccak256

log = logging.getLogger("hlbot.safety")

ROUTER = config.ROUTER
WRAPPED_NATIVE = config.WRAPPED_NATIVE
DEAD = "0x000000000000000000000000000000000000dead"
ZERO = "0x0000000000000000000000000000000000000000"
# arbitrary address with no history; only ever used inside eth_call
PROBE = "0x00000000000000000000000000000000000f1e2d"

TEST_BAL = 10 ** 30
MAX_SLOT = 12

# exactInputSingle: SwapRouter (with deadline) then SwapRouter02 (without)
SEL_SWAP_DEADLINE = "0x414bf389"
SEL_SWAP_NO_DEADLINE = "0x04e45aaf"
BURN_TOPIC = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"


_rpc_idx = [0]


def _rpc(method, params):
    """Rotate endpoints on failure; a provider that has burned its quota
    answers every method with -32601 rather than failing the connection."""
    n = len(config.SAFETY_RPCS)
    for off in range(n):
        url = config.SAFETY_RPCS[(_rpc_idx[0] + off) % n]
        r = post_json(url, {"jsonrpc": "2.0", "id": 1,
                            "method": method, "params": params}, retries=1)
        if r and "result" in r:
            _rpc_idx[0] = (_rpc_idx[0] + off) % n
            return r["result"]
    return None


def _pad(x) -> str:
    if isinstance(x, str):
        return x.lower().replace("0x", "").rjust(64, "0")
    return format(x, "064x")


def _call(to, data, overrides=None, frm=None):
    tx = {"to": to, "data": data}
    if frm:
        tx["from"] = frm
    params = [tx, "latest"]
    if overrides:
        params.append(overrides)
    r = post_json(config.SAFETY_RPC, {"jsonrpc": "2.0", "id": 1,
                                      "method": "eth_call", "params": params})
    if not r:
        return None, "no response"
    if "error" in r:
        return None, str(r["error"].get("message"))[:80]
    return r.get("result"), None


def _uint(hexstr) -> Optional[int]:
    if not hexstr or hexstr == "0x":
        return None
    try:
        return int(hexstr, 16)
    except ValueError:
        return None


def erc20_uint(token, selector, arg=None):
    data = selector + (_pad(arg) if arg else "")
    r, _ = _call(token, data)
    return _uint(r)


def total_supply(token):
    return erc20_uint(token, "0x18160ddd")


def balance_of(token, who):
    return erc20_uint(token, "0x70a08231", who)


def _slot_key(addr, slot, vyper=False):
    a = bytes.fromhex(_pad(addr))
    s = bytes.fromhex(_pad(slot))
    return "0x" + keccak256((s + a) if vyper else (a + s)).hex()


def find_balance_slot(token) -> Optional[tuple]:
    """Locate the balances mapping slot by probing overrides."""
    for vyper in (False, True):
        for slot in range(MAX_SLOT):
            key = _slot_key(PROBE, slot, vyper)
            ov = {token: {"stateDiff": {key: "0x" + _pad(TEST_BAL)}}}
            r, err = _call(token, "0x70a08231" + _pad(PROBE), ov)
            if _uint(r) == TEST_BAL:
                return (slot, vyper)
    return None


def find_allowance_slot(token) -> Optional[tuple]:
    """allowance mapping: keccak(spender . keccak(owner . slot))."""
    for vyper in (False, True):
        for slot in range(MAX_SLOT):
            inner = keccak256(bytes.fromhex(_pad(slot) + _pad(PROBE)) if vyper
                              else bytes.fromhex(_pad(PROBE) + _pad(slot)))
            outer = keccak256(bytes.fromhex(_pad(ROUTER)) + inner) if not vyper \
                else keccak256(inner + bytes.fromhex(_pad(ROUTER)))
            key = "0x" + outer.hex()
            ov = {token: {"stateDiff": {key: "0x" + _pad(TEST_BAL)}}}
            data = "0xdd62ed3e" + _pad(PROBE) + _pad(ROUTER)
            r, _ = _call(token, data, ov)
            if _uint(r) == TEST_BAL:
                return (slot, vyper)
    return None


def _swap_calldata(token_in, token_out, fee, recipient, amount_in, with_deadline):
    body = (_pad(token_in) + _pad(token_out) + _pad(fee) + _pad(recipient))
    if with_deadline:
        body += _pad(2 ** 32 - 1)
    body += _pad(amount_in) + _pad(0) + _pad(0)
    return (SEL_SWAP_DEADLINE if with_deadline else SEL_SWAP_NO_DEADLINE) + body


def _allowance_key(owner, aslot, avy):
    inner = keccak256(bytes.fromhex(_pad(aslot) + _pad(owner)) if avy
                      else bytes.fromhex(_pad(owner) + _pad(aslot)))
    return "0x" + (keccak256(bytes.fromhex(_pad(ROUTER)) + inner) if not avy
                   else keccak256(inner + bytes.fromhex(_pad(ROUTER)))).hex()


def simulate_sell(token, fee, amount_in, bal_slot, allow_slot,
                  seller=None, seller_has_balance=False) -> tuple:
    """Return (amount_out, error).

    When `seller` already holds the token we override only the allowance, which
    keeps the simulation faithful for templates that gate on purchase history.
    """
    aslot, avy = allow_slot
    who = seller or PROBE
    diff = {_allowance_key(who, aslot, avy): "0x" + _pad(2 ** 256 - 1)}
    if not seller_has_balance:
        bslot, bvy = bal_slot
        diff[_slot_key(who, bslot, bvy)] = "0x" + _pad(amount_in * 2)

    ov = {token: {"stateDiff": diff},
          who: {"balance": "0x56bc75e2d63100000"}}
    last = None
    for with_deadline in (True, False):
        data = _swap_calldata(token, WRAPPED_NATIVE, fee, who, amount_in, with_deadline)
        r, err = _call(ROUTER, data, ov, frm=who)
        out = _uint(r)
        if out is not None:
            return out, None
        last = err
    return None, last


def spot_price_wrapped(pool, token, wrapped_is_t0=None) -> Optional[float]:
    """WRAPPED_NATIVE per token, from slot0.

    The raw balance ratio is NOT the price in a V3 pool -- liquidity is
    concentrated, so reserves say nothing about where the tick sits.
    """
    r, _ = _call(pool, "0x3850c7bd")          # slot0()
    if not r or len(r) < 66:
        return None
    sqrt_px = _uint("0x" + r[2:66])
    if not sqrt_px:
        return None
    p = (sqrt_px / float(1 << 96)) ** 2       # token1 per token0
    if p <= 0:
        return None
    if wrapped_is_t0 is None:
        wrapped_is_t0 = WRAPPED_NATIVE.lower() < token.lower()
    return (1.0 / p) if wrapped_is_t0 else p


def pool_fee(pool) -> Optional[int]:
    r, _ = _call(pool, "0xddca3f43")      # fee()
    return _uint(r)


SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"


def _s256(h):
    v = int(h, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


def swap_stats(pool, token, lookback=9000) -> Dict:
    """Observed trading, which beats simulation as evidence.

    Simulation can only test one router path; real sells may route through
    aggregators. If distinct wallets are demonstrably selling on chain, the
    token is sellable regardless of what our simulated path does.
    """
    out = {"buys": 0, "sells": 0, "sellers": set(), "buy_txs": []}
    tip = _rpc("eth_blockNumber", [])
    if not tip:
        return out
    tip = int(tip, 16)
    wrapped_t0 = WRAPPED_NATIVE.lower() < token.lower()
    b = max(tip - lookback, 0)
    while b <= tip:
        e = min(b + 9999, tip)
        logs = _rpc("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(e),
                                     "address": pool, "topics": [SWAP_TOPIC]}])
        for lg in (logs or []):
            try:
                d = lg["data"][2:]
                a0, a1 = _s256(d[0:64]), _s256(d[64:128])
                wamt = a0 if wrapped_t0 else a1
            except (KeyError, IndexError, ValueError):
                continue
            if wamt > 0:
                out["buys"] += 1
                if len(out["buy_txs"]) < 40:
                    out["buy_txs"].append((lg["transactionHash"],
                                           "0x" + lg["topics"][2][-40:]))
            else:
                out["sells"] += 1
                out["sellers"].add("0x" + lg["topics"][2][-40:])
        b = e + 1
    return out


def recent_buyer(pool, token, lookback=9000):
    """Find a wallet that actually bought recently, and still holds.

    A synthetic probe address is not a faithful stand-in: several launchpad
    templates block transfers from wallets that never bought (anti-bot), which
    makes a perfectly tradeable token look like a honeypot. Simulating from a
    real holder avoids that entirely.
    """
    tip = _rpc("eth_blockNumber", [])
    if not tip:
        return None
    tip = int(tip, 16)
    wrapped_t0 = WRAPPED_NATIVE.lower() < token.lower()
    cands = []
    b = max(tip - lookback, 0)
    while b <= tip and len(cands) < 40:
        e = min(b + 9999, tip)
        logs = _rpc("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(e),
                                     "address": pool, "topics": [SWAP_TOPIC]}])
        for lg in (logs or []):
            try:
                d = lg["data"][2:]
                a0, a1 = _s256(d[0:64]), _s256(d[64:128])
                wamt = a0 if wrapped_t0 else a1
                if wamt > 0:                      # WRAPPED_NATIVE in => a buy
                    # `recipient` is frequently the router or an aggregator,
                    # so take the transaction sender: that is a real wallet.
                    cands.append((lg["transactionHash"],
                                  "0x" + lg["topics"][2][-40:]))
            except (KeyError, IndexError, ValueError):
                continue
        b = e + 1
    seen = set()
    tried = 0
    for tx_hash, recipient in reversed(cands):    # most recent first
        if tried >= 8:
            break
        for who in (recipient, None):
            if who is None:
                tx = _rpc("eth_getTransactionByHash", [tx_hash])
                who = (tx or {}).get("from")
            if not who:
                continue
            w = who.lower()
            if w == ROUTER.lower() or w in seen:
                continue
            seen.add(w)
            tried += 1
            bal = balance_of(token, who)
            if bal and bal > 0:
                return (who, bal)
    return None


def recent_lp_pulls(pool, blocks=20000) -> Optional[int]:
    tip = _rpc("eth_blockNumber", [])
    if not tip:
        return None
    tip = int(tip, 16)
    n = 0
    start = max(tip - blocks, 0)
    b = start
    while b <= tip:
        e = min(b + 999, tip)
        logs = _rpc("eth_getLogs", [{"fromBlock": hex(b), "toBlock": hex(e),
                                     "address": pool, "topics": [BURN_TOPIC]}])
        if logs is None:
            return None
        n += len(logs)
        b = e + 1
    return n


def check(token: str, pool: str, fee: int, position_usd: float,
          native_usd: float, deep: bool = True) -> Dict:
    """Run the safety suite. Never raises; unknown fields come back as None."""
    out = {"token": token, "pool": pool, "fee": fee, "can_sell": None,
           "tax_pct": None, "impact_pct": None, "supply_in_pool": None,
           "burned_pct": None, "lp_pulls": None, "seller": None,
           "buys_seen": None, "sells_seen": None, "distinct_sellers": None,
           "notes": []}

    ts = total_supply(token)
    pool_bal = balance_of(token, pool)
    if ts and pool_bal is not None:
        out["supply_in_pool"] = pool_bal / ts * 100.0
    dead = balance_of(token, DEAD) or 0
    zero = balance_of(token, ZERO) or 0
    if ts:
        out["burned_pct"] = (dead + zero) / ts * 100.0

    if not deep:
        return out

    px = spot_price_wrapped(pool, token, None)
    if not px or px <= 0:
        out["notes"].append("no spot price")
        return out

    bal_slot = find_balance_slot(token)
    if not bal_slot:
        out["notes"].append("balance slot not found (proxy or unusual layout)")
        return out
    allow_slot = find_allowance_slot(token)
    if not allow_slot:
        out["notes"].append("allowance slot not found")
        return out

    stats = swap_stats(pool, token)
    out["buys_seen"] = stats["buys"]
    out["sells_seen"] = stats["sells"]
    out["distinct_sellers"] = len(stats["sellers"])

    holder = recent_buyer(pool, token)
    seller = holder[0] if holder else None
    seller_bal = holder[1] if holder else 0
    out["seller"] = seller or "synthetic probe"

    dec = erc20_uint(token, "0x313ce567")
    if dec is None or dec > 36:
        dec = 18
    scale = 10 ** dec

    def expected_wrapped_wei(tokens_wei):
        """px is WRAPPED_NATIVE per whole token; WRAPPED_NATIVE is always 18 decimals."""
        return (tokens_wei / scale) * px * 1e18

    # Negligible size, but not so small the output rounds to zero -- that
    # reads back as a 100% tax. Floor it at ~0.002 WRAPPED_NATIVE of expected output.
    min_meaningful = int((0.002 / px) * scale) if px > 0 else 0
    tiny_wei = max(int((pool_bal or 0) * 1e-5), min_meaningful, 1)
    if seller and seller_bal:
        if seller_bal < min_meaningful:
            seller, seller_bal = None, 0        # holder too small; use the probe
            out["seller"] = "synthetic probe"
        else:
            tiny_wei = min(tiny_wei, max(int(seller_bal * 0.2), min_meaningful))
    out_tiny, err = simulate_sell(token, fee, tiny_wei, bal_slot, allow_slot,
                                  seller, bool(seller and seller_bal >= tiny_wei))

    if out_tiny is None:
        # Observed trading outranks a failed simulation: we can only simulate
        # one router path, and several launchpad templates permit selling only
        # through their own router. Many real sells means it is sellable.
        if out["sells_seen"] >= 5:
            out["can_sell"] = True
            out["notes"].append("%d sells seen on chain (sim path blocked)"
                                % out["sells_seen"])
        elif out["buys_seen"] >= 20 and out["sells_seen"] == 0:
            out["can_sell"] = False
            out["notes"].append("HONEYPOT: %d buys, zero sells" % out["buys_seen"])
        else:
            out["can_sell"] = None
            out["notes"].append("undetermined: sim reverted, too little history")
        return out

    out["can_sell"] = True
    exp_tiny = expected_wrapped_wei(tiny_wei)
    if exp_tiny > 1e12:                          # only trust a non-dust quote
        out["tax_pct"] = max(0.0, (1 - out_tiny / exp_tiny) * 100.0)
    else:
        out["notes"].append("tax undetermined (test size too small)")

    tokens_human = (position_usd / native_usd) / px
    size_wei = int(tokens_human * scale)
    if size_wei > 0:
        out_real, err2 = simulate_sell(token, fee, size_wei, bal_slot, allow_slot,
                                       seller,
                                       bool(seller and seller_bal >= size_wei))
        if out_real is not None:
            exp_real = expected_wrapped_wei(size_wei)
            total_loss = (1 - out_real / exp_real) * 100.0
            out["impact_pct"] = max(0.0, total_loss - (out["tax_pct"] or 0.0))
        else:
            out["notes"].append("sell at size reverted: %s" % err2)

    return out


def verdict(s: Dict) -> tuple:
    """(ok, reasons). Blocks only on positive evidence of a problem.

    `can_sell is None` means undetermined and is NOT a block -- suppressing
    everything we cannot simulate would silence most of the feed.
    """
    reasons = []
    if s.get("can_sell") is False:
        reasons.append("CANNOT SELL")
    if (s.get("tax_pct") or 0) > config.MAX_SELL_TAX_PCT:
        reasons.append("sell tax %.1f%%" % s["tax_pct"])
    if (s.get("impact_pct") or 0) > config.MAX_IMPACT_PCT:
        reasons.append("impact %.1f%% at $%.0f" % (s["impact_pct"], config.POSITION_USD))
    b, sl = s.get("buys_seen") or 0, s.get("sells_seen") or 0
    if b >= 30 and sl == 0:
        reasons.append("no sells in %d buys" % b)
    return (not reasons), reasons
