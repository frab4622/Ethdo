"""
Polymarket BTC 5-Min Copytrade v2 - PAPER (CUR_PRICE)
"""

import os
_proxy = os.getenv("PROXY_URL", "")
if _proxy:
    os.environ["HTTP_PROXY"] = _proxy
    os.environ["HTTPS_PROXY"] = _proxy
    os.environ["ALL_PROXY"] = _proxy

import time, asyncio, logging
import aiohttp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("paper")

CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

TARGET = "0x63ce342161250d705dc0b16df89036c8e5f9ba9a"
POLL = 3

seen_ids = set()
positions = {}

def scale(price):
    if price < 0.10 or price > 0.90:
        return None
    return round(min(5, 1 + ((price - 0.10)/0.80)*4),2)

async def get_price(session, tid):
    try:
        async with session.get(CLOB+"/price", params={"token_id": tid}) as r:
            d = await r.json()
            return float(d["price"])
    except:
        return None

async def fetch(session):
    async with session.get(DATA_API+"/trades", params={"user": TARGET}) as r:
        return await r.json()

async def main():
    async with aiohttp.ClientSession() as s:
        while True:
            try:
                trades = await fetch(s)

                for t in trades:
                    tx = t.get("transactionHash")
                    if tx in seen_ids:
                        continue
                    seen_ids.add(tx)

                    if "buy" not in t.get("side",""):
                        continue

                    token = t.get("asset_id")
                    price = await get_price(s, token)
                    if not price:
                        continue

                    bet = scale(price)
                    if bet is None:
                        continue

                    log.info(f"PAPER BUY {bet} @ {price}")

                    positions[tx] = {"bet":bet, "entry":price, "ts":time.time(), "token":token}

                for tx, p in list(positions.items()):
                    if time.time() - p["ts"] < 300:
                        continue

                    price = await get_price(s, p["token"])
                    if not price:
                        continue

                    if price > 0.9:
                        profit = (p["bet"]/p["entry"])*0.98 - p["bet"]
                        log.info(f"WIN {profit}")
                    else:
                        log.info(f"LOSS {-p['bet']}")

                    del positions[tx]

                await asyncio.sleep(POLL)

            except Exception as e:
                log.error(e)
                await asyncio.sleep(5)

asyncio.run(main())
