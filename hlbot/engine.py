"""Poll loop and alert formatting."""
import logging
import time
from typing import Dict, List
from urllib.parse import quote

from . import config, dexscreener, gecko, launchpad, meta, pons, safety, scanner, score, tg
from .chain import block_number, erc20_name, erc20_symbol, poll_discoveries
from .store import Store

log = logging.getLogger("hlbot.engine")

KIND_NEW = "new_launch"
KIND_MOMENTUM = "momentum"
KIND_REAWAKEN = "reawaken"
KIND_WHALE = "whale_buy"
KIND_ACCUM = "accumulation"

LABEL = {
    KIND_NEW: "\U0001F195 NEW LAUNCH",
    KIND_MOMENTUM: "\U0001F680 MOMENTUM",
    KIND_REAWAKEN: "\U000023F0 REAWAKENING",
    KIND_WHALE: "\U0001F433 WHALE BUY",
    KIND_ACCUM: "\U0001F4C8 ACCUMULATION",
}


def fomo_token_url(token_address: str) -> str:
    """Return Fomo's token deep link, preserving the referral at signup."""
    url = "https://fomo.family/tokens/{}/{}".format(
        quote(config.FOMO_CHAIN, safe=""), quote(token_address, safe=""))
    if config.FOMO_REFERRAL_CODE:
        url += "?r=" + quote(config.FOMO_REFERRAL_CODE, safe="")
    return url


def _usd(v) -> str:
    v = float(v or 0)
    if v >= 1_000_000:
        return "$%.2fM" % (v / 1_000_000)
    if v >= 1_000:
        return "$%.1fk" % (v / 1_000)
    return "$%.0f" % v


def _pct(v) -> str:
    if v is None:
        return "n/a"
    return "{:+,.1f}%".format(float(v))


def _age(hours: float) -> str:
    if hours < 1:
        return "%.0fm" % (hours * 60)
    if hours < 48:
        return "%.1fh" % hours
    return "%.1fd" % (hours / 24)


def format_safety(s: Dict) -> list:
    if not s:
        return []
    out = ["", "<b>safety</b>"]
    cs = s.get("can_sell")
    out.append("sell     %s" % ("OK" if cs else ("BLOCKED" if cs is False else "unknown")))
    if s.get("tax_pct") is not None:
        out.append("tax      {:.2f}%  (incl. pool fee)".format(s["tax_pct"]))
    if s.get("impact_pct") is not None:
        out.append("impact   {:.2f}% at ${:,.0f}".format(s["impact_pct"], config.POSITION_USD))
    if s.get("supply_in_pool") is not None:
        out.append("in pool  {:.1f}% of supply".format(s["supply_in_pool"]))
    for n in (s.get("notes") or [])[:2]:
        out.append("note     " + str(n)[:60])
    return out


def format_history(h: Dict, price_now: float) -> str:
    """How often we have called this token, and how the first call aged."""
    if not h or not h.get("count"):
        return "calls    #1 (first time)"
    n = h["count"] + 1
    bits = "calls    #{}".format(n)
    if h.get("first_ts"):
        ago = (time.time() - h["first_ts"]) / 3600.0
        bits += " | first {} ago".format(
            "%.0fm" % (ago * 60) if ago < 1 else "%.1fh" % ago)
        fp = h.get("first_price")
        if fp and fp > 0 and price_now:
            bits += " at ${:.6g} ({:+.0f}% since)".format(
                fp, (price_now / fp - 1) * 100)
    kinds = h.get("by_kind") or {}
    if kinds:
        bits += " | " + ", ".join("%dx %s" % (v, k.replace("_", " "))
                                  for k, v in sorted(kinds.items(),
                                                     key=lambda kv: -kv[1]))
    return bits


def format_alert(kind: str, p: Dict, f: score.Features,
                 saf: Dict = None, m: Dict = None, rep: Dict = None,
                 lm: Dict = None, tok: Dict = None, dup: Dict = None,
                 hist: Dict = None) -> str:
    addr = p["address"]
    base = p.get("base_token") or ""
    mcap_liq = (p["fdv_usd"] / p["liq_usd"]) if p["liq_usd"] else 0.0

    lines = [
        "<b>{}</b>  <code>{}</code>".format(LABEL[kind], tg.esc(p["name"])),
        "<b>score {:.2f}</b>  |  {}  |  age {}".format(
            f.score, tg.esc(p.get("dex", "")), _age(p["age_hours"])),
    ]
    if tok and tok.get("name") and tok["name"] not in ("?", tok.get("symbol")):
        lines.append("name     <b>{}</b>".format(tg.esc(tok["name"])))
    lines.append(format_history(hist, p.get("price_usd")))
    lines += [
        "",
        "price   {} 5m | <b>{} 1h</b> | {} 6h | {} 24h".format(
            _pct(p["chg_5m"]), _pct(p["chg_1h"]), _pct(p["chg_6h"]), _pct(p["chg_24h"])),
        "liq     {}      fdv {}".format(_usd(p["liq_usd"]), _usd(p["fdv_usd"])),
        "vol     {} 1h | {} 24h".format(_usd(p["vol_1h"]), _usd(p["vol_24h"])),
        "",
        "velocity {:.2f}/hr | accel {:.2f} | zvol {:.1f}".format(
            f.velocity, f.accel, f.zvol),
        "trend    {:.2f} (dir {:.2f})".format(f.trend, f.direction),
        "breadth  {:.2f}  ({} buyers / {} buys)".format(
            f.breadth, p["buyers_1h"], p["buys_1h"]),
        "flow     {:+.2f}  ({}B vs {}S)".format(
            f.imbalance, p["buyers_1h"], p["sellers_1h"]),
    ]
    if p.get("whale_pct"):
        lines.append("big buy  {:.2f}% of liq (${:,.0f})".format(
            p["whale_pct"], p.get("whale_usd", 0)))
    if p.get("buy_streak"):
        lines.append("streak   {} buys in a row, {:.2f}% of liq (${:,.0f})".format(
            p["buy_streak"], p.get("streak_pct", 0), p.get("streak_usd", 0)))

    warn = []
    if mcap_liq >= config.MCAP_LIQ_WARN:
        warn.append("mcap/liq {:.0f}x".format(mcap_liq))
    if f.imbalance < 0:
        warn.append("net sellers")
    if f.accel < 0.5 and kind != KIND_NEW:
        warn.append("decelerating")
    if f.breadth < 0.35:
        warn.append("low breadth")
    if dup and dup.get("same_symbol"):
        warn.append("ticker shared with {}".format(dup["same_symbol"]))
    if dup and dup.get("same_name"):
        warn.append("name shared with {}".format(dup["same_name"]))
    if warn:
        lines += ["", "\U000026A0 " + " | ".join(warn)]

    lines += format_safety(saf)

    if lm is not None:
        lines += ["", "at launch " + launchpad.summary(lm)]
    if m:
        lines.append("now      " + meta.summary(m))
    if rep and rep.get("pools", 0) > 1:
        lines.append("deployer {} prior pools, {} honeypots".format(
            rep["pools"] - 1, rep.get("honeypots", 0)))

    lines += [
        "",
        '<a href="https://dexscreener.com/{ds}/{a}">dexscreener</a>'
        ' | <a href="https://www.geckoterminal.com/{gt}/pools/{a}">geckoterminal</a>'.format(
            ds=config.DEXSCREENER_CHAIN, gt=config.GT_WEB_NETWORK, a=addr),
    ]
    if base:
        gmgn_token = "{}_{}".format(config.GMGN_REFERRAL_CODE, base) \
            if config.GMGN_REFERRAL_CODE else base
        lines.append(
            '<a href="https://gmgn.ai/robinhood/token/{gmgn}">gmgn</a>'
            ' | <a href="{fomo}">fomo</a>'
            ' | <a href="{explorer}/token/{b}">explorer</a>'.format(
                gmgn=tg.esc(gmgn_token), fomo=tg.esc(fomo_token_url(base)),
                b=tg.esc(base),
                explorer=config.EXPLORER_URL.rstrip("/")))
        lines.append("<code>{}</code>".format(tg.esc(base)))
    return "\n".join(lines)


class Engine:
    def __init__(self, store: Store = None, dry_run: bool = False):
        self.store = store or Store()
        self.dry_run = dry_run
        self.last_block = None
        self.last_launch_block = None
        self.native_usd = config.NATIVE_USD_FALLBACK

    # -- chain tier --------------------------------------------------------
    def scan_chain(self) -> List[Dict]:
        """Persist every launch/pool emitted by configured chain contracts."""
        if not config.ENABLE_CHAIN_WATCH:
            return []
        tip = block_number()
        if tip is None:
            return []
        safe_tip = max(tip - config.CHAIN_CONFIRMATIONS, 0)
        if self.last_block is None:
            saved = self.store.get_state("discovery:last_block")
            self.last_block = (int(saved) if saved is not None else
                               max(safe_tip - config.CHAIN_INITIAL_LOOKBACK, 0))
        if safe_tip <= self.last_block:
            return []
        target = min(safe_tip, self.last_block + config.CHAIN_MAX_REPLAY)
        created = poll_discoveries(self.last_block + 1, target)
        fresh = []
        for row in created:
            if self.store.put_discovery(row):
                fresh.append(row)
        # Advance only after every source and every DB insert succeeded.
        self.store.set_state("discovery:last_block", target)
        self.last_block = target
        if fresh:
            by_protocol = {}
            for row in fresh:
                name = row["protocol"]
                by_protocol[name] = by_protocol.get(name, 0) + 1
            log.info("chain: %d discoveries %s", len(fresh), by_protocol)
        return fresh

    # -- enrichment tier ---------------------------------------------------
    def gather(self) -> Dict[str, Dict]:
        """Enrich only pools first discovered from on-chain events."""
        pools = {}
        if config.ENABLE_PONS_SCANNER:
            try:
                pools.update(pons.scan(self.store))
            except Exception:  # noqa: BLE001
                log.exception("Pons on-chain scanner failed")
        if config.ENABLE_SCANNER:
            try:
                pools.update(self.scan_pools())
            except Exception:  # noqa: BLE001 - never let the scanner kill a tick
                log.exception("scanner failed")

        if config.ENABLE_GECKO_DISCOVERY:
            # Explicit fallback only; disabled in the deployed configuration.
            for p in gecko.new_pools():
                address = (p.get("address") or "").lower()
                if address:
                    pools[address] = p

        queue = self.store.pending_discoveries(
            config.ENRICH_BATCH_SIZE, config.NEW_LAUNCH_MAX_AGE_H,
            exclude_protocol="pons-v2")
        attempted = [row["address"] for row in queue]
        if attempted:
            enriched = dexscreener.pairs_by_address(attempted)
            self.store.touch_discoveries(attempted)
            queued = {row["address"]: row for row in queue}
            for p in enriched:
                address = (p.get("address") or "").lower()
                if not address:
                    continue
                discovery = queued.get(address) or self.store.discovery(address)
                if discovery:
                    if not p.get("base_token"):
                        p["base_token"] = discovery.get("base_token") or ""
                    if not p.get("dex"):
                        p["dex"] = discovery.get("protocol") or ""
                self.store.upsert_pool(p)
                if discovery and discovery.get("creator"):
                    self.store.set_creator(address, discovery["creator"])
                pools[address] = p

        self._tick_no = getattr(self, "_tick_no", 0) + 1
        if (config.TRENDING_EVERY_N_TICKS > 0
                and self._tick_no % config.TRENDING_EVERY_N_TICKS == 1):
            for p in gecko.trending_pools():
                if p["address"]:
                    pools[p["address"].lower()] = p

        stale = [a for a in self.store.watchlist() if a not in pools]
        if stale and config.WATCHLIST_REFRESH > 0:
            for p in gecko.pools_by_address(stale[:config.WATCHLIST_REFRESH]):
                if p["address"]:
                    pools[p["address"].lower()] = p
        return pools

    def scan_pools(self) -> Dict[str, Dict]:
        pmap = self.store.pool_map(config.WRAPPED_NATIVE, config.SCAN_MAX_POOLS)
        if not pmap:
            return {}
        scanner.scan(self.store, pmap, self.native_usd)
        since = time.time() - 24 * 3600
        windows = self.store.bucket_windows(since)
        out = {}
        for pool, rows in windows.items():
            w0 = pmap.get(pool)
            if w0 is None:
                continue
            m = scanner.build_metrics(pool, rows, self.native_usd, w0)
            if not m:
                continue
            rec = self.store.pool_meta(pool) or {}
            base = rec.get("base_token")
            if base:
                t = self.store.get_token(base)
                if t and t.get("supply"):
                    m["fdv_usd"] = m["price_usd"] * t["supply"]
            m["name"] = rec.get("name") or ""
            m["base_token"] = rec.get("base_token") or ""
            m["dex"] = rec.get("dex") or config.V3_DEX_NAME
            # Bucket span understates age for a pool we only started watching
            # recently, and REAWAKENING keys off age.
            if rec.get("first_seen"):
                seen_h = (time.time() - rec["first_seen"]) / 3600.0
                m["age_hours"] = max(m["age_hours"], seen_h)
            out[pool] = m
        return out

    def creator_of(self, pool_addr):
        cur = self.store.db.execute("SELECT creator FROM pools WHERE address = ?",
                                    (pool_addr.lower(),))
        row = cur.fetchone()
        return row["creator"] if row else None

    def backfill_scanner(self, hours: int = None):
        """Seed swap history so the first ticks have real 1h/24h context."""
        if not config.ENABLE_SCANNER:
            return 0
        pmap = self.store.pool_map(config.WRAPPED_NATIVE, config.SCAN_MAX_POOLS)
        if not pmap:
            return 0
        self.native_usd = meta.native_usd()
        try:
            return scanner.backfill(self.store, pmap, self.native_usd,
                                    hours or config.SCAN_BACKFILL_HOURS)
        except Exception:  # noqa: BLE001
            log.exception("scanner backfill failed")
            return 0

    def backfill_launches(self, blocks: int = None, max_fetch: int = None):
        """Populate launch metadata for tokens launched in the recent past.

        Without this, only pools first seen by this process would have their
        deploy-time links; everything already trading would show blank.
        """
        if not config.ENABLE_LAUNCHPAD_WATCH:
            return 0
        blocks = blocks or config.LAUNCH_BACKFILL_BLOCKS
        max_fetch = max_fetch or config.LAUNCH_BACKFILL_MAX
        tip = block_number()
        if tip is None:
            return 0
        n = 0
        try:
            launches = launchpad.poll_launches(max(tip - blocks, 0), tip)
        except Exception:  # noqa: BLE001
            log.exception("launch backfill failed")
            return 0
        for l in launches:
            if not l.get("cid"):
                continue
            if self.store.get_launch_meta(l["token"]) is not None:
                continue
            m = launchpad.fetch_metadata(l["cid"])
            self.store.put_launch_meta(l["token"], l["cid"], m)
            if self.store.get_token(l["token"]) is None:
                self.store.put_token(l["token"], erc20_symbol(l["token"]),
                                     erc20_name(l["token"]))
            if l.get("creator"):
                self.store.set_creator(l["pool"], l["creator"])
            n += 1
            if n >= max_fetch:      # IPFS is slow; bound the startup cost
                break
        log.info("launch backfill: %d tokens over %d blocks", n, blocks)
        return n

    def token_identity(self, p: Dict):
        """symbol + full name, cached. Fetched on demand for pools that were
        discovered through the indexer rather than the chain watcher."""
        token = p.get("base_token")
        if not token:
            return None
        t = self.store.get_token(token)
        if t and t.get("name") and t.get("supply"):
            return t
        if not (t and t.get("name")):
            sym = erc20_symbol(token)
            nm = erc20_name(token)
            self.store.put_token(token, sym, nm)
            t = {"symbol": sym, "name": nm, "supply": None}
        if not t.get("supply"):
            try:
                sup = safety.total_supply(token)
                if sup:
                    dec = safety.erc20_uint(token, "0x313ce567")
                    dec = 18 if dec is None or dec > 36 else dec
                    self.store.set_supply(token, sup / (10 ** dec))
                    t["supply"] = sup / (10 ** dec)
            except Exception:  # noqa: BLE001
                pass
        return t

    def launch_meta_for(self, p: Dict):
        """Links set at deploy time. Immutable, so fetched once and kept."""
        token = p.get("base_token")
        if not token:
            return None
        cached = self.store.get_launch_meta(token)
        if cached is not None:
            return cached
        return None

    def safety_for(self, p: Dict):
        """Cached; the slot probing is ~40 eth_calls so never repeat it per tick."""
        if not config.ENABLE_SAFETY:
            return None
        token = p.get("base_token")
        if not token:
            return None
        cached = self.store.get_safety(token)
        if cached is not None:
            return cached
        fee = safety.pool_fee(p["address"]) or 3000
        try:
            s = safety.check(token, p["address"], fee,
                             config.POSITION_USD, self.native_usd)
        except Exception:  # noqa: BLE001 - safety must never break the loop
            log.exception("safety check failed for %s", token)
            return None
        self.store.put_safety(token, s)
        return s

    # -- decisioning -------------------------------------------------------
    def evaluate(self, p: Dict):
        """Return (kind, features); kind is None when nothing should be sent."""
        f = score.compute(
            liq_usd=p["liq_usd"], vol_1h=p["vol_1h"], vol_24h=p["vol_24h"],
            vol_5m=p["vol_5m"], buys_1h=p["buys_1h"], buyers_1h=p["buyers_1h"],
            sellers_1h=p["sellers_1h"], age_hours=p["age_hours"],
            chg_5m=p["chg_5m"], chg_15m=p.get("chg_15m", 0.0), chg_1h=p["chg_1h"],
        )
        if f is None:
            return None, None

        # Stablecoin arb pairs and blue chips are never the subject here.
        if score.is_excluded(p["name"]):
            return None, f

        # Only ever alert on something that is actually going up.
        if not score.direction_ok(p["chg_1h"], p["chg_5m"]):
            return None, f

        # Early tells the aggregate feed cannot see. Checked before the score
        # gate because the whole point is to fire before momentum is obvious.
        if score.passes_gate(p["liq_usd"], p["vol_1h"], p["name"],
                             p["chg_1h"], p["chg_5m"]):
            if (p.get("whale_pct", 0) >= config.WHALE_PCT
                    and p.get("whale_usd", 0) >= config.WHALE_MIN_USD):
                return KIND_WHALE, f
            if (p.get("buy_streak", 0) >= config.STREAK_MIN
                    and p.get("streak_pct", 0) >= config.STREAK_PCT):
                return KIND_ACCUM, f

        # New launch: young, funded, rising, and not yet announced.
        if (p["age_hours"] <= config.NEW_LAUNCH_MAX_AGE_H
                and p["liq_usd"] >= config.NEW_LAUNCH_MIN_LIQ
                and not self.store.was_announced(p["address"])
                and not self.store.token_was_announced(p.get("base_token"))):
            return KIND_NEW, f

        if not config.ENABLE_MOMENTUM_ALERTS:
            return None, f

        if not score.passes_gate(p["liq_usd"], p["vol_1h"], p["name"],
                                 p["chg_1h"], p["chg_5m"]):
            return None, f

        # Reawakening: an old pool trading far above its own baseline. This is
        # the EGG case -- the 150x came 19 days after the pool was created, so
        # a new-launch-only feed would never have surfaced it.
        if (p["age_hours"] >= config.REAWAKEN_MIN_AGE_H
                and f.zvol >= config.REAWAKEN_Z
                and f.score >= config.REAWAKEN_MIN_SCORE):
            return KIND_REAWAKEN, f

        if f.score >= config.SCORE_THRESHOLD:
            return KIND_MOMENTUM, f

        return None, f

    def tick(self) -> int:
        if config.ENABLE_SAFETY:
            self.native_usd = meta.native_usd()
        self.scan_chain()
        pools = self.gather()
        sent = 0

        for addr, p in pools.items():
            self.store.upsert_pool(p)
            kind, f = self.evaluate(p)
            if f is not None:
                payload = dict(p)
                payload.update(f.as_dict())
                self.store.log_features(addr, payload)
            if not kind:
                continue
            if not self.store.should_alert(addr, kind, f.score):
                continue

            saf = self.safety_for(p)
            if saf and config.SAFETY_BLOCK_ON_FAIL:
                ok, reasons = safety.verdict(saf)
                if not ok:
                    log.info("suppressed %s %s: %s", kind, p["name"], "; ".join(reasons))
                    self.store.record_alert(addr, kind, f.score)
                    continue

            m = meta.fetch(p["base_token"]) if (config.ENABLE_METADATA and p.get("base_token")) else None
            rep = self.store.creator_stats(self.creator_of(addr))
            lm = self.launch_meta_for(p)
            hist = self.store.alert_history(addr, p.get("base_token"))
            tok = self.token_identity(p)
            if tok and tok.get("supply") and not p.get("fdv_usd"):
                p["fdv_usd"] = p.get("price_usd", 0.0) * tok["supply"]
            dup = self.store.copycats(p["base_token"], (tok or {}).get("symbol"),
                                      (tok or {}).get("name")) if tok else None
            msg = format_alert(kind, p, f, saf, m, rep, lm, tok, dup, hist)
            if self.dry_run:
                print("\n" + "=" * 60 + "\n[DRY RUN] " + kind + "\n" + msg)
                ok = True
            else:
                ok = tg.send(msg)
            if ok:
                self.store.record_alert(addr, kind, f.score)
                self.store.log_alert(addr, p.get("base_token"), kind,
                                     f.score, p.get("price_usd"))
                if kind == KIND_NEW:
                    self.store.mark_announced(addr)
                sent += 1
                log.info("alert %s %s score=%.2f", kind, p["name"], f.score)

        log.info("tick: %d pools evaluated, %d alerts", len(pools), sent)
        return sent

    def run(self):
        log.info("starting %s; poll=%ss gate=liq>=%s vol1h>=%s score>=%s",
                 config.CHAIN_NAME, config.POLL_SECONDS, config.MIN_LIQ_USD,
                 config.MIN_VOL_1H_USD, config.SCORE_THRESHOLD)
        cycle = 0
        while True:
            started = time.time()
            try:
                self.tick()
            except KeyboardInterrupt:
                raise
            except Exception:  # noqa: BLE001 - a bad tick must not kill the bot
                log.exception("tick failed")
            cycle += 1
            if cycle % 240 == 0:
                self.store.prune()
                self.store.prune_buckets(config.BUCKET_RETENTION_H)
            elapsed = time.time() - started
            time.sleep(max(5.0, config.POLL_SECONDS - elapsed))
