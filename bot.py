#!/usr/bin/env python3
"""
Polymarket BTC 5-minute Up/Down -- late-entry PAPER trader.

Strategy
  * Every 5-minute window, wait until ENTRY_START_SEC seconds have elapsed
    (default 180 = 2 minutes left).
  * Between ENTRY_START_SEC and ENTRY_END_SEC, watch the best ask of Up and Down.
  * The first time either side's best ask is inside [PRICE_MIN, PRICE_MAX]
    (default 0.93-0.96) buy a fixed STAKE_USD of that side (one trade per window).
  * Hold to resolution. Winner pays $1/share, loser pays $0.

Data: Polymarket only (Gamma for discovery/resolution, CLOB for order books).
Mode: paper only. Fills are simulated by walking the real order book.
"""
import csv
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
WINDOW = 300


def env_f(name, default):
    return float(os.getenv(name, default))


ENTRY_START = env_f("ENTRY_START_SEC", 180)   # seconds into the window
ENTRY_END = env_f("ENTRY_END_SEC", 285)       # stop looking this late
PRICE_MIN = env_f("PRICE_MIN", 0.93)
PRICE_MAX = env_f("PRICE_MAX", 0.96)
STAKE_USD = env_f("STAKE_USD", 5.0)
FEE_RATE = env_f("FEE_RATE", 0.0)             # fraction of notional; set from Polymarket's fee schedule
POLL_SEC = env_f("POLL_SEC", 1.0)
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))

TRADE_FIELDS = ["slug", "side", "entry_ts", "elapsed_s", "avg_price", "shares",
                "cost_usd", "fee_usd", "winner", "payout_usd", "pnl_usd", "status"]
WINDOW_FIELDS = ["slug", "traded", "side", "up_ask_at_end", "down_ask_at_end",
                 "max_up_ask", "max_down_ask"]

log = logging.getLogger("bot")
session = requests.Session()
session.headers["User-Agent"] = "polybot-paper/0.1"


# ---------------------------------------------------------------- HTTP helpers
def get_json(url, params=None, timeout=8):
    r = session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def get_event(slug):
    data = get_json(f"{GAMMA}/events", {"slug": slug})
    return data[0] if data else None


def get_market(start):
    """Return {'slug','tokens':{'Up':id,'Down':id}} or None if not listed yet."""
    slug = f"btc-updown-5m-{start}"
    ev = get_event(slug)
    if not ev or not ev.get("markets"):
        return None
    m = ev["markets"][0]
    outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
    tokens = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
    return {"slug": slug, "tokens": dict(zip(outcomes, tokens))}


def get_asks(token_id):
    """Ask levels sorted cheapest first: [(price, size), ...]."""
    book = get_json(f"{CLOB}/book", {"token_id": token_id})
    asks = [(float(a["price"]), float(a["size"])) for a in book.get("asks", [])]
    return sorted(asks)


def get_winner(slug):
    """'Up'/'Down' once resolved, else None."""
    ev = get_event(slug)
    if not ev or not ev.get("markets"):
        return None
    m = ev["markets"][0]
    prices = json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices")
    outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
    if not prices:
        return None
    prices = [float(p) for p in prices]
    if max(prices) >= 0.99 and min(prices) <= 0.01:
        return outcomes[prices.index(max(prices))]
    return None


# ------------------------------------------------------------------ simulation
def simulate_fill(asks, usd):
    """Walk the book spending `usd`. Returns (avg_price, shares, spent) or None if depth is too thin."""
    remaining, shares, spent = usd, 0.0, 0.0
    for price, size in asks:
        level_usd = price * size
        take = min(remaining, level_usd)
        shares += take / price
        spent += take
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-6 or shares <= 0:
        return None
    return spent / shares, shares, spent


def in_range(price):
    return PRICE_MIN <= price <= PRICE_MAX


# ----------------------------------------------------------------------- files
def append_csv(path, fields, row):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(row)


class Stats:
    def __init__(self):
        self.n = self.wins = 0
        self.pnl = 0.0

    def add(self, win, pnl):
        self.n += 1
        self.wins += int(win)
        self.pnl += pnl

    def line(self):
        wr = self.wins / self.n * 100 if self.n else 0
        return f"trades={self.n} wins={self.wins} win_rate={wr:.1f}% pnl=${self.pnl:+.2f}"


# ------------------------------------------------------------------- main loop
def settle(open_trades, stats):
    now = time.time()
    for t in list(open_trades):
        if now < t["end_ts"] + 5 or now - t["last_check"] < 5:
            continue
        t["last_check"] = now
        try:
            winner = get_winner(t["slug"])
        except Exception as e:  # noqa: BLE001
            log.warning("resolution check failed for %s: %s", t["slug"], e)
            winner = None
        if winner is None:
            if now - t["end_ts"] > 1800:
                log.error("%s unresolved after 30 min, marking unresolved", t["slug"])
                append_csv(DATA_DIR / "trades.csv", TRADE_FIELDS, {**t["row"], "status": "unresolved"})
                open_trades.remove(t)
            continue
        row = t["row"]
        win = winner == row["side"]
        payout = row["shares"] if win else 0.0
        pnl = payout - row["cost_usd"] - row["fee_usd"]
        row.update(winner=winner, payout_usd=round(payout, 4), pnl_usd=round(pnl, 4), status="settled")
        append_csv(DATA_DIR / "trades.csv", TRADE_FIELDS, row)
        stats.add(win, pnl)
        open_trades.remove(t)
        log.info("SETTLED %s side=%s winner=%s pnl=$%+.3f | %s", t["slug"], row["side"], winner, pnl, stats.line())


def try_entry(cur, elapsed, open_trades):
    tokens = cur["market"]["tokens"]
    asks = {side: get_asks(tokens[side]) for side in ("Up", "Down")}
    best = {side: (a[0][0] if a else None) for side, a in asks.items()}
    for side, p in best.items():
        if p is not None:
            cur["max_ask"][side] = max(cur["max_ask"][side], p)
    cur["last_ask"] = best

    for side in ("Up", "Down"):
        p = best[side]
        if p is None or not in_range(p):
            continue
        fill = simulate_fill(asks[side], STAKE_USD)
        if fill is None:
            log.info("%s %s ask %.3f in range but book too thin for $%.2f", cur["market"]["slug"], side, p, STAKE_USD)
            return
        avg, shares, spent = fill
        if not in_range(avg) and avg > PRICE_MAX:
            log.info("%s %s avg fill %.3f above max, skipping", cur["market"]["slug"], side, avg)
            return
        fee = spent * FEE_RATE
        row = {"slug": cur["market"]["slug"], "side": side, "entry_ts": int(time.time()),
               "elapsed_s": round(elapsed, 1), "avg_price": round(avg, 4), "shares": round(shares, 4),
               "cost_usd": round(spent, 4), "fee_usd": round(fee, 4), "winner": "", "payout_usd": "",
               "pnl_usd": "", "status": "open"}
        open_trades.append({"slug": row["slug"], "end_ts": cur["start"] + WINDOW, "last_check": 0, "row": row})
        cur["traded"], cur["side"] = True, side
        log.info("PAPER BUY %s %s @ %.3f x %.2f shares ($%.2f) at t+%.0fs",
                 row["slug"], side, avg, shares, spent, elapsed)
        return


def close_window(cur):
    if cur["market"] is None:
        return
    la = cur.get("last_ask", {})
    append_csv(DATA_DIR / "windows.csv", WINDOW_FIELDS, {
        "slug": cur["market"]["slug"], "traded": cur["traded"], "side": cur.get("side", ""),
        "up_ask_at_end": la.get("Up"), "down_ask_at_end": la.get("Down"),
        "max_up_ask": cur["max_ask"]["Up"], "max_down_ask": cur["max_ask"]["Down"]})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log.info("PAPER MODE | stake=$%.2f range=%.2f-%.2f entry=%ds-%ds fee_rate=%.4f",
             STAKE_USD, PRICE_MIN, PRICE_MAX, ENTRY_START, ENTRY_END, FEE_RATE)
    be = (PRICE_MIN + PRICE_MAX) / 2
    log.info("At an average entry of %.3f you need a win rate above ~%.1f%% to break even", be, be * 100)

    stats, open_trades, cur = Stats(), [], None
    while True:
        try:
            now = time.time()
            start = int(now) // WINDOW * WINDOW
            elapsed = now - start

            if cur is None or cur["start"] != start:
                if cur:
                    close_window(cur)
                cur = {"start": start, "market": None, "traded": False,
                       "max_ask": {"Up": 0.0, "Down": 0.0}}

            settle(open_trades, stats)

            if elapsed < ENTRY_START - 30:
                time.sleep(min(5.0, max(0.5, ENTRY_START - 30 - elapsed)))
                continue

            if cur["market"] is None:
                cur["market"] = get_market(start)
                if cur["market"] is None:
                    log.warning("market btc-updown-5m-%d not found yet", start)
                    time.sleep(2)
                    continue

            if not cur["traded"] and ENTRY_START <= elapsed <= ENTRY_END:
                try_entry(cur, elapsed, open_trades)
        except KeyboardInterrupt:
            log.info("stopped. %s | open trades awaiting resolution: %d", stats.line(), len(open_trades))
            return
        except Exception as e:  # noqa: BLE001
            log.warning("loop error: %s", e)
            time.sleep(2)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
