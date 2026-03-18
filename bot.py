"""
Polymarket ETH 5-Min Safe Bet - PAPER
Analyze: T-150 to T-60 (build averages).
Entry: T-60 to close, if winning side is $0.70-$0.90 AND avg > $0.65, paper buy $10.
TP $0.97, SL $0.60. Simulated trades, no real money.
"""
import os

import sys, json, time, asyncio, logging, traceback
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv
from web3 import Web3


load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("eth_safebet")

GAMMA = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com")
CLOB = os.getenv("CLOB_API", "https://clob.polymarket.com")
RPC = os.getenv("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")

BET_SIZE = float(os.getenv("BET_SIZE", "10.0"))
ENTRY_MIN = float(os.getenv("ENTRY_MIN", "0.70"))
ENTRY_MAX = float(os.getenv("ENTRY_MAX", "0.90"))
AVG_MIN = float(os.getenv("AVG_MIN", "0.75"))
TP_PRICE = float(os.getenv("TP_PRICE", "0.99"))
SL_PRICE = float(os.getenv("SL_PRICE", "0.60"))
ANALYSIS_START = int(os.getenv("ANALYSIS_START_SECS", "150"))  # T minus
ANALYSIS_END = int(os.getenv("ANALYSIS_END_SECS", "30"))      # T minus
POLL = float(os.getenv("POLL_INTERVAL", "2.0"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS_USDC", "200.0"))
MAX_TRADES = int(os.getenv("MAX_TRADES_PER_DAY", "200"))
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")

daily_pnl = 0.0
trades_today = 0
last_reset = None
wins = 0
losses = 0
skips = 0

CL_ADDR = "0xF9680D99D6C9589e2a93a78A04A279e509205945"
CL_ABI = json.loads('[{"inputs":[],"name":"latestRoundData","outputs":[{"name":"roundId","type":"uint80"},{"name":"answer","type":"int256"},{"name":"startedAt","type":"uint256"},{"name":"updatedAt","type":"uint256"},{"name":"answeredInRound","type":"uint80"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"}]')




def trunc2(v):
    return float(int(v * 100)) / 100.0

def trunc4(v):
    return float(int(v * 10000)) / 10000.0


def init_clob():
    log.info("PAPER MODE")
    return True


def do_buy(tid, amt, price):
    shares = trunc4(amt / trunc2(price))
    log.info("PAPER BUY: %.4f @ $%.2f ($%.2f)", shares, price, amt)
    return True, shares


def do_sell(tid, shares_est):
    log.info("PAPER SELL: %.4f shares", shares_est)
    return True


def winfo():
    now = int(time.time())
    wt = now - (now % 300)
    return "eth-updown-5m-%d" % wt, wt, wt + 300


async def fetch(session, slug):
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


async def get_price(session, tid, side="BUY"):
    try:
        async with session.get(CLOB + "/price", params={"token_id": tid, "side": side}) as r:
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
                if side == "BUY":
                    a = b.get("asks", [])
                    if a:
                        return float(a[0]["price"])
                else:
                    bi = b.get("bids", [])
                    if bi:
                        return float(bi[0]["price"])
    except Exception:
        pass
    return None


async def tg(session, msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        await session.post(
            "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN,
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"})
    except Exception:
        pass


def risk_ok():
    global daily_pnl, trades_today, last_reset
    today = datetime.now(timezone.utc).date()
    if last_reset != today:
        daily_pnl = trades_today = 0
        last_reset = today
    return daily_pnl > -MAX_DAILY_LOSS and trades_today < MAX_TRADES


def stats_str():
    total = wins + losses
    rate = (wins / total * 100) if total > 0 else 0
    return "W%d L%d S%d (%.0f%%) | PnL $%.2f" % (wins, losses, skips, rate, daily_pnl)


async def cycle(session):
    global daily_pnl, trades_today, wins, losses, skips

    slug, ot, ct = winfo()
    end = datetime.fromtimestamp(ct, tz=timezone.utc)
    start = datetime.fromtimestamp(ot, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    remaining = (end - now).total_seconds()

    if remaining < ANALYSIS_END:
        await asyncio.sleep(remaining + 2)
        return

    uid, did = await fetch(session, slug)
    if not uid or not did:
        log.info("No market %s", slug)
        await asyncio.sleep(15)
        return

    if not risk_ok():
        log.warning("Risk limit | %s", stats_str())
        await asyncio.sleep(30)
        return

    # =============================================
    # Wait until T-90 (analysis window start)
    # =============================================
    while True:
        now = datetime.now(timezone.utc)
        remaining = (end - now).total_seconds()
        if remaining <= ANALYSIS_START:
            break
        await asyncio.sleep(min(remaining - ANALYSIS_START, 5))

    log.info("%s | Analyzing T-%d to T-%d...", slug, ANALYSIS_START, ANALYSIS_END)

    # =============================================
    # ANALYSIS: T-90 to T-30, collect price samples
    # =============================================
    up_samples = []
    dn_samples = []

    while True:
        now = datetime.now(timezone.utc)
        remaining = (end - now).total_seconds()
        if remaining <= ANALYSIS_END:
            break

        up_p = await get_price(session, uid, "BUY")
        dn_p = await get_price(session, did, "BUY")
        if up_p is not None:
            up_samples.append(up_p)
        if dn_p is not None:
            dn_samples.append(dn_p)

        if up_p and dn_p:
            log.info("  [T-%.0f] UP $%.3f DN $%.3f", remaining, up_p, dn_p)

        await asyncio.sleep(POLL)

    if not up_samples or not dn_samples:
        log.info("No data, skip")
        skips += 1
        await tg(session, "SKIP: no data | %s\n%s" % (slug, stats_str()))
        now = datetime.now(timezone.utc)
        left = (end - now).total_seconds()
        if left > 0:
            await asyncio.sleep(left + 2)
        return

    avg_up = sum(up_samples) / len(up_samples)
    avg_dn = sum(dn_samples) / len(dn_samples)

    log.info("Analysis done | UP avg $%.3f (%d) | DN avg $%.3f (%d)",
             avg_up, len(up_samples), avg_dn, len(dn_samples))

    # =============================================
    # ENTRY: Last 30 seconds
    # =============================================
    bought = False
    buy_side = None
    buy_tok = None
    buy_price = None
    buy_shares = 0.0
    done = False
    ll = 0

    while True:
        now = datetime.now(timezone.utc)
        remaining = (end - now).total_seconds()
        nt = int(time.time())

        # Resolution
        if remaining < 3:
            await asyncio.sleep(remaining + 6)
            if bought and not done:
                fp = await get_price(session, buy_tok, "BUY")
                if fp is not None and fp > 0.90:
                    payout = buy_shares * 1.0 * 0.98
                    profit = payout - BET_SIZE
                    wins += 1
                    result = "WIN"
                elif fp is not None and fp < 0.10:
                    profit = -BET_SIZE
                    losses += 1
                    result = "LOSS"
                else:
                    profit = ((fp or buy_price) - buy_price) * buy_shares
                    if profit >= 0:
                        wins += 1
                    else:
                        losses += 1
                    result = "RESOLVED"
                daily_pnl += profit
                trades_today += 1
                done = True
                log.info("%s %s $%.3f | $%.2f | %s",
                         result, buy_side, buy_price, profit, stats_str())
                await tg(session, "%s %s $%.3f\n$%.2f\n%s" % (
                    result, buy_side, buy_price, profit, stats_str()))
            elif not bought:
                skips += 1
                log.info("No entry | UP avg $%.3f DN avg $%.3f | %s",
                         avg_up, avg_dn, stats_str())
                await tg(session, "SKIP: no entry\nUP avg $%.3f DN avg $%.3f\n%s" % (
                    avg_up, avg_dn, stats_str()))
            break

        # TP / SL if bought
        if bought and not done:
            cur = await get_price(session, buy_tok, "SELL")
            if cur is None:
                cur = await get_price(session, buy_tok, "BUY")

            if cur is not None:
                if cur >= TP_PRICE:
                    sold = do_sell(buy_tok, trunc4(buy_shares))
                    profit = (cur - buy_price) * buy_shares
                    daily_pnl += profit
                    trades_today += 1
                    wins += 1
                    done = True
                    log.info("TP %s $%.3f->$%.3f | +$%.2f | %s",
                             buy_side, buy_price, cur, profit, stats_str())
                    await tg(session, "TP %s $%.3f->$%.3f\n+$%.2f\n%s" % (
                        buy_side, buy_price, cur, profit, stats_str()))

                elif cur <= SL_PRICE:
                    sold = do_sell(buy_tok, trunc4(buy_shares))
                    profit = (cur - buy_price) * buy_shares
                    daily_pnl += profit
                    trades_today += 1
                    losses += 1
                    done = True
                    log.info("SL %s $%.3f->$%.3f | $%.2f | %s",
                             buy_side, buy_price, cur, profit, stats_str())
                    await tg(session, "SL %s $%.3f->$%.3f\n$%.2f\n%s" % (
                        buy_side, buy_price, cur, profit, stats_str()))

            if not done and nt - ll >= 5:
                cur_str = "$%.3f" % cur if cur else "?"
                log.info("Hold %s $%.3f->%s | %.0fs", buy_side, buy_price, cur_str, remaining)
                ll = nt

            await asyncio.sleep(POLL)
            continue

        if done:
            await asyncio.sleep(POLL)
            continue

        # Try to enter
        if not bought:
            up_p = await get_price(session, uid, "BUY")
            dn_p = await get_price(session, did, "BUY")

            # Check UP: current $0.70-$0.90 AND avg was > $0.65
            if up_p is not None and ENTRY_MIN <= up_p <= ENTRY_MAX and avg_up >= AVG_MIN:
                log.info("ENTRY: UP @ $%.3f (avg $%.3f >= $%.2f)", up_p, avg_up, AVG_MIN)
                ok, got = do_buy(uid, BET_SIZE, price=up_p)
                if ok:
                    bought = True
                    buy_side = "UP"
                    buy_tok = uid
                    buy_price = up_p
                    buy_shares = got or trunc4(BET_SIZE / up_p)
                    await tg(session,
                        "BUY <b>UP</b> $%.1f @ $%.3f\nAvg $%.3f | %.0fs left\nTP $%.2f SL $%.2f\n%s" % (
                            BET_SIZE, up_p, avg_up, remaining, TP_PRICE, SL_PRICE, stats_str()))
                else:
                    await tg(session, "BUY FAILED UP @ $%.3f" % up_p)

            # Check DOWN: current $0.70-$0.90 AND avg was > $0.65
            elif dn_p is not None and ENTRY_MIN <= dn_p <= ENTRY_MAX and avg_dn >= AVG_MIN:
                log.info("ENTRY: DOWN @ $%.3f (avg $%.3f >= $%.2f)", dn_p, avg_dn, AVG_MIN)
                ok, got = do_buy(did, BET_SIZE, price=dn_p)
                if ok:
                    bought = True
                    buy_side = "DOWN"
                    buy_tok = did
                    buy_price = dn_p
                    buy_shares = got or trunc4(BET_SIZE / dn_p)
                    await tg(session,
                        "BUY <b>DOWN</b> $%.1f @ $%.3f\nAvg $%.3f | %.0fs left\nTP $%.2f SL $%.2f\n%s" % (
                            BET_SIZE, dn_p, avg_dn, remaining, TP_PRICE, SL_PRICE, stats_str()))
                else:
                    await tg(session, "BUY FAILED DOWN @ $%.3f" % dn_p)

            elif nt - ll >= 5:
                up_str = "$%.3f" % up_p if up_p else "?"
                dn_str = "$%.3f" % dn_p if dn_p else "?"
                log.info("Scanning: UP %s (avg $%.3f) DN %s (avg $%.3f) | %.0fs",
                         up_str, avg_up, dn_str, avg_dn, remaining)
                ll = nt

        await asyncio.sleep(POLL)

    # Next round
    now = datetime.now(timezone.utc)
    left = (end - now).total_seconds()
    if left > 0:
        await asyncio.sleep(left + 2)
    else:
        await asyncio.sleep(3)


async def main():
    log.info("=" * 55)
    log.info("  ETH Safe Bet [PAPER]")
    log.info("  Analyze T-%d to T-%d", ANALYSIS_START, ANALYSIS_END)
    log.info("  Entry $%.2f-$%.2f if avg >= $%.2f | $%.1f bet", ENTRY_MIN, ENTRY_MAX, AVG_MIN, BET_SIZE)
    log.info("  TP $%.2f | SL $%.2f", TP_PRICE, SL_PRICE)
    log.info("  Mode: PAPER")
    log.info("=" * 55)

    if not init_clob():
        log.error("CLOB failed")
        sys.exit(1)

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=300, connect=30, sock_read=60),
        connector=aiohttp.TCPConnector(keepalive_timeout=120, limit=20),
    ) as session:
        await tg(session,
            "ETH Safe Bet PAPER\nAnalyze T-%d to T-%d\n$%.2f-$%.2f avg>=$%.2f\n$%.1f | TP$%.2f SL$%.2f" % (
                ANALYSIS_START, ANALYSIS_END, ENTRY_MIN, ENTRY_MAX, AVG_MIN,
                BET_SIZE, TP_PRICE, SL_PRICE))
        cycle_errors = 0
        while True:
            try:
                await cycle(session)
                cycle_errors = 0
            except KeyboardInterrupt:
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, OSError) as e:
                cycle_errors += 1
                log.warning("Conn #%d: %s", cycle_errors, e)
                if cycle_errors >= 3:
                    init_clob()
                    cycle_errors = 0
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
