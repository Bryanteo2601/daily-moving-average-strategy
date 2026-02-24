
import pandas as pd
import pandas_ta_classic as ta
import os, certifi
os.environ["SSL_CERT_FILE"] = certifi.where()
import asyncio
from collections import deque
from zoneinfo import ZoneInfo
from alpaca.data.live.stock import StockDataStream
from datetime import datetime, timedelta, timezone
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest, MarketOrderRequest
from alpaca.trading.enums import AssetStatus, ContractType, OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
# 1. Initialize Alpaca
API_KEY = "PKJJPOFGNPK3XQ5NAD6EUSD5KJ"
SECRET_KEY = "5N1GgVQuAsmgGxB7fMRY7CDVjFHFcMww4U9WaFWoKJcs"
SYMBOL = "NVDA"
trading_client = TradingClient('API_KEY', 'SECRET_KEY', paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

ET = ZoneInfo("America/New_York")

SMA_LEN_BARS = 20
LOOKBACK_DAYS = 3
QTY = 1
PAPER = True

closes_15m = deque(maxlen=SMA_LEN_BARS)  # last SMA_LEN_BARS closes (15m)
current_bucket_start = None             # UTC timestamp floored to 15m
current_close = None                    # live-updating close for current 15m bucket
last_above = None                       
order_lock = None


def get_15m_bars() -> pd.DataFrame:
    start = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    req = StockBarsRequest(
        symbol_or_symbols=SYMBOL,
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        start=start,
    )

    df = data_client.get_stock_bars(req).df

    if isinstance(df.index, pd.MultiIndex):
        df = df.reset_index()
        df = df[df["symbol"] == SYMBOL].set_index("timestamp").sort_index()

    return df


def floor_to_15m(dt):
    minute = (dt.minute // 15) * 15
    return dt.replace(minute=minute, second=0, microsecond=0)


def seed_closes_from_history():
    bars = get_15m_bars()
    if bars.empty:
        raise RuntimeError("No historical bars returned to seed SMA.")

    recent = bars["close"].dropna().tail(SMA_LEN_BARS).tolist()
    closes_15m.clear()
    closes_15m.extend([float(x) for x in recent])


def compute_live_sma(live_close: float) -> float | None:
    if len(closes_15m) < SMA_LEN_BARS:
        return None
    tmp = list(closes_15m)
    tmp[-1] = float(live_close)
    return sum(tmp) / len(tmp)
def get_underlying_price() -> float:
    """Fetches the most recent trade price for the underlying."""
    req = StockLatestTradeRequest(symbol_or_symbols=SYMBOL)
    res = data_client.get_stock_latest_trade(req)
    return float(res[SYMBOL].price)


def pick_atm_contract(side: str):
    """
    Picks an ATM-ish option contract (CALL for bullish, PUT for bearish),
    expiring 30–45 days out.
    """
    current_price = get_underlying_price()

    contract_type = ContractType.CALL if side == "BULLISH" else ContractType.PUT
    exp_gte = (datetime.now().date() + timedelta(days=30))
    exp_lte = (datetime.now().date() + timedelta(days=45))

    req = GetOptionContractsRequest(
        underlying_symbol=[SYMBOL],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=exp_gte,
        expiration_date_lte=exp_lte,
        type=contract_type,
    )

    res = trading_client.get_option_contracts(req)
    contracts = res.option_contracts

    if not contracts:
        raise RuntimeError("No option contracts found. Check options permissions / filters.")

    # Choose strike closest to current underlying price
    best = min(contracts, key=lambda x: abs(float(x.strike_price) - current_price))
    return best, current_price


def open_option(side: str):
    """
    Buys 1 ATM call/put based on side ("BULLISH"=CALL, "BEARISH"=PUT).
    """
    best_contract, current_price = pick_atm_contract(side)

    print(
        f"Placing order: {side} | Underlying {SYMBOL}=${current_price:.2f} | "
        f"Contract {best_contract.symbol} (strike {best_contract.strike_price})"
    )

    order = MarketOrderRequest(
        symbol=best_contract.symbol,
        qty=QTY,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    )

    resp = trading_client.submit_order(order)
    print(f"SUCCESS: Order submitted. ID: {resp.id}")
    
def close_any_open_nvda_option_positions():
    positions = trading_client.get_all_positions()
    for p in positions:
        sym = getattr(p, "symbol", "")
        if isinstance(sym, str) and sym.startswith(SYMBOL):
            try:
                print(f"Closing position: {sym} (qty={p.qty})")
                trading_client.close_position(sym)  # market close
            except Exception as e:
                print(f"ERROR closing {sym}: {e}")

async def maybe_trade_on_cross(live_price: float):
    global last_above, order_lock

    # create the lock inside the running event loop
    if order_lock is None:
        order_lock = asyncio.Lock()

    sma = compute_live_sma(live_price)
    if sma is None:
        return

    above = live_price > sma

    if last_above is None:
        last_above = above
        print(f"Init state: price={live_price:.2f}, SMA={sma:.2f}, above={above}")
        return

    if above != last_above:
        last_above = above
        side = "BULLISH" if above else "BEARISH"
        print(f"CROSS! price={live_price:.2f} vs SMA={sma:.2f} => {side}")

    


async def on_trade(trade):
    global current_bucket_start, current_close

    ts = trade.timestamp
    price = float(trade.price)

    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    bucket = floor_to_15m(ts)

    if current_bucket_start is None:
        current_bucket_start = bucket
        current_close = price
    elif bucket != current_bucket_start:
        closes_15m.append(float(current_close))
        current_bucket_start = bucket
        current_close = price
        print(f"New 15m bucket: {bucket.isoformat()} | seeded closes={len(closes_15m)}")
    else:
        current_close = price

    await maybe_trade_on_cross(current_close)


def run_stream_forever():
    seed_closes_from_history()

    stream = StockDataStream(API_KEY, SECRET_KEY)
    stream.subscribe_trades(on_trade, SYMBOL)

    print("Streaming live trades… (Ctrl+C to stop)")
    stream.run()


if __name__ == "__main__":
    run_stream_forever()

