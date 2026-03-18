"""
Polymarket ETH 5-Min Contrarian - PAPER
Analyze: T-150 to T-60 (build averages).
If winning side avg > $0.65 and price $0.70-$0.90 (confirms a clear leader),
buy the LOSING side for $1 at T-60. No TP, no SL. Hold to resolution.
Betting on last-minute reversals. Track everything.
"""
import os, sys, json, time, asyncio, logging, traceback
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("eth_contra")

GAMMA = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com")
CLOB = os.getenv("CLOB_API", "https://clob.polymarket.com")
RPC = os.getenv("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")

BET_SIZE = float(os.getenv("BET_SIZE", "1.0"))
# These define when we consider there's a clear winner to bet AGAINST
WINNER_MIN = float(os.getenv("WINNER_MIN", "0.70"))
WINNER_MAX = float(os.getenv("WINNER_MAX", "0.90"))
WINNER_AVG_MIN = float(os.getenv("WINNER_AVG_MIN", "0.65"))
ANALYSIS_START = int(os.getenv("ANALYSIS_START_SECS", "150"))
ANALYSIS_END = int(os.getenv("ANALYSIS_END_SECS", "60"))
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

def oracle():
    try:
        w3 = Web3(Web3.HTTPProvider(RPC))
        c = w3.eth.contract(address=Web3.to_checksum_address(CL_ADDR), abi=CL_ABI)
        dec = c.functions.decimals().call()
        d = c.functions.latestRoundData().call()
        return float(d[1]) / (10 ** dec), d[3]
    except Exception as e:
        log.error("Oracle: %s", e)
        return None, None


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

    # Wait until analysis window
    while True:
        now = datetime.now(timezone.utc)
        remaining = (end - now).total_seconds()
        if remaining <= ANALYSIS_START:
            break
        await asyncio.sleep(min(remaining - ANALYSIS_START, 5))

    log.info("%s | Contrarian analyzing T-%d to T-%d...", slug, ANALYSIS_START, ANALYSIS_END)

    # =============================================
    # ANALYSIS: T-150 to T-60
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
    # DECIDE: is there a clear winner to bet against?
    # =============================================
    # Get current prices at T-60
    up_now = await get_price(session, uid, "BUY")
    dn_now = await get_price(session, did, "BUY")

    winner_side = None
    loser_side = None
    loser_tok = None
    loser_price = None
    winner_avg = None
    winner_price = None

    # Check if UP is the clear winner
    if (up_now is not None and WINNER_MIN <= up_now <= WINNER_MAX
            and avg_up >= WINNER_AVG_MIN):
        winner_side = "UP"
        winner_avg = avg_up
        winner_price = up_now
        loser_side = "DOWN"
        loser_tok = did
        loser_price = dn_now

    # Check if DOWN is the clear winner
    elif (dn_now is not None and WINNER_MIN <= dn_now <= WINNER_MAX
            and avg_dn >= WINNER_AVG_MIN):
        winner_side = "DOWN"
        winner_avg = avg_dn
        winner_price = dn_now
        loser_side = "UP"
        loser_tok = uid
        loser_price = up_now

    if not winner_side or loser_price is None:
        skips += 1
        log.info("No clear winner | UP $%s (avg $%.3f) DN $%s (avg $%.3f) | %s",
                 ("%.3f" % up_now) if up_now else "?", avg_up,
                 ("%.3f" % dn_now) if dn_now else "?", avg_dn, stats_str())
        await tg(session, "SKIP: no clear winner\nUP $%s avg $%.3f\nDN $%s avg $%.3f\n%s" % (
            ("%.3f" % up_now) if up_now else "?", avg_up,
            ("%.3f" % dn_now) if dn_now else "?", avg_dn, stats_str()))
        now = datetime.now(timezone.utc)
        left = (end - now).total_seconds()
        if left > 0:
            await asyncio.sleep(left + 2)
        return

    # =============================================
    # BUY THE LOSER at any price
    # =============================================
    buy_shares = trunc4(BET_SIZE / loser_price)
    log.info("CONTRARIAN: Winner %s $%.3f (avg $%.3f) | BUY %s @ $%.3f | $%.1f | PAPER",
             winner_side, winner_price, winner_avg, loser_side, loser_price, BET_SIZE)
    await tg(session,
        "BUY <b>%s</b> (loser) @ $%.3f | $%.1f\nWinner: %s $%.3f avg $%.3f\nPayout if reversal: ~$%.1f\n%s" % (
            loser_side, loser_price, BET_SIZE,
            winner_side, winner_price, winner_avg,
            BET_SIZE / loser_price, stats_str()))

    # =============================================
    # HOLD TO RESOLUTION — no TP, no SL
    # =============================================
    ll = 0
    while True:
        now = datetime.now(timezone.utc)
        remaining = (end - now).total_seconds()
        nt = int(time.time())

        if remaining < 3:
            await asyncio.sleep(remaining + 6)

            fp = await get_price(session, loser_tok, "BUY")
            if fp is not None and fp > 0.90:
                # Loser actually won — reversal!
                payout = buy_shares * 1.0 * 0.98
                profit = payout - BET_SIZE
                wins += 1
                result = "WIN (REVERSAL!)"
            elif fp is not None and fp < 0.10:
                profit = -BET_SIZE
                losses += 1
                result = "LOSS"
            else:
                profit = ((fp or loser_price) - loser_price) * buy_shares
                if profit >= 0:
                    wins += 1
                    result = "WIN"
                else:
                    losses += 1
                    result = "LOSS"

            daily_pnl += profit
            trades_today += 1
            log.info("%s | Bought %s @ $%.3f | Final $%s | $%.2f | %s",
                     result, loser_side, loser_price,
                     ("%.3f" % fp) if fp else "?", profit, stats_str())
            await tg(session,
                "%s\n%s @ $%.3f -> $%s\n$%.2f\n%s" % (
                    result, loser_side, loser_price,
                    ("%.3f" % fp) if fp else "?", profit, stats_str()))
            break

        # Just log position while waiting
        if nt - ll >= 10:
            cur = await get_price(session, loser_tok, "BUY")
            cur_str = "$%.3f" % cur if cur else "?"
            log.info("Hold %s $%.3f -> %s | %.0fs left",
                     loser_side, loser_price, cur_str, remaining)
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
    log.info("  ETH Contrarian [PAPER]")
    log.info("  Analyze T-%d to T-%d", ANALYSIS_START, ANALYSIS_END)
    log.info("  If winner $%.2f-$%.2f avg>=$%.2f -> buy LOSER $%.1f",
             WINNER_MIN, WINNER_MAX, WINNER_AVG_MIN, BET_SIZE)
    log.info("  No TP, no SL. Hold to resolution.")
    log.info("  Mode: PAPER")
    log.info("=" * 55)

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=300, connect=30, sock_read=60),
        connector=aiohttp.TCPConnector(keepalive_timeout=120, limit=20),
    ) as session:
        await tg(session,
            "ETH Contrarian PAPER\nT-%d to T-%d | Winner $%.2f-$%.2f avg>=$%.2f\nBuy LOSER $%.1f | Hold to resolution" % (
                ANALYSIS_START, ANALYSIS_END, WINNER_MIN, WINNER_MAX, WINNER_AVG_MIN, BET_SIZE))
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
