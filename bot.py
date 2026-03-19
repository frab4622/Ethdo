"""
Polymarket BTC 5-Min Copytrade v2 - PAPER (PRICE SCALED)

Target: 0x63ce342161250d705dc0b16df89036c8e5f9ba9a

Rules:
- Copy buys ONLY
- Skip price <0.10 or >0.90
- Bet size based on PRICE:
    $0.10 → $1
    $0.90 → $5
- Linear scale between
- No real orders (paper only)
"""

import os
import sys, json, time, math, asyncio, logging, traceback
from datetime import datetime

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("paperbot")

GAMMA = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com")
CLOB = os.getenv("CLOB_API", "https://clob.polymarket.com")
DATA_API = os.getenv("DATA_API", "https://data-api.polymarket.com")

TARGET = "0x63ce342161250d705dc0b16df89036c8e5f9ba9a"
POLL = 3.0

# 📊 Stats
daily_pnl = 0.0
wins = 0
losses = 0
copied = 0

seen_ids = set()
open_positions = {}


# ✅ PRICE-BASED SCALING
def scale_bet_from_price(price):
    # clamp just in case
    price = max(0.10, min(price, 0.90))

    # linear mapping: 0.10 → 1, 0.90 → 5
    bet = 1 + ((price - 0.10) / 0.80) * 4

    return round(bet, 2)


async def get_price(session, tid):
    try:
        async with session.get(CLOB + "/price", params={"token_id": tid, "side": "BUY"}) as r:
            if r.status == 200:
                d = await r.json()
                if d.get("price"):
                    return float(d["price"])
    except:
        pass
    return None


async def fetch_market(session, slug):
    try:
        async with session.get(GAMMA + "/events", params={"slug": slug}) as r:
            data = await r.json()
            item = data[0]
            cr = json.loads(item["clobTokenIds"])
            outr = json.loads(item["outcomes"])

            uid = did = ""
            for i, out in enumerate(outr):
                if "up" in out.lower():
                    uid = cr[i]
                elif "down" in out.lower():
                    did = cr[i]

            return uid, did
    except:
        return None, None


async def fetch_all_trades(session):
    try:
        async with session.get(DATA_API + "/trades", params={"user": TARGET, "limit": 50}) as r:
            return await r.json()
    except:
        return []


def stats():
    total = wins + losses
    wr = (wins / total * 100) if total else 0
    return f"Copied {copied} | W{wins} L{losses} ({wr:.0f}%) | PnL ${daily_pnl:.2f}"


async def check_resolutions(session):
    global daily_pnl, wins, losses

    now = int(time.time())

    for tx, pos in list(open_positions.items()):
        if now < pos["ts"] + 300:
            continue

        price = await get_price(session, pos["token_id"])
        if price is None:
            continue

        bet = pos["bet"]

        if price > 0.90:
            shares = bet / pos["entry"]
            profit = (shares * 0.98) - bet
            wins += 1
            result = "WIN"
        else:
            profit = -bet
            losses += 1
            result = "LOSS"

        daily_pnl += profit

        log.info("%s %s $%.2f → $%.2f | %s",
                 result, pos["side"], bet, profit, stats())

        del open_positions[tx]


async def main():
    global copied

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await check_resolutions(session)

                trades = await fetch_all_trades(session)

                for trade in trades:
                    tx = trade.get("transactionHash", "")
                    if tx in seen_ids:
                        continue
                    seen_ids.add(tx)

                    if "buy" not in trade.get("side", "").lower():
                        continue

                    price = float(trade.get("price", 0))

                    # 🚫 PRICE FILTER
                    if price < 0.10 or price > 0.90:
                        continue

                    bet = scale_bet_from_price(price)

                    slug = trade.get("eventSlug", "")
                    uid, did = await fetch_market(session, slug)
                    if not uid:
                        continue

                    side = "UP" if "up" in trade.get("outcome", "").lower() else "DOWN"
                    token = uid if side == "UP" else did

                    log.info("PAPER COPY: %s @ $%.3f → Bet $%.2f",
                             side, price, bet)

                    open_positions[tx] = {
                        "side": side,
                        "entry": price,
                        "bet": bet,
                        "token_id": token,
                        "ts": int(time.time())
                    }

                    copied += 1

                await asyncio.sleep(POLL)

            except Exception as e:
                log.error("Error: %s", e)
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
