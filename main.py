import os
import time
import requests
from typing import List, Dict, Optional

# =========================
# ENV VARIABLES (Railway)
# =========================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise RuntimeError("Missing BOT_TOKEN or CHAT_ID")

# =========================
# CONFIG
# =========================

MAX_PRICE = 0.25
MIN_VOLUME_USDT = 5_000_000
MIN_CHANGE_PCT = 6

ELITE_SCORE = 8.0
ATOMIC_SCORE = 9.0

SCAN_INTERVAL = 60

COOLDOWN_ELITE = 90 * 60
COOLDOWN_ATOMIC = 180 * 60

last_alert = {}

BINANCE_24H = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
COINBASE_PRODUCTS = "https://api.exchange.coinbase.com/products"

# =========================
# TELEGRAM
# =========================

def send_telegram(text: str, atomic=False):

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": CHAT_ID,
        "text": text
    }

    # Atomic alerts trigger sound/vibration
    if atomic:
        payload["disable_notification"] = False
    else:
        payload["disable_notification"] = True

    try:
        requests.post(url, data=payload, timeout=10)
    except:
        pass

# =========================
# HELPERS
# =========================

def can_alert(key, cooldown):
    now = time.time()
    last = last_alert.get(key, 0)
    return now - last > cooldown

def mark_alert(key):
    last_alert[key] = time.time()

def format_price(p):

    if p >= 1:
        return f"{p:.4f}"

    if p >= .01:
        return f"{p:.6f}"

    return f"{p:.8f}"

# =========================
# DATA
# =========================

def get_coinbase_symbols():

    try:
        data = requests.get(COINBASE_PRODUCTS, timeout=10).json()

        bases = set()

        for p in data:
            if p["quote_currency"] == "USD":
                bases.add(p["base_currency"])

        return {b + "USDT" for b in bases}

    except:
        return set()

def get_klines(symbol, interval):

    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": 100
    }

    try:
        return requests.get(BINANCE_KLINES, params=params, timeout=10).json()
    except:
        return None

# =========================
# SIGNAL LOGIC
# =========================

def compute_signal(symbol):

    k15 = get_klines(symbol, "15m")
    k5 = get_klines(symbol, "5m")
    k1 = get_klines(symbol, "1m")

    if not k15 or not k5 or not k1:
        return None

    closes15 = [float(x[4]) for x in k15]
    closes5 = [float(x[4]) for x in k5]
    closes1 = [float(x[4]) for x in k1]

    highs5 = [float(x[2]) for x in k5]
    highs1 = [float(x[2]) for x in k1]

    vols5 = [float(x[5]) for x in k5]
    vols1 = [float(x[5]) for x in k1]

    price = closes1[-1]

    if price > MAX_PRICE:
        return None

    breakout5 = price > max(highs5[-20:-1])
    breakout1 = price > max(highs1[-10:-1])

    avg_vol5 = sum(vols5[-20:-1]) / 20
    avg_vol1 = sum(vols1[-20:-1]) / 20

    vol_ratio5 = vols5[-1] / avg_vol5 if avg_vol5 else 0
    vol_ratio1 = vols1[-1] / avg_vol1 if avg_vol1 else 0

    score = 0

    if breakout5:
        score += 3

    if breakout1:
        score += 2

    if vol_ratio5 > 2:
        score += 2

    if vol_ratio1 > 3:
        score += 3

    score = min(score, 10)

    entry_low = price * .995
    entry_high = price * 1.015

    stop = price * .94
    t1 = price * 1.15
    t2 = price * 1.30

    return {
        "symbol": symbol,
        "price": price,
        "score": score,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop": stop,
        "t1": t1,
        "t2": t2,
        "vol_ratio1": vol_ratio1,
        "vol_ratio5": vol_ratio5
    }

# =========================
# MAIN LOOP
# =========================

def main():

    send_telegram("Scanner ONLINE")

    coinbase = get_coinbase_symbols()

    while True:

        try:

            data = requests.get(BINANCE_24H, timeout=10).json()

            for coin in data:

                symbol = coin["symbol"]

                if symbol not in coinbase:
                    continue

                if not symbol.endswith("USDT"):
                    continue

                price = float(coin["lastPrice"])
                volume = float(coin["quoteVolume"])
                change = float(coin["priceChangePercent"])

                if price > MAX_PRICE:
                    continue

                if volume < MIN_VOLUME_USDT:
                    continue

                if change < MIN_CHANGE_PCT:
                    continue

                signal = compute_signal(symbol)

                if not signal:
                    continue

                score = signal["score"]

                # =====================
                # ATOMIC ALERT
                # =====================

                if score >= ATOMIC_SCORE:

                    key = symbol + "_atomic"

                    if not can_alert(key, COOLDOWN_ATOMIC):
                        continue

                    explosion = int(score * 10)

                    msg = (
                        f"🚨🚨🚨 ATOMIC BREAKOUT 🚨🚨🚨\n\n"

                        f"{symbol}\n\n"

                        f"Score: {score}/10\n"
                        f"Explosion Probability: {explosion}%\n\n"

                        f"Entry Zone:\n"
                        f"{format_price(signal['entry_low'])}"
                        f" → "
                        f"{format_price(signal['entry_high'])}\n\n"

                        f"Targets:\n"
                        f"{format_price(signal['t1'])}\n"
                        f"{format_price(signal['t2'])}\n\n"

                        f"Stop:\n"
                        f"{format_price(signal['stop'])}\n\n"

                        f"Volume:\n"
                        f"1m {signal['vol_ratio1']:.2f}x\n"
                        f"5m {signal['vol_ratio5']:.2f}x\n\n"

                        f"EXPLOSIVE CONDITIONS"
                    )

                    send_telegram(msg, atomic=True)

                    mark_alert(key)

                # =====================
                # ELITE ALERT
                # =====================

                elif score >= ELITE_SCORE:

                    key = symbol + "_elite"

                    if not can_alert(key, COOLDOWN_ELITE):
                        continue

                    msg = (
                        f"🚀 Elite Breakout\n\n"
                        f"{symbol}\n"
                        f"Score: {score}/10\n"
                        f"Price: {format_price(price)}"
                    )

                    send_telegram(msg, atomic=False)

                    mark_alert(key)

            time.sleep(SCAN_INTERVAL)

        except Exception as e:

            print("Error:", e)

            time.sleep(10)

# =========================

main()
