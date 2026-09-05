"""Configuration, loaded from environment or a .env file sitting next to run.py."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()


def _f(key, default):
    return float(os.environ.get(key, default))


def _i(key, default):
    return int(os.environ.get(key, default))


def _b(key, default):
    return os.environ.get(key, str(default)).lower() in ("1", "true", "yes", "on")


# --- Telegram -------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
BOT_LABEL = os.environ.get("BOT_LABEL", "robinhood-chain-signal-bot")

# --- Data sources ---------------------------------------------------------
GT_BASE = "https://api.geckoterminal.com/api/v2"
NETWORK = os.environ.get("NETWORK", "robinhood")
CHAIN_NAME = os.environ.get("CHAIN_NAME", "Robinhood Chain")
CHAIN_ID = _i("CHAIN_ID", 4663)
RPC_URL = os.environ.get("RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
EXPLORER_URL = os.environ.get("EXPLORER_URL", "https://robinhoodchain.blockscout.com")
DEXSCREENER_CHAIN = os.environ.get("DEXSCREENER_CHAIN", "robinhood")
GT_WEB_NETWORK = os.environ.get("GT_WEB_NETWORK", "robinhood")
GMGN_REFERRAL_CODE = os.environ.get("GMGN_REFERRAL_CODE", "gC9GHIgN").strip()
FOMO_CHAIN = os.environ.get("FOMO_CHAIN", "robinhood").strip()
FOMO_REFERRAL_CODE = os.environ.get(
    "FOMO_REFERRAL_CODE", "WarmFineHare").strip()

# Canonical on-chain discovery contracts on Robinhood Chain.
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
PONS_V2_FACTORY = os.environ.get(
    "PONS_V2_FACTORY", "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e").lower()
V2_FACTORY = os.environ.get(
    "V2_FACTORY", "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f").lower()
V3_FACTORY = os.environ.get("V3_FACTORY", "0x1f7d7550b1b028f7571e69a784071f0205fd2efa").lower()
V4_POOL_MANAGER = os.environ.get(
    "V4_POOL_MANAGER", "0x8366a39cc670b4001a1121b8f6a443a643e40951").lower()
PONS_TOKEN_LAUNCHED_TOPIC = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"
V2_PAIR_CREATED_TOPIC = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"
V3_POOL_CREATED_TOPIC = "0x783cca1c0412dd0d695e784568c96da2e9c22ff989357a2e8b1d9b2b4e6b7118"
V4_INITIALIZE_TOPIC = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
# Old name retained for inherited helpers.
POOL_CREATED_TOPIC = V3_POOL_CREATED_TOPIC
WRAPPED_NATIVE = os.environ.get(
    "WRAPPED_NATIVE", "0x0bd7d308f8e1639fab988df18a8011f41eacad73").lower()
NATIVE_SYMBOL = os.environ.get("NATIVE_SYMBOL", "ETH")
WRAPPED_NATIVE_SYMBOL = os.environ.get("WRAPPED_NATIVE_SYMBOL", "WETH")
NATIVE_USD_FALLBACK = _f("NATIVE_USD_FALLBACK", 3500.0)
V3_DEX_NAME = os.environ.get("V3_DEX_NAME", "uniswap-v3-robinhood")
ROUTER = os.environ.get("ROUTER", "0xcaf681a66d020601342297493863e78c959e5cb2").lower()

# HyperEVM's custom launchpad decoder is intentionally off here. Robinhood
# launches span many unrelated protocols and event layouts.
LAUNCHPAD = os.environ.get("LAUNCHPAD", "0x0000000000000000000000000000000000000000").lower()
LAUNCH_TOPIC = os.environ.get(
    "LAUNCH_TOPIC",
    "0x0b4cfda446fdf9ec5a85855f088c154869eb62e3e723d7d80319b680f90e0cfd")
ENABLE_LAUNCHPAD_WATCH = _b("ENABLE_LAUNCHPAD_WATCH", False)
# IPFS gateways take seconds per fetch, so bound what startup will do.
LAUNCH_BACKFILL_BLOCKS = _i("LAUNCH_BACKFILL_BLOCKS", 12000)
LAUNCH_BACKFILL_MAX = _i("LAUNCH_BACKFILL_MAX", 25)
# ipfs.io and pinata do not resolve from every host; trustless-gateway does,
# but only answers requests carrying the ipld raw Accept header.
IPFS_GATEWAYS = [g.strip() for g in os.environ.get(
    "IPFS_GATEWAYS",
    "https://trustless-gateway.link/ipfs/,https://dweb.link/ipfs/,"
    "https://w3s.link/ipfs/,https://ipfs.io/ipfs/").split(",") if g.strip()]

# --- Safety -------------------------------------------------------------
# The inherited sell simulation understands Uniswap V3 pools only. Robinhood's
# feed contains V2, V3, V4 and custom AMMs, so leave it off until each protocol
# has an adapter; otherwise valid launches would be mislabeled.
ENABLE_SAFETY = _b("ENABLE_SAFETY", False)
SAFETY_RPCS = [u.strip() for u in os.environ.get(
    "SAFETY_RPCS", RPC_URL
).split(",") if u.strip()]
SAFETY_RPC = SAFETY_RPCS[0]
POSITION_USD = _f("POSITION_USD", 500)      # size used for the impact check
MAX_SELL_TAX_PCT = _f("MAX_SELL_TAX_PCT", 15.0)
MAX_IMPACT_PCT = _f("MAX_IMPACT_PCT", 25.0)
SAFETY_BLOCK_ON_FAIL = _b("SAFETY_BLOCK_ON_FAIL", True)
ENABLE_METADATA = _b("ENABLE_METADATA", True)

# --- Loop -----------------------------------------------------------------
POLL_SECONDS = _i("POLL_SECONDS", 60)
NEW_POOL_PAGES = _i("NEW_POOL_PAGES", 1)
ENABLE_CHAIN_WATCH = _b("ENABLE_CHAIN_WATCH", True)
ENABLE_GECKO_DISCOVERY = _b("ENABLE_GECKO_DISCOVERY", False)
# Hold a few blocks for reorg safety. On a fresh DB, inspect only a short recent
# window; after that the durable cursor guarantees gap-free catch-up.
CHAIN_CONFIRMATIONS = _i("CHAIN_CONFIRMATIONS", 3)
CHAIN_INITIAL_LOOKBACK = _i("CHAIN_INITIAL_LOOKBACK", 180)
CHAIN_LOG_CHUNK = _i("CHAIN_LOG_CHUNK", 900)
CHAIN_MAX_REPLAY = _i("CHAIN_MAX_REPLAY", 5400)
ENRICH_BATCH_SIZE = _i("ENRICH_BATCH_SIZE", 120)
# Most capacity goes to brand-new pools so launch bursts cannot bury them.
# The remainder is a fairness lane for older first-pass work and refreshes.
ENRICH_FRESH_SHARE = _f("ENRICH_FRESH_SHARE", 0.75)
# A raw V4 Initialize is often just another fee-tier pool for an existing
# token. Count it as a launch only if one of its currencies minted in that tx.
V4_REQUIRE_SAME_TX_MINT = _b("V4_REQUIRE_SAME_TX_MINT", True)
ENABLE_PONS_SCANNER = _b("ENABLE_PONS_SCANNER", True)
PONS_INITIAL_LOOKBACK = _i("PONS_INITIAL_LOOKBACK", 180)
PONS_MAX_RESERVE_CURVES = _i("PONS_MAX_RESERVE_CURVES", 10)
# GeckoTerminal documents an approximately 10 calls/minute public limit.
GT_MIN_INTERVAL = _f("GT_MIN_INTERVAL", 7.0)
GT_RETRIES = _i("GT_RETRIES", 1)
TRENDING_EVERY_N_TICKS = _i("TRENDING_EVERY_N_TICKS", 0)
WATCHLIST_REFRESH = _i("WATCHLIST_REFRESH", 0)
ENABLE_MOMENTUM_ALERTS = _b("ENABLE_MOMENTUM_ALERTS", False)

# --- On-chain scanner -----------------------------------------------------
# The inherited raw Swap decoder is V3-only. Multi-DEX coverage comes from
# GeckoTerminal until protocol-specific adapters are added.
ENABLE_SCANNER = _b("ENABLE_SCANNER", False)
SCAN_CHUNK = _i("SCAN_CHUNK", 900)        # blocks per getLogs call
SCAN_MAX_POOLS = _i("SCAN_MAX_POOLS", 400)
SURGE_WINDOW_S = _i("SURGE_WINDOW_S", 900)  # window for whale / streak tells
SCAN_BACKFILL_HOURS = _i("SCAN_BACKFILL_HOURS", 24)
SCAN_MAX_REPLAY = _i("SCAN_MAX_REPLAY", 3600)   # blocks of catch-up per tick
BUCKET_RETENTION_H = _i("BUCKET_RETENTION_H", 48)

# A single buy worth this share of pool liquidity is a whale entry.
# Set at ~p90 of the live distribution across 88 scanned pools
# (whale p90 2.25%, streak p90 12 buys / 6.35% of liquidity). Principled
# starting points, not validated predictors -- same caveat as the score weights.
WHALE_PCT = _f("WHALE_PCT", 3.0)
WHALE_MIN_USD = _f("WHALE_MIN_USD", 300)
# Consecutive buys with no sell in between, and what they must be worth.
STREAK_MIN = _i("STREAK_MIN", 12)
STREAK_PCT = _f("STREAK_PCT", 3.0)

# --- Gates (applied before scoring) --------------------------------------
MIN_LIQ_USD = _f("MIN_LIQ_USD", 10_000)
MIN_VOL_1H_USD = _f("MIN_VOL_1H_USD", 5_000)

# An hour of volume worth at least this fraction of the pool.
MIN_VELOCITY = _f("MIN_VELOCITY", 0.15)

# Base symbols that are never the subject of a shitcoin signal. Matched against
# the base leg of the pool name, case-insensitively.
EXCLUDE_BASE = set(x.strip().upper() for x in os.environ.get(
    "EXCLUDE_BASE",
    "USDC,USDT,USDG,DAI,USDE,ETH,WETH,WBTC,EURC"
).split(",") if x.strip())

# --- Alert thresholds -----------------------------------------------------
# Conservative carry-over from the HyperEVM bot. This must be recalibrated
# against Robinhood Chain outcomes before treating the rank as predictive.
SCORE_THRESHOLD = _f("SCORE_THRESHOLD", 6.0)
REAWAKEN_Z = _f("REAWAKEN_Z", 4.0)
REAWAKEN_MIN_SCORE = _f("REAWAKEN_MIN_SCORE", 1.5)
REAWAKEN_MIN_AGE_H = _f("REAWAKEN_MIN_AGE_H", 24.0)

NEW_LAUNCH_MIN_LIQ = _f("NEW_LAUNCH_MIN_LIQ", 15_000)
NEW_LAUNCH_MAX_AGE_H = _f("NEW_LAUNCH_MAX_AGE_H", 6.0)

# Re-alert only after this long, or if the score improves materially.
COOLDOWN_MINUTES = _f("COOLDOWN_MINUTES", 90)
RE_ALERT_SCORE_DELTA = _f("RE_ALERT_SCORE_DELTA", 1.0)

# Risk annotations (shown in the alert, not used to block)
MCAP_LIQ_WARN = _f("MCAP_LIQ_WARN", 20.0)

# --- Direction gates -----------------------------------------------------
# The score used to be pure volume. A pool down -100% (a rug) scored 28.19 and
# a flat pool scored 87. These gates make "going up" a precondition, not a
# tiebreaker.
MIN_CHG_1H = _f("MIN_CHG_1H", 3.0)        # must be up at least this % on the hour
MAX_DUMP_5M = _f("MAX_DUMP_5M", -20.0)    # reject only a real rollover; microcaps wick 15%
MAX_CHG_1H = _f("MAX_CHG_1H", 900.0)      # already vertical => late, not early

# --- Scoring weights ------------------------------------------------------
W_VELOCITY = _f("W_VELOCITY", 2.0)
W_ZVOL = _f("W_ZVOL", 1.2)
W_ACCEL = _f("W_ACCEL", 1.0)
Q_BREADTH_BASE = _f("Q_BREADTH_BASE", 0.4)
Q_BREADTH_GAIN = _f("Q_BREADTH_GAIN", 0.6)
Q_IMBALANCE_GAIN = _f("Q_IMBALANCE_GAIN", 0.5)

# Direction multiplier: sigmoid over blended short-window price trend.
D_W_5M = _f("D_W_5M", 0.5)
D_W_15M = _f("D_W_15M", 0.8)
D_W_1H = _f("D_W_1H", 1.0)
D_STEEPNESS = _f("D_STEEPNESS", 2.0)

# --- Storage --------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", str(ROOT / "signals.db"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
