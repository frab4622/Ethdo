"""
Polymarket BTC 5-Min Copytrade v2 - PAPER (custom for 0x63ce342161250d705dc0b16df89036c8e5f9ba9a)
Copy EVERY BTC 5-min BUY where price 0.10 < p < 0.90 and his cash >= $1.
Bet scaling: <=$1 → $1 | $1-$10 → mirror exact | >$10 → cap $5
No bets over $5. Poll every 3s.
"""
import os
import sys, json, time, math, asyncio, logging, traceback
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("copyv2-paper-custom")

GAMMA = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com")
CLOB = os.getenv("CLOB_API", "https://clob.polymarket.com")
DATA_API = os.getenv("DATA_API", "https://data-api.polymarket.com")

TARGET = os.getenv("COPY_TARGET", "0x63ce342161250d705dc0b16df89036c8e5f9ba9a")
POLL = float(os.getenv("POLL_INTERVAL", "3.0"))
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")

daily_pnl = 0.0
wins = 0
losses = 0
copied = 0
seen_ids = set()
open_positions = {}
proxy_wallet = None


def trunc2(v):
    return float(int(v * 100)) / 100.0


def trunc4(v):
    return float(int(v * 10000)) / 10000.0


def init_clob():
    log.info("PAPER MODE - no real orders")
    return True


def do_buy(tid, amt, price):
    log.info("PAPER BUY: $%.2f @ $%.3f (token %s)", amt, price, tid)
    return True


def scale_bet(target_cash):
    """Custom scaling: <=1 → 1 | 1-10 → mirror | >10 → 5"""
    if target_cash <= 1:
        return 1.0
    elif target_cash <= 10:
        return trunc2(target_cash)  # mirror exact, rounded to 2 decimals
    else:
        return 5.0


async def get_price(session, tid):
    try:
        async with session.get(CLOB + "/price", params={"token_id": tid, "side": "BUY"}) as r:
            if r.status == 200:
                d = await r.json()
                if d.get("price"):
                    return float(d["price"])
    except Exception:
        pass
    try:
        async with session.get(CLOB + "/book", params={"token_id": tid}) as r:
            if r.status == 200:
                b = await r.json()
                a = b.get("asks", [])
                if a:
                    return float(a[0]["price"])
    except Exception:
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
        except Exception as e:
            log.debug("fetch %s: %s", ep, e)
    return None, None


async def discover_proxy(session):
    global proxy_wallet
    for addr in [TARGET, TARGET.lower()]:
        for path in ["/profiles/" + addr, "/profiles?address=" + addr]:
            try:
                async with session.get(GAMMA + path) as r:
                    if r.status == 200:
                        data = await r.json()
                        if isinstance(data, list) and data:
                            data = data[0]
                        if isinstance(data, dict):
                            pw = data.get("proxyWallet") or data.get("proxy") or data.get("address")
                            if pw and pw.lower() != TARGET.lower():
                                proxy_wallet = pw
                                log.info("DISCOVERED proxy: %s", pw)
                                return pw
            except Exception:
                pass
    return None


async def fetch_all_trades(session):
    results = []
    addrs = [TARGET, TARGET.lower()]
    if proxy_wallet:
        addrs.append(proxy_wallet)
        addrs.append(proxy_wallet.lower())

    for addr in addrs:
        try:
            async with session.get(DATA_API + "/trades",
                                   params={"user": addr, "limit": 50, "takerOnly": "true"}) as r:
                if r.status == 200:
                    data = await r.json()
                    if data and len(data) > len(results):
                        results = data
        except Exception:
            pass

        try:
            async with session.get(DATA_API + "/activity",
                                   params={"user": addr, "limit": 50}) as r:
                if r.status == 200:
                    data = await r.json()
                    for item in data:
                        tx = item.get("transactionHash") or item.get("id") or item.get("hash", "")
                        if not tx:
                            continue
                        if any(t.get("transactionHash") == tx for t in results):
                            continue
                        results.append({
                            "transactionHash": tx,
                            "eventSlug": item.get("eventSlug", item.get("slug", "")),
                            "slug": item.get("slug", ""),
                            "side": item.get("side", ""),
                            "outcome": item.get("outcome", ""),
                            "price": item.get("price", 0),
                            "size": item.get("size", item.get("tokens", 0)),
                            "asset": item.get("asset", item.get("conditionId", "")),
                            "type": item.get("type", "trade"),
                        })
        except Exception:
            pass

    return results


async def tg(session, msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        await session.post(
            "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN,
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"})
    except Exception:
        pass


def stats_str():
    total = wins + losses
    rate = (wins / total * 100) if total > 0 else 0
    return "Copied %d | W%d L%d (%.0f%%) | PnL $%.2f" % (copied, wins, losses, rate, daily_pnl)


async def check_resolutions(session):
    global daily_pnl, wins, losses
    now_ts = int(time.time())
    resolved = []
    res_msgs = []
    for tx, pos in open_positions.items():
        if now_ts < pos["window_ts"] + 308:
            continue
        fp = await get_price(session, pos["token_id"])
        if fp is None and now_ts < pos["window_ts"] + 60:
            continue
        bet = pos["bet"]
        if fp is not None and fp > 0.90:
            shares = bet / pos["price"]
            profit = (shares * 0.98) - bet
            wins += 1
            result = "WIN"
        elif fp is not None and fp < 0.10:
            profit = -bet
            losses += 1
            result = "LOSS"
        else:
            profit = -bet
            losses += 1
            result = "LOSS"
        daily_pnl += profit
        resolved.append(tx)
        log.info("%s %s $%.2f @ $%.3f | $%.2f | %s",
                 result, pos["side"], bet, pos["price"], profit, stats_str())
        res_msgs.append("%s %s $%.2f -> $%.2f" % (result, pos["side"], bet, profit))
    for tx in resolved:
        del open_positions[tx]
    if res_msgs:
        msg = "<b>Resolved</b>\n" + "\n".join("- " + r for r in res_msgs) + "\n\n" + stats_str()
        await tg(session, msg)


async def main():
    global copied, proxy_wallet

    log.info("=" * 55)
    log.info("  BTC Copytrade PAPER - custom scaling")
    log.info("  Target: %s", TARGET)
    log.info("  Copy BUY only if 0.10 < price < 0.90 and his cash >=1")
    log.info("  Bet: mirror 1-10, cap 5, min 1 | Poll %.0fs", POLL)
    log.info("=" * 55)

    init_clob()

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=300, connect=30, sock_read=60),
        connector=aiohttp.TCPConnector(keepalive_timeout=120, limit=20),
    ) as session:
        await discover_proxy(session)

        proxy_str = proxy_wallet if proxy_wallet else "not found"
        await tg(session,
                "PAPER Custom LIVE\nTarget: %s\nProxy: %s\nBet mirror 1-10 cap $5 | Poll %.0fs" % (
                    TARGET[:10] + "..." + TARGET[-6:],
                    proxy_str[:10] + "..." + proxy_str[-6:] if proxy_wallet else "none",
                    POLL))

        ll = 0
        api_hits = 0
        conn_errors = 0

        seen_ids.clear()

        while True:
            try:
                nt = int(time.time())

                await check_resolutions(session)

                trades = await fetch_all_trades(session)
                api_hits += 1

                new_count = 0
                cycle_actions = []

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

                    side = trade.get("side", "").upper()
                    ttype = trade.get("type", "trade").lower()
                    if side != "BUY" and "buy" not in ttype:
                        seen_ids.add(uid)
                        continue

                    seen_ids.add(uid)
                    new_count += 1

                    if "up" in outcome or "yes" in outcome:
                        pick_side = "UP"
                    elif "down" in outcome or "no" in outcome:
                        pick_side = "DOWN"
                    else:
                        log.info("Unknown outcome: %s | %s", outcome, event_slug)
                        continue

                    try:
                        trade_ts = int(event_slug.split("-")[-1])
                    except (ValueError, IndexError):
                        trade_ts = nt - (nt % 300)

                    if nt > trade_ts + 295:
                        log.info("SKIP expired: %s %s", pick_side, event_slug)
                        continue

                    target_price = float(trade.get("price", 0) or 0)
                    target_size = float(trade.get("size", 0) or 0)
                    target_cash = target_size * target_price

                    if target_cash < 1:
                        log.info("SKIP small bet: tgt cash $%.2f < $1 | %s", target_cash, event_slug)
                        continue

                    market_slug = "btc-updown-5m-%d" % trade_ts
                    uid_tok, did_tok = await fetch_market(session, market_slug)
                    if not uid_tok or not did_tok:
                        log.error("No market %s", market_slug)
                        continue

                    buy_tid = uid_tok if pick_side == "UP" else did_tok
                    cur_price = await get_price(session, buy_tid)
                    if cur_price is None or cur_price <= 0.01:
                        log.error("No price for %s", pick_side)
                        continue

                    if cur_price <= 0.10 or cur_price >= 0.90:
                        log.info("SKIP out-of-range price: %s @ $%.3f (tgt $%.2f) | %s",
                                 pick_side, cur_price, target_cash, event_slug)
                        continue

                    bet_amount = scale_bet(target_cash)

                    log.info("COPY: %s $%.2f @ $%.3f | Target: $%.2f ($%.1f cash) | %s",
                             pick_side, bet_amount, cur_price, target_price, target_cash, market_slug)

                    ok = do_buy(buy_tid, bet_amount, cur_price)
                    if ok:
                        copied += 1
                        open_positions[uid] = {
                            "side": pick_side,
                            "price": cur_price,
                            "bet": bet_amount,
                            "token_id": buy_tid,
                            "window_ts": trade_ts,
                        }
                        cycle_actions.append("%s $%.2f@$%.3f (tgt $%.1f)" % (
                            pick_side, bet_amount, cur_price, target_cash))
                    else:
                        cycle_actions.append("FAIL %s $%.2f@$%.3f" % (pick_side, bet_amount, cur_price))

                if cycle_actions:
                    msg = "<b>Cycle update</b>\n" + "\n".join("- " + a for a in cycle_actions) + "\n" + stats_str()
                    await tg(session, msg)

                if len(seen_ids) > 1000:
                    seen_ids.clear()

                if nt - ll >= 20:
                    remaining = 300 - (nt % 300)
                    log.info("Poll #%d | %d trades fetched | %d new | Open: %d | Proxy: %s | %s",
                             api_hits, len(trades), new_count, len(open_positions),
                             "YES" if proxy_wallet else "NO", stats_str())
                    if not proxy_wallet and api_hits % 100 == 0:
                        await discover_proxy(session)
                    ll = nt

                conn_errors = 0
                await asyncio.sleep(POLL)

            except KeyboardInterrupt:
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, OSError) as e:
                conn_errors += 1
                log.warning("Conn #%d: %s", conn_errors, e)
                if conn_errors >= 3:
                    init_clob()
                    conn_errors = 0
                await asyncio.sleep(10)
            except Exception as e:
                log.error("Error: %s", e)
                traceback.print_exc()
                await asyncio.sleep(10)


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error("FATAL: %s - restart 15s", e)
            traceback.print_exc()
            time.sleep(15)
