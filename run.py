#!/usr/bin/env python3
"""Robinhood Chain launch + momentum signal bot.

    python3 run.py --verify     check bot token and channel wiring
    python3 run.py --once       one poll cycle, then exit
    python3 run.py --dry-run    print alerts to stdout, never send
    python3 run.py --backtest   score the current market and rank it
    python3 run.py              run forever
"""
import argparse
import logging
import sys

from hlbot import config, gecko, score, tg
from hlbot.chain import chain_id
from hlbot.engine import Engine, format_alert
from hlbot.store import Store


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_verify() -> int:
    cid = chain_id()
    if cid != config.CHAIN_ID:
        print("FAIL  RPC chain id = {} (expected {})".format(cid, config.CHAIN_ID))
        return 1
    print("OK    RPC = {} (chain id {})".format(config.CHAIN_NAME, cid))
    print("BOT_TOKEN set:", bool(config.BOT_TOKEN))
    print("CHAT_ID:", config.CHAT_ID or "(unset)")

    me = tg.get_me()
    if not me or not me.get("ok"):
        print("FAIL  getMe rejected the token:", (me or {}).get("description"))
        return 1
    bot = me["result"]
    print("OK    bot = @{} ({})".format(bot.get("username"), bot.get("first_name")))

    chat = tg.get_chat()
    if not chat or not chat.get("ok"):
        desc = (chat or {}).get("description", "no response")
        print("FAIL  cannot reach chat {}: {}".format(config.CHAT_ID, desc))
        print()
        print("      Add @{} to the channel as an ADMIN with".format(bot.get("username")))
        print("      'Post Messages' enabled, then run --verify again.")
        return 1
    c = chat["result"]
    print("OK    chat = {} ({})".format(c.get("title"), c.get("type")))

    if tg.send("<b>{}</b> connected. Wiring verified.".format(config.BOT_LABEL)):
        print("OK    test message delivered")
        return 0
    print("FAIL  could not post to the channel (is 'Post Messages' enabled?)")
    return 1


def cmd_backtest(limit: int = 20) -> int:
    """Score the live market and rank it. No alerts, no state written."""
    pools = {}
    for p in gecko.new_pools() + gecko.trending_pools() + gecko.top_pools():
        if p["address"]:
            pools[p["address"].lower()] = p

    rows = []
    for p in pools.values():
        f = score.compute(
            liq_usd=p["liq_usd"], vol_1h=p["vol_1h"], vol_24h=p["vol_24h"],
            vol_5m=p["vol_5m"], buys_1h=p["buys_1h"], buyers_1h=p["buyers_1h"],
            sellers_1h=p["sellers_1h"], age_hours=p["age_hours"],
            chg_5m=p["chg_5m"], chg_15m=p.get("chg_15m", 0.0), chg_1h=p["chg_1h"],
        )
        if f is None or not score.passes_gate(p["liq_usd"], p["vol_1h"], p["name"],
                                              p["chg_1h"], p["chg_5m"]):
            continue
        rows.append((f.score, p, f))
    rows.sort(key=lambda r: -r[0])

    print("{} pools sampled, {} passed the gate\n".format(len(pools), len(rows)))
    hdr = "{:<24} {:>10} {:>10} {:>7} {:>7} {:>6} {:>7} {:>9} {:>7} {:>6}"
    print(hdr.format("POOL", "LIQ", "VOL_1H", "V/hr", "ACCEL", "BRDT",
                     "BUYIMB", "CHG1H%", "AGEh", "SCORE"))
    for s, p, f in rows[:limit]:
        print(hdr.format(
            p["name"][:24], "{:,.0f}".format(p["liq_usd"]), "{:,.0f}".format(p["vol_1h"]),
            "{:.2f}".format(f.velocity), "{:.2f}".format(f.accel), "{:.2f}".format(f.breadth),
            "{:+.2f}".format(f.imbalance), "{:,.1f}".format(p["chg_1h"]),
            "{:,.0f}".format(p["age_hours"]), "{:.2f}".format(s)))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Robinhood Chain launch + momentum signal bot")
    ap.add_argument("--verify", action="store_true", help="check token and channel, send a test message")
    ap.add_argument("--once", action="store_true", help="run a single poll cycle then exit")
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending them")
    ap.add_argument("--backtest", action="store_true", help="rank the live market by score")
    args = ap.parse_args()

    setup_logging()

    if args.verify:
        return cmd_verify()
    if args.backtest:
        return cmd_backtest()

    if not args.dry_run and (not config.BOT_TOKEN or not config.CHAT_ID):
        print("BOT_TOKEN and CHAT_ID must be set (see .env.example). "
              "Use --dry-run to test without sending.", file=sys.stderr)
        return 1

    engine = Engine(store=Store(), dry_run=args.dry_run)
    engine.backfill_scanner()
    engine.backfill_launches()
    if args.once:
        engine.tick()
        return 0
    try:
        engine.run()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
