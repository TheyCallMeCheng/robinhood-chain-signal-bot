"""Launchpad watcher.

One event on the launchpad factory carries everything we want about a launch:

    topic0  event id
    topic1  token address
    topic2  pool address
    topic3  creator
    data    ABI-encoded strings including an ipfs:// metadata URI

That is strictly more than PoolCreated gives us -- the creator arrives without a
separate transaction lookup, and the metadata URI is fixed at deploy time, so
unlike DexScreener token info it cannot be back-filled after a pump.

Measured at ~13 launches/hour, which matches the whole HyperSwap V3 new-pool
rate: this single launchpad is effectively the entire memecoin firehose.
"""
import json
import logging
import re
import time
from typing import Dict, List, Optional

from . import config
from .http import get_bytes, post_json

log = logging.getLogger("hlbot.launchpad")

CID_RE = re.compile(r"ipfs://([A-Za-z0-9]{40,70})")
MAX_RANGE = 1000


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


def _ascii(data_hex: str) -> str:
    try:
        return bytes.fromhex(data_hex[2:]).decode("ascii", "ignore")
    except ValueError:
        return ""


def head() -> Optional[int]:
    r = _rpc("eth_blockNumber", [])
    return int(r, 16) if r else None


def poll_launches(from_block: int, to_block: int) -> List[Dict]:
    # This endpoint is a different provider from the one that produced the tip,
    # and it trails by ~60 blocks. Asking it for a block it has not seen yet
    # returns "Unknown block" and loses the whole window.
    h = head()
    if h is not None:
        to_block = min(to_block, h)
    if from_block > to_block:
        return []
    out = []
    start = from_block
    while start <= to_block:
        end = min(start + MAX_RANGE - 1, to_block)
        logs = _rpc("eth_getLogs", [{
            "fromBlock": hex(start), "toBlock": hex(end),
            "address": config.LAUNCHPAD,
            "topics": [config.LAUNCH_TOPIC],
        }])
        for lg in (logs or []):
            try:
                t = lg["topics"]
                if len(t) < 4:
                    continue
                m = CID_RE.search(_ascii(lg["data"]))
                out.append({
                    "token": ("0x" + t[1][-40:]).lower(),
                    "pool": ("0x" + t[2][-40:]).lower(),
                    "creator": ("0x" + t[3][-40:]).lower(),
                    "cid": m.group(1) if m else None,
                    "block": int(lg["blockNumber"], 16),
                })
            except (KeyError, IndexError, ValueError) as e:
                log.warning("could not decode launch log: %s", e)
        start = end + 1
    return out


def fetch_metadata(cid: str) -> Optional[Dict]:
    """Resolve the launch metadata JSON. Immutable, so cache it forever."""
    if not cid:
        return None
    for gw in config.IPFS_GATEWAYS:
        # Several gateways sit behind Cloudflare, which serves a 403 challenge
        # page to unknown user agents, and trustless gateways require the ipld
        # raw Accept header.
        raw = get_bytes(gw + cid,
                        {"Accept": "application/vnd.ipld.raw",
                         "User-Agent": "Mozilla/5.0"},
                        timeout=15)
        if not raw:
            raw = get_bytes(gw + cid,
                            {"Accept": "application/json",
                             "User-Agent": "Mozilla/5.0"},
                            timeout=15)
        if not raw:
            continue
        try:
            d = json.loads(raw.decode("utf-8", "ignore"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(d, dict):
            return {
                "name": (d.get("name") or "").strip(),
                "symbol": (d.get("symbol") or "").strip(),
                "description": (d.get("description") or "").strip(),
                "website": (d.get("website") or "").strip(),
                "twitter": (d.get("twitter") or "").strip(),
                "telegram": (d.get("telegram") or "").strip(),
                "image": (d.get("image") or "").strip(),
            }
    return None


def summary(m: Optional[Dict]) -> str:
    """One line for the alert, with the destinations visible."""
    from . import tg
    if not m:
        return "no launch metadata"
    bits = []
    seen = set()
    for field in ("website", "twitter", "telegram"):
        raw = (m.get(field) or "").strip()
        if not raw or raw.lower() in seen:
            continue
        a = tg.link(raw)
        if a:
            seen.add(raw.lower())
            bits.append(a)
    if not bits:
        bits.append("no links at launch")
    d = (m.get("description") or "").strip()
    if d:
        bits.append('"%s"' % tg.esc(d[:60] + ("..." if len(d) > 60 else "")))
    return " | ".join(bits)
