"""On-chain pool and launch discovery for Robinhood Chain.

The RPC log stream is the source of truth. GeckoTerminal is used later only
to enrich addresses discovered here with indexed market metrics.
"""
import logging
from typing import Dict, List, Optional

from . import config
from .http import post_json

log = logging.getLogger("hlbot.chain")

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_TOPIC = "0x" + "0" * 64


def _rpc(method: str, params: list):
    resp = post_json(config.RPC_URL, {
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
    })
    if not resp or "result" not in resp:
        message = (resp or {}).get("error", {}).get("message") or str(resp)
        log.warning("rpc %s error: %s", method, message)
        return None
    return resp["result"]


def block_number():
    result = _rpc("eth_blockNumber", [])
    return int(result, 16) if result else None


def chain_id():
    result = _rpc("eth_chainId", [])
    return int(result, 16) if result else None


def _topic_addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def _word(data: str, index: int) -> str:
    body = data[2:] if data.startswith("0x") else data
    start = index * 64
    return body[start:start + 64]


def _data_addr(data: str, index: int) -> str:
    return "0x" + _word(data, index)[-40:].lower()


def _base_token(token0: str, token1: str) -> str:
    """Pick the non-native leg provisionally; Gecko can refine it later."""
    native = {config.ZERO_ADDRESS, config.WRAPPED_NATIVE.lower()}
    if token0.lower() in native:
        return token1.lower()
    if token1.lower() in native:
        return token0.lower()
    return token0.lower()


def _logs(address: str, topic: str, start: int, end: int) -> list:
    result = _rpc("eth_getLogs", [{
        "fromBlock": hex(start), "toBlock": hex(end),
        "address": address, "topics": [topic],
    }])
    if result is None:
        raise RuntimeError("eth_getLogs failed for {} {}-{}".format(
            address, start, end))
    return result


def _v4_launch_logs(entries: list) -> list:
    """Keep V4 initializations that minted a currency in the same tx.

    V4's Initialize means "new pool", not necessarily "new token". On this
    chain bots routinely open many fee-tier pools for the same old token. A
    same-transaction ERC-20 mint identifies factory-style launches (including
    Bankr) while Pons launches are already covered directly by its factory.
    """
    if not entries or not config.V4_REQUIRE_SAME_TX_MINT:
        return entries
    payload = [{
        "jsonrpc": "2.0", "id": index, "method": "eth_getTransactionReceipt",
        "params": [entry["transactionHash"]],
    } for index, entry in enumerate(entries)]
    response = post_json(config.RPC_URL, payload)
    if not isinstance(response, list):
        raise RuntimeError("batch receipt lookup failed for V4 launches")
    receipts = {item.get("id"): item.get("result") for item in response
                if isinstance(item, dict)}
    kept = []
    for index, entry in enumerate(entries):
        receipt = receipts.get(index)
        if not receipt:
            raise RuntimeError("missing V4 receipt {}".format(
                entry.get("transactionHash")))
        currencies = {_topic_addr(entry["topics"][2]),
                      _topic_addr(entry["topics"][3])}
        minted = False
        for emitted in receipt.get("logs", []):
            topics = emitted.get("topics") or []
            if (emitted.get("address", "").lower() in currencies
                    and len(topics) >= 3
                    and topics[0].lower() == TRANSFER_TOPIC
                    and topics[1].lower() == ZERO_TOPIC):
                minted = True
                break
        if minted:
            entry["_creator"] = receipt.get("from", "").lower() or None
            kept.append(entry)
    if len(kept) != len(entries):
        log.info("V4: %d launches from %d pool initializations",
                 len(kept), len(entries))
    return kept


def _common(log_entry: dict, protocol: str) -> Dict:
    return {
        "protocol": protocol,
        "block": int(log_entry["blockNumber"], 16),
        "log_index": int(log_entry.get("logIndex", "0x0"), 16),
        "tx": log_entry.get("transactionHash"),
    }


def _decode_pons(log_entry: dict) -> Dict:
    # TokenLaunched(token indexed, curve indexed, deployer indexed,
    #               pairToken, launchConfigId, graduationThreshold)
    topics = log_entry["topics"]
    token = _topic_addr(topics[1])
    curve = _topic_addr(topics[2])
    pair = _data_addr(log_entry["data"], 0)
    out = _common(log_entry, "pons-v2")
    out.update({
        "pool": curve, "token0": token, "token1": pair,
        "base_token": token, "creator": _topic_addr(topics[3]),
    })
    return out


def _decode_v2(log_entry: dict) -> Dict:
    # PairCreated(token0 indexed, token1 indexed, pair, pairCount)
    topics = log_entry["topics"]
    token0, token1 = _topic_addr(topics[1]), _topic_addr(topics[2])
    out = _common(log_entry, "uniswap-v2")
    out.update({
        "pool": _data_addr(log_entry["data"], 0),
        "token0": token0, "token1": token1,
        "base_token": _base_token(token0, token1), "creator": None,
    })
    return out


def _decode_v3(log_entry: dict) -> Dict:
    # PoolCreated(token0 indexed, token1 indexed, fee indexed,
    #             tickSpacing, pool)
    topics = log_entry["topics"]
    token0, token1 = _topic_addr(topics[1]), _topic_addr(topics[2])
    out = _common(log_entry, "uniswap-v3")
    out.update({
        "pool": _data_addr(log_entry["data"], 1),
        "token0": token0, "token1": token1,
        "base_token": _base_token(token0, token1), "creator": None,
        "fee": int(topics[3], 16),
    })
    return out


def _decode_v4(log_entry: dict) -> Dict:
    # Initialize(poolId indexed, currency0 indexed, currency1 indexed,
    #            fee, tickSpacing, hooks, sqrtPriceX96, tick)
    topics = log_entry["topics"]
    token0, token1 = _topic_addr(topics[2]), _topic_addr(topics[3])
    out = _common(log_entry, "uniswap-v4")
    out.update({
        # V4 pools have no contract address. Indexers key them by PoolId.
        "pool": topics[1].lower(),
        "token0": token0, "token1": token1,
        "base_token": _base_token(token0, token1),
        "creator": log_entry.get("_creator"),
        "fee": int(_word(log_entry["data"], 0), 16),
    })
    return out


SOURCES = (
    ("pons-v2", lambda: config.PONS_V2_FACTORY,
     lambda: config.PONS_TOKEN_LAUNCHED_TOPIC, _decode_pons),
    ("uniswap-v2", lambda: config.V2_FACTORY,
     lambda: config.V2_PAIR_CREATED_TOPIC, _decode_v2),
    # eth_getLogs accepts an address array, so all compatible V3 factories are
    # covered in one RPC call per block chunk without increasing request load.
    ("uniswap-v3", lambda: config.V3_FACTORIES,
     lambda: config.V3_POOL_CREATED_TOPIC, _decode_v3),
    ("uniswap-v4", lambda: config.V4_POOL_MANAGER,
     lambda: config.V4_INITIALIZE_TOPIC, _decode_v4),
)


def poll_discoveries(from_block: int, to_block: int) -> List[Dict]:
    """Return creation events from every configured source.

    All sources must complete before callers advance their persistent cursor.
    This makes an RPC failure retryable instead of silently losing blocks.
    """
    found = []
    for start in range(from_block, to_block + 1, config.CHAIN_LOG_CHUNK):
        end = min(start + config.CHAIN_LOG_CHUNK - 1, to_block)
        for name, address_fn, topic_fn, decoder in SOURCES:
            address = address_fn()
            if not address or address == config.ZERO_ADDRESS:
                continue
            entries = _logs(address, topic_fn(), start, end)
            if name == "uniswap-v4":
                entries = _v4_launch_logs(entries)
            for entry in entries:
                try:
                    found.append(decoder(entry))
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    log.warning("could not decode %s event: %s", name, exc)
    found.sort(key=lambda row: (row["block"], row["log_index"]))
    return found


# Backward-compatible name used by inherited V3-only tooling.
def poll_new_pools(from_block: int, to_block: int) -> List[Dict]:
    return [row for row in poll_discoveries(from_block, to_block)
            if row["protocol"] == "uniswap-v3"]


def tx_sender(tx_hash: str) -> Optional[str]:
    if not tx_hash:
        return None
    tx = _rpc("eth_getTransactionByHash", [tx_hash])
    return (tx or {}).get("from", "").lower() or None


def erc20_symbol(address: str) -> str:
    return _erc20_string(address, "0x95d89b41")


def erc20_name(address: str) -> str:
    return _erc20_string(address, "0x06fdde03")


def _erc20_string(address: str, selector: str) -> str:
    if not address or address == config.ZERO_ADDRESS:
        return config.NATIVE_SYMBOL
    result = _rpc("eth_call", [{"to": address, "data": selector}, "latest"])
    if not result or result == "0x":
        return "?"
    body = result[2:]
    try:
        if len(body) >= 128:
            length = int(body[64:128], 16)
            if 0 < length <= 200:
                raw = bytes.fromhex(body[128:128 + length * 2])
                return raw.decode("utf-8", "replace").strip("\x00") or "?"
        return bytes.fromhex(body[:64]).decode("utf-8", "replace").strip("\x00") or "?"
    except (ValueError, UnicodeDecodeError):
        return "?"
