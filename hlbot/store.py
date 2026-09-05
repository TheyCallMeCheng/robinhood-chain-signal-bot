"""SQLite state: dedup, cooldowns, and a feature log for tuning the weights."""
import json
import logging
import sqlite3
import time
from typing import Optional

from . import config

log = logging.getLogger("hlbot.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pools (
    address       TEXT PRIMARY KEY,
    name          TEXT,
    dex           TEXT,
    base_token    TEXT,
    created_at    TEXT,
    first_seen    REAL,
    announced_new INTEGER DEFAULT 0,
    creator       TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    address    TEXT,
    kind       TEXT,
    sent_at    REAL,
    score      REAL,
    PRIMARY KEY (address, kind)
);
CREATE TABLE IF NOT EXISTS alert_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT,
    token   TEXT,
    kind    TEXT,
    ts      REAL,
    score   REAL,
    price   REAL
);
CREATE INDEX IF NOT EXISTS idx_alert_log_token ON alert_log(token);
CREATE INDEX IF NOT EXISTS idx_alert_log_addr ON alert_log(address);
CREATE TABLE IF NOT EXISTS safety (
    token      TEXT PRIMARY KEY,
    checked_at REAL,
    can_sell   INTEGER,
    tax_pct    REAL,
    impact_pct REAL,
    payload    TEXT
);
CREATE TABLE IF NOT EXISTS launch_meta (
    token       TEXT PRIMARY KEY,
    cid         TEXT,
    website     TEXT,
    twitter     TEXT,
    telegram    TEXT,
    description TEXT,
    fetched_at  REAL
);
CREATE TABLE IF NOT EXISTS tokens (
    address     TEXT PRIMARY KEY,
    symbol      TEXT,
    name        TEXT,
    symbol_norm TEXT,
    name_norm   TEXT,
    first_seen  REAL,
    supply      REAL
);
CREATE INDEX IF NOT EXISTS idx_tokens_symbol ON tokens(symbol_norm);
CREATE INDEX IF NOT EXISTS idx_tokens_name ON tokens(name_norm);
CREATE TABLE IF NOT EXISTS buckets (
    pool    TEXT,
    ts      INTEGER,
    vbuy    REAL DEFAULT 0,
    vsell   REAL DEFAULT 0,
    nb      INTEGER DEFAULT 0,
    ns      INTEGER DEFAULT 0,
    nbu     INTEGER DEFAULT 0,
    nse     INTEGER DEFAULT 0,
    maxbuy  REAL DEFAULT 0,
    streak  INTEGER DEFAULT 0,
    streakv REAL DEFAULT 0,
    px      REAL,
    liq     REAL,
    PRIMARY KEY (pool, ts)
);
CREATE INDEX IF NOT EXISTS idx_buckets_ts ON buckets(ts);
CREATE TABLE IF NOT EXISTS scanner_state (
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE TABLE IF NOT EXISTS discoveries (
    address       TEXT PRIMARY KEY,
    protocol      TEXT NOT NULL,
    token0        TEXT,
    token1        TEXT,
    base_token    TEXT,
    block_number  INTEGER NOT NULL,
    tx_hash       TEXT,
    creator       TEXT,
    discovered_at REAL NOT NULL,
    last_enriched REAL
);
CREATE INDEX IF NOT EXISTS idx_discoveries_enrich
    ON discoveries(last_enriched, block_number);
CREATE TABLE IF NOT EXISTS features (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    address   TEXT,
    ts        REAL,
    payload   TEXT
);
CREATE INDEX IF NOT EXISTS idx_features_addr_ts ON features(address, ts);
"""


class Store:
    def __init__(self, path: str = None):
        self.path = path or config.DB_PATH
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self):
        """CREATE TABLE IF NOT EXISTS will not add columns to an existing db,
        so an upgraded bot pointed at an old signals.db needs this."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(pools)")}
        for col, decl in (("creator", "TEXT"),):
            if col not in have:
                log.info("migrating: adding pools.%s", col)
                self.db.execute("ALTER TABLE pools ADD COLUMN %s %s" % (col, decl))
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(tokens)")}
        for col, decl in (("supply", "REAL"),):
            if col not in have:
                log.info("migrating: adding tokens.%s", col)
                self.db.execute("ALTER TABLE tokens ADD COLUMN %s %s" % (col, decl))
        self.db.commit()

        # Seed the append-only log from the latest-per-kind table so the call
        # counter does not restart from zero on an existing database. Price is
        # unknown for these, which format_history handles.
        n = self.db.execute("SELECT COUNT(*) c FROM alert_log").fetchone()["c"]
        if not n:
            cur = self.db.execute(
                "SELECT a.address, p.base_token, a.kind, a.sent_at, a.score"
                " FROM alerts a LEFT JOIN pools p ON p.address = a.address")
            rows = [(r["address"], (r["base_token"] or "").lower(), r["kind"],
                     r["sent_at"], r["score"], None) for r in cur.fetchall()]
            if rows:
                self.db.executemany(
                    "INSERT INTO alert_log (address, token, kind, ts, score, price)"
                    " VALUES (?,?,?,?,?,?)", rows)
                self.db.commit()
                log.info("migrating: seeded alert_log with %d prior alerts", len(rows))

    # -- pools -------------------------------------------------------------
    def seen_pool(self, address: str) -> bool:
        cur = self.db.execute("SELECT 1 FROM pools WHERE address = ?", (address.lower(),))
        return cur.fetchone() is not None

    def upsert_pool(self, p: dict) -> bool:
        """Insert or refresh indexer metadata; preserve local alert state."""
        addr = (p.get("address") or "").lower()
        if not addr:
            return False
        is_new = not self.seen_pool(addr)
        self.db.execute(
            "INSERT INTO pools (address, name, dex, base_token, created_at, first_seen)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(address) DO UPDATE SET"
            " name=COALESCE(excluded.name, pools.name),"
            " dex=COALESCE(excluded.dex, pools.dex),"
            " base_token=COALESCE(excluded.base_token, pools.base_token),"
            " created_at=COALESCE(excluded.created_at, pools.created_at)",
            (addr, p.get("name"), p.get("dex"), p.get("base_token"),
             p.get("created_at"), time.time()),
        )
        self.db.commit()
        return is_new

    # -- on-chain discovery queue ----------------------------------------
    def put_discovery(self, row: dict) -> bool:
        address = (row.get("pool") or "").lower()
        if not address:
            return False
        inserted = self.db.execute(
            "INSERT OR IGNORE INTO discoveries"
            " (address, protocol, token0, token1, base_token, block_number,"
            "  tx_hash, creator, discovered_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (address, row.get("protocol"), row.get("token0"), row.get("token1"),
             row.get("base_token"), row.get("block"), row.get("tx"),
             row.get("creator"), time.time()),
        ).rowcount > 0
        self.db.commit()
        return inserted

    def pending_discoveries(self, limit=30, max_age_h=6.0,
                            exclude_protocol=None, fresh_share=0.75):
        """Return a newest-first fast lane plus an oldest-first fairness lane.

        A single oldest-first queue lets a burst of pool creations delay a new
        launch for hours. Giving every cycle both lanes keeps current launches
        close to real time without permanently starving older discoveries.
        """
        limit = max(0, int(limit))
        if not limit:
            return []
        cutoff = time.time() - max_age_h * 3600
        columns = (
            "address, protocol, token0, token1, base_token, creator,"
            " block_number, discovered_at, last_enriched")
        where = "discovered_at >= ?"
        params = [cutoff]
        if exclude_protocol:
            where += " AND protocol <> ?"
            params.append(exclude_protocol)

        share = min(1.0, max(0.0, float(fresh_share)))
        fast_limit = min(limit, max(1, int(round(limit * share)))) if share else 0
        fast = []
        if fast_limit:
            cur = self.db.execute(
                "SELECT {} FROM discoveries WHERE {}"
                " AND last_enriched IS NULL"
                " ORDER BY block_number DESC LIMIT ?".format(columns, where),
                params + [fast_limit])
            fast = [dict(row) for row in cur.fetchall()]

        remaining = limit - len(fast)
        if remaining <= 0:
            return fast

        fair_where = where
        fair_params = list(params)
        if fast:
            placeholders = ",".join("?" for _ in fast)
            fair_where += " AND address NOT IN ({})".format(placeholders)
            fair_params.extend(row["address"] for row in fast)
        cur = self.db.execute(
            "SELECT {} FROM discoveries WHERE {}"
            " ORDER BY CASE WHEN last_enriched IS NULL THEN 0 ELSE 1 END,"
            " COALESCE(last_enriched, 0), block_number ASC LIMIT ?".format(
                columns, fair_where),
            fair_params + [remaining])
        return fast + [dict(row) for row in cur.fetchall()]

    def touch_discoveries(self, addresses):
        now = time.time()
        self.db.executemany(
            "UPDATE discoveries SET last_enriched = ? WHERE address = ?",
            [(now, address.lower()) for address in addresses])
        self.db.commit()

    def discovery(self, address):
        cur = self.db.execute(
            "SELECT address, protocol, token0, token1, base_token, creator,"
            " block_number, discovered_at"
            " FROM discoveries WHERE address = ?", (address.lower(),))
        row = cur.fetchone()
        return dict(row) if row else None

    def discoveries_by_protocol(self, protocol, max_age_h=6.0):
        cutoff = time.time() - max_age_h * 3600
        cur = self.db.execute(
            "SELECT address, protocol, token0, token1, base_token, creator,"
            " block_number, discovered_at FROM discoveries"
            " WHERE protocol = ? AND discovered_at >= ?",
            (protocol, cutoff))
        return {row["address"]: dict(row) for row in cur.fetchall()}

    def token_was_announced(self, token: str) -> bool:
        if not token:
            return False
        cur = self.db.execute(
            "SELECT 1 FROM alert_log WHERE token = ? AND kind = 'new_launch' LIMIT 1",
            (token.lower(),))
        return cur.fetchone() is not None

    def mark_announced(self, address: str):
        self.db.execute("UPDATE pools SET announced_new = 1 WHERE address = ?", (address.lower(),))
        self.db.commit()

    def was_announced(self, address: str) -> bool:
        cur = self.db.execute("SELECT announced_new FROM pools WHERE address = ?", (address.lower(),))
        row = cur.fetchone()
        return bool(row and row["announced_new"])

    def watchlist(self, limit: int = 120):
        """Recently seen pools worth re-checking each cycle."""
        cur = self.db.execute(
            "SELECT address FROM pools ORDER BY first_seen DESC LIMIT ?", (limit,))
        return [r["address"] for r in cur.fetchall()]

    # -- alerts ------------------------------------------------------------
    def last_alert(self, address: str, kind: str) -> Optional[sqlite3.Row]:
        cur = self.db.execute(
            "SELECT sent_at, score FROM alerts WHERE address = ? AND kind = ?",
            (address.lower(), kind))
        return cur.fetchone()

    def should_alert(self, address: str, kind: str, score: float) -> bool:
        row = self.last_alert(address, kind)
        if row is None:
            return True
        age_min = (time.time() - row["sent_at"]) / 60.0
        if age_min >= config.COOLDOWN_MINUTES:
            return True
        # Still cooling down, but let a materially stronger move through.
        return score >= (row["score"] + config.RE_ALERT_SCORE_DELTA)

    def record_alert(self, address: str, kind: str, score: float):
        self.db.execute(
            "INSERT INTO alerts (address, kind, sent_at, score) VALUES (?,?,?,?)"
            " ON CONFLICT(address, kind) DO UPDATE SET sent_at=excluded.sent_at, score=excluded.score",
            (address.lower(), kind, time.time(), score))
        self.db.commit()

    # -- alert history -----------------------------------------------------
    def log_alert(self, address, token, kind, score, price):
        """Append-only. The `alerts` table only keeps the latest per kind, so
        it cannot answer "how many times have we called this token"."""
        self.db.execute(
            "INSERT INTO alert_log (address, token, kind, ts, score, price)"
            " VALUES (?,?,?,?,?,?)",
            (address.lower(), (token or "").lower(), kind, time.time(),
             score, price))
        self.db.commit()

    def alert_history(self, address, token=None):
        """Counts and the first call, so an alert can show whether earlier
        calls on this token actually went anywhere."""
        if token:
            cur = self.db.execute(
                "SELECT kind, ts, score, price FROM alert_log"
                " WHERE token = ? OR address = ? ORDER BY ts",
                (token.lower(), address.lower()))
        else:
            cur = self.db.execute(
                "SELECT kind, ts, score, price FROM alert_log"
                " WHERE address = ? ORDER BY ts", (address.lower(),))
        rows = cur.fetchall()
        if not rows:
            return {"count": 0, "by_kind": {}, "first_ts": None,
                    "first_price": None, "last_ts": None}
        by_kind = {}
        for r in rows:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        first = rows[0]
        return {"count": len(rows), "by_kind": by_kind,
                "first_ts": first["ts"], "first_price": first["price"],
                "last_ts": rows[-1]["ts"]}

    # -- feature log -------------------------------------------------------
    def log_features(self, address: str, payload: dict):
        """Snapshot every scored pool so the weights can be fitted later."""
        self.db.execute(
            "INSERT INTO features (address, ts, payload) VALUES (?,?,?)",
            (address.lower(), time.time(), json.dumps(payload, separators=(",", ":"))))
        self.db.commit()

    def prune(self, days: int = 30):
        cutoff = time.time() - days * 86400
        self.db.execute("DELETE FROM features WHERE ts < ?", (cutoff,))
        self.db.commit()

    # -- safety cache ------------------------------------------------------
    def get_safety(self, token: str, max_age_h: float = 6.0):
        cur = self.db.execute(
            "SELECT checked_at, payload FROM safety WHERE token = ?", (token.lower(),))
        row = cur.fetchone()
        if not row:
            return None
        if (time.time() - row["checked_at"]) > max_age_h * 3600:
            return None
        try:
            return json.loads(row["payload"])
        except (ValueError, TypeError):
            return None

    def put_safety(self, token: str, s: dict):
        self.db.execute(
            "INSERT INTO safety (token, checked_at, can_sell, tax_pct, impact_pct, payload)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(token) DO UPDATE SET checked_at=excluded.checked_at,"
            " can_sell=excluded.can_sell, tax_pct=excluded.tax_pct,"
            " impact_pct=excluded.impact_pct, payload=excluded.payload",
            (token.lower(), time.time(),
             1 if s.get("can_sell") else (0 if s.get("can_sell") is False else None),
             s.get("tax_pct"), s.get("impact_pct"), json.dumps(s)))
        self.db.commit()

    # -- on-chain buckets --------------------------------------------------
    def merge_buckets(self, rows):
        """rows: (pool, ts, vbuy, vsell, nb, ns, nbu, nse, maxbuy, streak,
        streakv, px, liq). A bucket can be written twice as it fills, so
        additive fields accumulate and point-in-time fields overwrite."""
        self.db.executemany(
            "INSERT INTO buckets (pool, ts, vbuy, vsell, nb, ns, nbu, nse,"
            " maxbuy, streak, streakv, px, liq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(pool, ts) DO UPDATE SET"
            "  vbuy = vbuy + excluded.vbuy,"
            "  vsell = vsell + excluded.vsell,"
            "  nb = nb + excluded.nb,"
            "  ns = ns + excluded.ns,"
            "  nbu = MAX(nbu, excluded.nbu),"
            "  nse = MAX(nse, excluded.nse),"
            "  maxbuy = MAX(maxbuy, excluded.maxbuy),"
            "  streak = MAX(streak, excluded.streak),"
            "  streakv = MAX(streakv, excluded.streakv),"
            "  px = excluded.px,"
            "  liq = excluded.liq",
            rows)
        self.db.commit()

    def bucket_window(self, pool, since_ts):
        cur = self.db.execute(
            "SELECT ts, vbuy, vsell, nb, ns, nbu, nse, maxbuy, streak, streakv,"
            " px, liq FROM buckets WHERE pool = ? AND ts >= ? ORDER BY ts",
            (pool.lower(), int(since_ts)))
        return [dict(r) for r in cur.fetchall()]

    def bucket_windows(self, since_ts):
        """All pools at once -- one query per tick instead of one per pool."""
        cur = self.db.execute(
            "SELECT pool, ts, vbuy, vsell, nb, ns, nbu, nse, maxbuy, streak,"
            " streakv, px, liq FROM buckets WHERE ts >= ? ORDER BY pool, ts",
            (int(since_ts),))
        out = {}
        for r in cur.fetchall():
            out.setdefault(r["pool"], []).append(dict(r))
        return out

    def prune_buckets(self, hours=48):
        self.db.execute("DELETE FROM buckets WHERE ts < ?",
                        (int(time.time() - hours * 3600),))
        self.db.commit()

    def get_state(self, k, default=None):
        cur = self.db.execute("SELECT v FROM scanner_state WHERE k = ?", (k,))
        row = cur.fetchone()
        return row["v"] if row else default

    def set_state(self, k, v):
        self.db.execute(
            "INSERT INTO scanner_state (k, v) VALUES (?,?)"
            " ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, str(v)))
        self.db.commit()

    def pool_meta(self, address):
        cur = self.db.execute(
            "SELECT address, name, dex, base_token, created_at, first_seen"
            " FROM pools WHERE address = ?", (address.lower(),))
        row = cur.fetchone()
        return dict(row) if row else None

    def pool_map(self, wrapped_native, limit=400):
        """pool -> is wrapped native token0, needed to read Swap amounts."""
        cur = self.db.execute(
            "SELECT address, base_token FROM pools WHERE base_token IS NOT NULL"
            " ORDER BY first_seen DESC LIMIT ?", (limit,))
        w = wrapped_native.lower()
        return {r["address"]: (w < (r["base_token"] or "").lower())
                for r in cur.fetchall()}

    def tracked_pools(self, limit=400):
        cur = self.db.execute(
            "SELECT address FROM pools ORDER BY first_seen DESC LIMIT ?", (limit,))
        return [r["address"] for r in cur.fetchall()]

    # -- token identity ----------------------------------------------------
    @staticmethod
    def _norm(x):
        return "".join(ch for ch in (x or "").lower() if ch.isalnum())

    def put_token(self, address, symbol, name):
        self.db.execute(
            "INSERT INTO tokens (address, symbol, name, symbol_norm, name_norm, first_seen)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(address) DO UPDATE SET symbol=excluded.symbol,"
            " name=excluded.name, symbol_norm=excluded.symbol_norm,"
            " name_norm=excluded.name_norm",
            (address.lower(), symbol, name, self._norm(symbol), self._norm(name),
             time.time()))
        self.db.commit()

    def set_supply(self, address, supply):
        self.db.execute("UPDATE tokens SET supply = ? WHERE address = ?",
                        (float(supply), address.lower()))
        self.db.commit()

    def get_token(self, address):
        cur = self.db.execute(
            "SELECT symbol, name, supply FROM tokens WHERE address = ?",
            (address.lower(),))
        row = cur.fetchone()
        return dict(row) if row else None

    def copycats(self, address, symbol, name):
        """Earlier tokens sharing this ticker or name.

        Ticker collision is the norm on HyperEVM -- EGG spawned EGGCAT and a
        second 'Egg' within hours -- so knowing an earlier token already used
        the name is worth surfacing.
        """
        a = address.lower()
        sn, nn = self._norm(symbol), self._norm(name)
        out = {"same_symbol": 0, "same_name": 0, "example": None}
        # Count every other token sharing the identity. Ordering by our own
        # first_seen would be misleading: bulk-seeded rows all share a
        # timestamp, so "earlier" says nothing about which launched first.
        if sn:
            cur = self.db.execute(
                "SELECT address, name FROM tokens WHERE symbol_norm = ? AND address <> ?",
                (sn, a))
            rows = cur.fetchall()
            out["same_symbol"] = len(rows)
            if rows:
                out["example"] = rows[0]["address"]
        if nn and len(nn) >= 3:
            cur = self.db.execute(
                "SELECT COUNT(*) n FROM tokens WHERE name_norm = ? AND address <> ?",
                (nn, a))
            out["same_name"] = cur.fetchone()["n"]
        return out

    # -- launch metadata (immutable, fetched once) -------------------------
    def get_launch_meta(self, token: str):
        cur = self.db.execute(
            "SELECT cid, website, twitter, telegram, description FROM launch_meta"
            " WHERE token = ?", (token.lower(),))
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)

    def put_launch_meta(self, token: str, cid, m: dict):
        m = m or {}
        self.db.execute(
            "INSERT INTO launch_meta (token, cid, website, twitter, telegram,"
            " description, fetched_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(token) DO UPDATE SET cid=excluded.cid,"
            " website=excluded.website, twitter=excluded.twitter,"
            " telegram=excluded.telegram, description=excluded.description,"
            " fetched_at=excluded.fetched_at",
            (token.lower(), cid, m.get("website", ""), m.get("twitter", ""),
             m.get("telegram", ""), m.get("description", ""), time.time()))
        self.db.commit()

    # -- creator reputation ------------------------------------------------
    def set_creator(self, address: str, creator: str):
        self.db.execute("UPDATE pools SET creator = ? WHERE address = ?",
                        (creator.lower(), address.lower()))
        self.db.commit()

    def creator_stats(self, creator: str):
        """How this deployer's previous pools scored on the safety check."""
        if not creator:
            return None
        cur = self.db.execute(
            "SELECT COUNT(*) n,"
            " SUM(CASE WHEN s.can_sell = 0 THEN 1 ELSE 0 END) honeypots"
            " FROM pools p LEFT JOIN safety s ON s.token = p.base_token"
            " WHERE p.creator = ?", (creator.lower(),))
        row = cur.fetchone()
        if not row or not row["n"]:
            return None
        return {"pools": row["n"], "honeypots": row["honeypots"] or 0}

    def close(self):
        self.db.close()
