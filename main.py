import os
import time
import requests

# ================= CONFIG =================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

RUN_LIVE_TRADING = os.getenv("RUN_LIVE_TRADING", "false").lower() == "true"

PRODUCTS = ["BTC-PERP-INTX", "ETH-PERP-INTX"]

TREND_GRANULARITY = "ONE_HOUR"
ENTRY_GRANULARITY = "FIVE_MINUTE"

TREND_FAST_MA = 50
TREND_SLOW_MA = 200

ENTRY_FAST_MA = 20
RSI_PERIOD = 14

RSI_LONG = 55
RSI_SHORT = 45

STOP_LOSS = 0.01
TRAILING_ARM = 0.01
TRAILING_STOP = 0.008

TRADE_SIZE = 100
MAX_OPEN_TRADES = 1

SCAN_INTERVAL = 30
UPDATE_INTERVAL = 180

START_BALANCE = 500.0

# ================= STATE =================

balance = START_BALANCE
positions = {}
running = True
last_update = 0
telegram_offset = None

# ================= HELPERS =================

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg}
        )
    except:
        pass

def get_candles(product, granularity, limit=200):
    try:
        url = f"https://api.exchange.coinbase.com/products/{product.replace('-PERP-INTX','-USD')}/candles"
        params = {"granularity": 300 if granularity=="FIVE_MINUTE" else 3600}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        return list(reversed(data))[-limit:]
    except:
        return []

def calc_ma(prices, period):
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period

def calc_rsi(prices, period=14):
    if len(prices) < period+1:
        return None
    gains = []
    losses = []
    for i in range(-period, 0):
        diff = prices[i] - prices[i-1]
        if diff >= 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))
    avg_gain = sum(gains)/period if gains else 0
    avg_loss = sum(losses)/period if losses else 0
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_price(candle):
    return candle[4]

# ================= STATUS BUILDER =================

def build_status():
    lines = [f"📊 Balance: ${balance:.2f}", f"Open Trades: {len(positions)}"]

    for product, pos in positions.items():
        candles = get_candles(product, ENTRY_GRANULARITY, 2)
        if not candles:
            continue

        price = get_price(candles[-1])
        entry = pos["entry"]
        side = pos["side"]
        size = pos["size"]

        pnl_pct = ((price - entry) / entry) * 100 if side == "LONG" else ((entry - price) / entry) * 100
        pnl_usd = ((price - entry) * size) if side == "LONG" else ((entry - price) * size)

        peak = pos["peak"]

        if side == "LONG":
            trail_dist = ((peak - price) / peak) * 100
        else:
            trail_dist = ((price - peak) / peak) * 100

        lines.append("")
        lines.append(f"{product} | {side}")
        lines.append(f"Entry: {entry:.2f}")
        lines.append(f"Current: {price:.2f}")
        lines.append(f"PnL: ${pnl_usd:.2f} ({pnl_pct:.2f}%)")
        lines.append(f"Peak: {peak:.2f}")
        lines.append(f"Trail Distance: {trail_dist:.2f}%")

    return "\n".join(lines)

# ================= TELEGRAM CONTROL =================

def handle_telegram():
    global running, telegram_offset
    if not TELEGRAM_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {"timeout": 1}
        if telegram_offset:
            params["offset"] = telegram_offset

        r = requests.get(url, params=params, timeout=5).json()

        for update in r.get("result", []):
            telegram_offset = update["update_id"] + 1
            text = update.get("message", {}).get("text", "")

            if text == "/stop":
                running = False
                send_telegram("🛑 BOT STOPPED")

            elif text == "/start":
                running = True
                send_telegram("▶️ BOT STARTED")

            elif text == "/status":
                send_telegram(build_status())

    except:
        pass

# ================= CORE =================

def detect_trend(product):
    candles = get_candles(product, TREND_GRANULARITY)
    closes = [get_price(c) for c in candles]

    ma_fast = calc_ma(closes, TREND_FAST_MA)
    ma_slow = calc_ma(closes, TREND_SLOW_MA)

    if not ma_fast or not ma_slow:
        return "NONE"

    if abs(ma_fast - ma_slow) / ma_slow < 0.002:
        return "NONE"

    if ma_fast > ma_slow:
        return "LONG"
    elif ma_fast < ma_slow:
        return "SHORT"

    return "NONE"

def check_entry(product, trend):
    candles = get_candles(product, ENTRY_GRANULARITY)
    closes = [get_price(c) for c in candles]

    ma = calc_ma(closes, ENTRY_FAST_MA)
    rsi = calc_rsi(closes)

    if not ma or not rsi:
        return None

    price = closes[-1]

    if trend == "LONG" and price > ma and rsi > RSI_LONG:
        return "LONG"

    if trend == "SHORT" and price < ma and rsi < RSI_SHORT:
        return "SHORT"

    return None

def open_position(product, side, price):
    if len(positions) >= MAX_OPEN_TRADES:
        return

    size = TRADE_SIZE / price

    positions[product] = {
        "side": side,
        "entry": price,
        "size": size,
        "peak": price
    }

    send_telegram(f"🟡 PAPER ENTRY {side}\n{product}\nPrice: {price:.2f}")

def manage_positions():
    global balance

    for product in list(positions.keys()):
        candles = get_candles(product, ENTRY_GRANULARITY, 2)
        if not candles:
            continue

        price = get_price(candles[-1])
        pos = positions[product]

        entry = pos["entry"]
        side = pos["side"]

        pnl = (price - entry) / entry if side == "LONG" else (entry - price) / entry

        if pnl <= -STOP_LOSS:
            close_position(product, price, "SL")
            continue

        if pnl > TRAILING_ARM:
            if side == "LONG":
                pos["peak"] = max(pos["peak"], price)
                drop = (pos["peak"] - price) / pos["peak"]
            else:
                pos["peak"] = min(pos["peak"], price)
                drop = (price - pos["peak"]) / pos["peak"]

            if drop >= TRAILING_STOP:
                close_position(product, price, "TRAIL")

def close_position(product, price, reason):
    global balance

    pos = positions.pop(product)
    entry = pos["entry"]
    size = pos["size"]
    side = pos["side"]

    pnl = (price - entry) * size if side == "LONG" else (entry - price) * size
    balance += pnl

    send_telegram(f"🔴 EXIT ({reason})\n{product}\nPnL: ${pnl:.2f}\nBalance: ${balance:.2f}")

# ================= MAIN =================

send_telegram("🚀 Futures Trend Bot Started (PAPER MODE)")

while True:
    handle_telegram()

    if running:
        for product in PRODUCTS:
            if product in positions:
                continue

            trend = detect_trend(product)
            if trend == "NONE":
                continue

            signal = check_entry(product, trend)

            if signal:
                candles = get_candles(product, ENTRY_GRANULARITY, 1)
                if candles:
                    price = get_price(candles[-1])
                    open_position(product, signal, price)

        manage_positions()

    if time.time() - last_update > UPDATE_INTERVAL:
        last_update = time.time()
        send_telegram(build_status())

    time.sleep(SCAN_INTERVAL)
