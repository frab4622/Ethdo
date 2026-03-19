"""
Polymarket BTC 5-Min Copytrade v2 - PAPER (FIXED)

Target: 0x63ce342161250d705dc0b16df89036c8e5f9ba9a

Rules:
- Copy buys ONLY
- Skip price <0.10 or >0.90
- Bet based on PRICE:
    0.10 → $1
    0.90 → $5
- NO REAL ORDERS (paper only)
"""

import os
_proxy = os.getenv("PROXY_URL", "")
if _proxy:
    os.environ["HTTP_PROXY"] = _proxy
    os.environ["HTTPS_PROXY"] = _proxy
    os.environ["ALL_PROXY"] = _proxy
    os.environ["http_proxy"] = _proxy
    os.environ["https_proxy"] = _proxy
    os.environ["all_proxy"] = _proxy

import sys, json, time, math, asyncio, logging, traceback
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("paperv2")

GAMMA = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com")
CLOB = os.getenv("CLOB_API", "https://clob.polymarket.com")
DATA_API = os.getenv("DATA_API", "https://data-api.polymarket.com")

TARGET = os.getenv("COPY_TARGET", "0x63ce342161250d705dc0b16df89036c8e5f9ba9a")
POLL = float(os.getenv("POLL_INTERVAL", "3.0"))

daily_pnl = 0.0
wins = 0
losses = 0
copied = 0
seen_ids = set()
open_positions = {}
proxy_wallet = None


# ✅ PRICE-BASED SCALING (FIXED)
def scale_bet_from_price(price):
    if price < 0.10 or price > 0.90:
        return None
    bet = 1 + ((price - 0.10) / 0.80) * 4
    return round(min(max(bet, 1), 5), 2)


async def get_price(session, tid):
    try:
        async with session.get(CLOB + "/price", params={"token_id": tid, "side": "BUY"}) as r:
            if r.status == 200:
                d = await r.json()
                if d.get("price"):
                    return float(d["price"])
    except:
        pass
    try:
        async with session.get(CLOB + "/book", params={"token_id": tid}) as r:
            if r.status == 200:
                b = await r.json()
                a = b.get("asks", [])
                if a:
                    return float(a[0]["price"])
    except:
        pass
    return None


async def fetch_market(session, slug):
    for ep in ["/events", "/markets"]:
        try:
            async with session.get(GAMMA + ep, params={"slug": slug}) as r:
                if r.status != 200:
                    continue
                raw = await r.json()
                item = raw[0] if isinstance(raw, list) and raw else raw
                if not isinstance(item, dict):
                    continue
                if "markets" in item and item["markets"]:
                    item = item["markets"][0]

                cr = item.get("clobTokenIds", [])
                if isinstance(cr, str):
                    cr = json.loads(cr)

                outr = item.get("outcomes", [])
                if isinstance(outr, str):
                    outr = json.loads(outr)

                if len(cr) < 2:
                    continue

                uid = did = ""
                for i, out in enumerate(outr):
                    o = str(out).lower()
                    if "up" in o or "yes" in o:
                        uid = cr[i]
                    elif "down" in o or "no" in o:
                        did = cr[i]

                if not uid:
                    uid, did = cr[0], cr[1]

                return uid, did
        except:
            pass
    return None, None


async def fetch_all_trades(session):
    results = []
    addrs = [TARGET, TARGET.lower()]
    if proxy_wallet:
        addrs += [proxy_wallet, proxy_wallet.lower()]

    for addr in addrs:
        try:
            async with session.get(DATA_API + "/trades",
                                   params={"user": addr, "limit": 50, "takerOnly": "true"}) as r:
                if r.status == 200:
                    data = await r.json()
                    if data and len(data) > len(results):
                        results = data
        except:
            pass
    return results


def stats_str():
    total = wins + losses
    rate = (wins / total * 100) if total > 0 else 0
    return f"Copied {copied} | W{wins} L{losses} ({rate:.0f}%) | PnL ${daily_pnl:.2f}"


async def check_resolutions(session):
    global daily_pnl, wins, losses
    now_ts = int(time.time())

    for tx, pos in list(open_positions.items()):
        if now_ts < pos["window_ts"] + 308:
            continue

        fp = await get_price(session, pos["token_id"])
        if fp is None:
            continue

        bet = pos["bet"]

        if fp > 0.90:
            shares = bet / pos["price"]
            profit = (shares * 0.98) - bet
            wins += 1
            result = "WIN"
        else:
            profit = -bet
            losses += 1
            result = "LOSS"

        daily_pnl += profit

        log.info("%s %s $%.2f @ $%.3f | profit $%.2f | %s",
                 result, pos["side"], bet, pos["price"], profit, stats_str())

        del open_positions[tx]


async def main():
    global copied

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await check_resolutions(session)

                trades = await fetch_all_trades(session)

                for trade in trades:
                    tx = trade.get("transactionHash", "") or ""
                    outcome = (trade.get("outcome", "") or "").lower()
                    uid = tx + "|" + outcome

                    if not tx or uid in seen_ids:
                        continue

                    event_slug = trade.get("eventSlug", "") or trade.get("slug", "")
                    if "btc-updown-5m" not in event_slug:
                        seen_ids.add(uid)
                        continue

                    side = trade.get("side", "")
                    if "buy" not in side.lower():
                        seen_ids.add(uid)
                        continue

                    seen_ids.add(uid)

                    price = float(trade.get("price", 0))

                    # ✅ FILTER
                    if price < 0.10 or price > 0.90:
                        continue

                    bet_amount = scale_bet_from_price(price)
                    if bet_amount is None:
                        continue

                    try:
                        trade_ts = int(event_slug.split("-")[-1])
                    except:
                        trade_ts = int(time.time())

                    market_slug = f"btc-updown-5m-{trade_ts}"
                    uid_tok, did_tok = await fetch_market(session, market_slug)
                    if not uid_tok or not did_tok:
                        continue

                    pick_side = "UP" if "up" in outcome else "DOWN"
                    buy_tid = uid_tok if pick_side == "UP" else did_tok

                    log.info("PAPER COPY: %s $%.2f @ $%.3f | %s",
                             pick_side, bet_amount, price, market_slug)

                    open_positions[uid] = {
                        "side": pick_side,
                        "price": price,
                        "bet": bet_amount,
                        "token_id": buy_tid,
                        "window_ts": trade_ts,
                    }

                    copied += 1

                await asyncio.sleep(POLL)

            except Exception as e:
                log.error("Error: %s", e)
                traceback.print_exc()
                await asyncio.sleep(5)


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except:
            time.sleep(10)
