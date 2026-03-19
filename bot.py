"""
Polymarket BTC 5-Min Copytrade Bot - LIVE
Monitors target wallet trades on BTC 5-min markets via Data API.
When target buys UP or DOWN, bot mirrors the same side for $1.
No TP, no SL. Hold to resolution like the target.
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

import sys, json, time, asyncio, logging, traceback
from datetime import datetime, timezone

import aiohttp
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("copytrade")

GAMMA = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com")
CLOB = os.getenv("CLOB_API", "https://clob.polymarket.com")
DATA_API = os.getenv("DATA_API", "https://data-api.polymarket.com")
PK = os.getenv("POLY_PRIVATE_KEY", "")
FUNDER = os.getenv("POLY_PROXY_ADDRESS", "")
SIG_TYPE = int(os.getenv("POLY_SIGNATURE_TYPE", "2"))
clob_client = None

TARGET = os.getenv("COPY_TARGET", "0xc1a65ac2f608d940c493fe22f5ed54a7b8687601")
BET_SIZE = float(os.getenv("BET_SIZE", "3.0"))
POLL = float(os.getenv("POLL_INTERVAL", "5.0"))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS_USDC", "50.0"))
MAX_TRADES = int(os.getenv("MAX_TRADES_PER_DAY", "200"))
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")

daily_pnl = 0.0
trades_today = 0
last_reset = None
wins = 0
losses = 0
copied = 0
seen_tx = set()  # track transaction hashes we've already processed

if _proxy:
    log.info("Proxy: %s", _proxy.split("@")[-1] if "@" in _proxy else _proxy[:30])


def trunc2(v):
    return float(int(v * 100)) / 100.0

def trunc4(v):
    return float(int(v * 10000)) / 10000.0


def init_clob():
    global clob_client
    if not PK:
        log.error("POLY_PRIVATE_KEY required")
        return False
    pk = PK[2:] if PK.startswith("0x") else PK
    try:
        if FUNDER:
            clob_client = ClobClient(host=CLOB, key=pk, chain_id=137, signature_type=SIG_TYPE, funder=FUNDER)
        else:
            clob_client = ClobClient(host=CLOB, key=pk, chain_id=137)
        clob_client.set_api_creds(clob_client.create_or_derive_api_creds())
        if _proxy:
            try:
                import httpx
                import py_clob_client.http_helpers.helpers as helpers
                proxied = httpx.Client(http2=True, proxy=_proxy, timeout=120.0)
                for an in dir(helpers):
                    ob = getattr(helpers, an, None)
                    if isinstance(ob, httpx.Client):
                        setattr(helpers, an, proxied)
            except Exception:
                pass
        log.info("CLOB OK | Sig:%d", SIG_TYPE)
        return True
    except Exception as e:
        log.error("CLOB: %s", e)
        return False


def do_buy(tid, amt, price):
    amt = trunc2(amt)
    bp = trunc2(price)
    shares = trunc4(amt / bp)
    if shares < 0.01:
        return False
    for attempt in range(3):
        try:
            log.info("BUY: %.4f @ $%.2f ($%.2f)", shares, bp, amt)
            args = OrderArgs(token_id=tid, price=bp, size=shares, side=BUY)
            signed = clob_client.create_order(args)
            resp = clob_client.post_order(signed, OrderType.GTC)
            log.info("BUY OK: %s", resp)
            return True
        except Exception as e:
            emsg = str(e).lower()
            log.error("BUY #%d: %s", attempt + 1, e)
            if "timeout" in emsg or "connection" in emsg:
                init_clob()
                time.sleep(2)
            elif "decimal" in emsg or "accuracy" in emsg:
                shares = trunc2(amt / bp)
                try:
                    args = OrderArgs(token_id=tid, price=bp, size=shares, side=BUY)
                    signed = clob_client.create_order(args)
                    resp = clob_client.post_order(signed, OrderType.GTC)
                    return True
                except Exception:
                    return False
            else:
                return False
    return False


def winfo():
    now = int(time.time())
    wt = now - (now % 300)
    return "btc-updown-5m-%d" % wt, wt, wt + 300


async def fetch_market(session, slug):
    """Get token IDs for a market slug."""
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


async def get_price(session, tid):
    try:
        async with session.get(CLOB + "/price", params={"token_id": tid, "side": "BUY"}) as r:
            if r.status == 200:
                d = await r.json()
                if d.get("price"):
                    return float(d["price"])
    except Exception:
        pass
    return None


async def fetch_target_trades(session):
    """Fetch recent BTC 5-min trades from target wallet."""
    try:
        params = {
            "user": TARGET,
            "limit": 20,
        }
        async with session.get(DATA_API + "/trades", params=params) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        log.debug("Fetch trades: %s", e)
    return []


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
    return "Copied: %d | W%d L%d (%.0f%%) | PnL $%.2f" % (copied, wins, losses, rate, daily_pnl)


async def main():
    global daily_pnl, trades_today, wins, losses, copied

    log.info("=" * 55)
    log.info("  Copytrade Bot [LIVE]")
    log.info("  Target: %s", TARGET)
    log.info("  Bet: $%.1f | Poll: %.0fs", BET_SIZE, POLL)
    log.info("  Proxy: %s | Sig: %d", "YES" if _proxy else "NO", SIG_TYPE)
    log.info("=" * 55)

    if not init_clob():
        log.error("CLOB failed")
        sys.exit(1)

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=300, connect=30, sock_read=60),
        connector=aiohttp.TCPConnector(keepalive_timeout=120, limit=20),
    ) as session:
        await tg(session,
            "Copytrade LIVE\nTarget: %s\n$%.1f bets | Poll %ds" % (
                TARGET[:10] + "..." + TARGET[-6:], BET_SIZE, int(POLL)))

        # Track which markets we've already traded this window
        traded_windows = set()
        ll = 0
        conn_errors = 0

        while True:
            try:
                nt = int(time.time())
                slug, wt, ct = winfo()
                remaining = ct - nt

                # Reset traded_windows for new 5-min window
                if wt not in traded_windows or len(traded_windows) > 100:
                    # Prune old windows
                    traded_windows = {w for w in traded_windows if w >= wt - 600}

                # Don't trade in last 5 seconds (resolution imminent)
                if remaining < 5:
                    await asyncio.sleep(remaining + 2)
                    continue

                # Fetch target's recent trades
                trades = await fetch_target_trades(session)
                if not trades:
                    if nt - ll >= 30:
                        log.info("No target trades | %s | %.0fs left", slug, remaining)
                        ll = nt
                    await asyncio.sleep(POLL)
                    conn_errors = 0
                    continue

                # Filter for BTC 5-min trades we haven't seen
                for trade in trades:
                    tx_hash = trade.get("transactionHash", "")
                    if not tx_hash or tx_hash in seen_tx:
                        continue

                    event_slug = trade.get("eventSlug", "") or trade.get("slug", "")
                    if "btc-updown-5m" not in event_slug:
                        seen_tx.add(tx_hash)
                        continue

                    side = trade.get("side", "")
                    if side != "BUY":
                        seen_tx.add(tx_hash)
                        continue

                    outcome = (trade.get("outcome", "") or "").lower()
                    target_price = trade.get("price", 0)
                    target_size = trade.get("size", 0)
                    asset = trade.get("asset", "")

                    seen_tx.add(tx_hash)

                    # Determine if this is for current or next window
                    # Extract timestamp from slug
                    trade_slug = event_slug
                    try:
                        trade_ts = int(trade_slug.split("-")[-1])
                    except (ValueError, IndexError):
                        trade_ts = wt

                    # Skip if we already traded this window
                    if trade_ts in traded_windows:
                        continue

                    # Determine side
                    if "up" in outcome or "yes" in outcome:
                        pick_side = "UP"
                    elif "down" in outcome or "no" in outcome:
                        pick_side = "DOWN"
                    else:
                        log.info("Unknown outcome: %s", outcome)
                        continue

                    if not risk_ok():
                        log.warning("Risk limit | %s", stats_str())
                        continue

                    # Get the market tokens
                    market_slug = "btc-updown-5m-%d" % trade_ts
                    uid, did = await fetch_market(session, market_slug)
                    if not uid or not did:
                        log.error("Can't find market %s", market_slug)
                        continue

                    # Pick token
                    buy_tid = uid if pick_side == "UP" else did

                    # Get current price
                    cur_price = await get_price(session, buy_tid)
                    if cur_price is None or cur_price <= 0:
                        log.error("No price for %s %s", pick_side, market_slug)
                        continue

                    # Copy the trade
                    log.info("COPY: Target bought %s @ $%.3f ($%.1f) | We buy %s @ $%.3f ($%.1f)",
                             pick_side, target_price, target_size, pick_side, cur_price, BET_SIZE)

                    ok = do_buy(buy_tid, BET_SIZE, price=cur_price)
                    if ok:
                        copied += 1
                        traded_windows.add(trade_ts)
                        await tg(session,
                            "COPY <b>%s</b> @ $%.3f | $%.1f\nTarget: %s @ $%.3f ($%.1f)\nMarket: %s\n%s" % (
                                pick_side, cur_price, BET_SIZE,
                                pick_side, target_price, target_size,
                                market_slug, stats_str()))
                    else:
                        log.error("BUY FAILED %s @ $%.3f", pick_side, cur_price)
                        await tg(session, "COPY FAILED %s @ $%.3f\n%s" % (
                            pick_side, cur_price, stats_str()))

                # Check resolutions for windows we traded
                if traded_windows:
                    for tw in list(traded_windows):
                        tw_end = tw + 300
                        if nt > tw_end + 10:
                            # Window resolved, check result
                            res_slug = "btc-updown-5m-%d" % tw
                            res_uid, res_did = await fetch_market(session, res_slug)
                            if res_uid:
                                up_p = await get_price(session, res_uid)
                                dn_p = await get_price(session, res_did) if res_did else None
                                if up_p is not None and up_p > 0.90:
                                    winner = "UP"
                                elif dn_p is not None and dn_p > 0.90:
                                    winner = "DOWN"
                                else:
                                    winner = "?"
                                # We don't know exactly which side we bought for this window
                                # without more state tracking, so just log it
                                log.info("Resolved %s | Winner: %s", res_slug, winner)
                            traded_windows.discard(tw)

                if nt - ll >= 30:
                    log.info("Watching %s... | %.0fs left | seen %d tx | %s",
                             TARGET[:10], remaining, len(seen_tx), stats_str())
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
