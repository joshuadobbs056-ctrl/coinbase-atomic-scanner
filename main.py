import requests
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from telegram import Bot

# ==========================
# CONFIG
# ==========================

TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"

bot = Bot(token=TELEGRAM_TOKEN)

COINGECKO_URL = "https://api.coingecko.com/api/v3"

SCAN_INTERVAL = 300  # seconds (5 min)
MIN_SCORE_ALERT = 7

# Track accumulation start times
accumulation_tracker = {}

# ==========================
# FETCH COINS FROM COINBASE
# ==========================

def get_coinbase_coins():
    url = f"{COINGECKO_URL}/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "volume_desc",
        "per_page": 250,
        "page": 1,
        "sparkline": False
    }

    response = requests.get(url, params=params)
    data = response.json()

    coinbase_coins = []

    for coin in data:
        if "coinbase" in str(coin).lower():
            coinbase_coins.append(coin)

    return coinbase_coins


# ==========================
# FETCH PRICE HISTORY
# ==========================

def get_price_history(coin_id):

    url = f"{COINGECKO_URL}/coins/{coin_id}/market_chart"

    params = {
        "vs_currency": "usd",
        "days": 2,
        "interval": "hourly"
    }

    response = requests.get(url, params=params)

    if response.status_code != 200:
        return None

    data = response.json()

    prices = [p[1] for p in data["prices"]]
    volumes = [v[1] for v in data["total_volumes"]]

    return prices, volumes


# ==========================
# CALCULATE ACCUMULATION SCORE
# ==========================

def calculate_score(prices, volumes, coin):

    score = 0

    prices = np.array(prices)
    volumes = np.array(volumes)

    # 1. Price compression
    price_range = (max(prices[-12:]) - min(prices[-12:])) / np.mean(prices[-12:]) * 100

    if price_range < 2.5:
        score += 2
    elif price_range < 4:
        score += 1

    # 2. Volume trend
    volume_slope = np.polyfit(range(len(volumes[-12:])), volumes[-12:], 1)[0]

    if volume_slope > 0:
        score += 2

    # 3. Higher lows
    lows = pd.Series(prices[-12:]).rolling(3).min()

    if lows.iloc[-1] > lows.iloc[0]:
        score += 2

    # 4. Resistance proximity
    resistance = max(prices[-24:])
    current_price = prices[-1]

    resistance_distance = (resistance - current_price) / resistance * 100

    if resistance_distance < 3:
        score += 2
    elif resistance_distance < 6:
        score += 1

    # 5. Volume stability
    volume_std = np.std(volumes[-12:]) / np.mean(volumes[-12:])

    if volume_std < 0.5:
        score += 1

    # 6. Market cap preference
    if coin["market_cap"] < 5000000000:
        score += 1

    return round(score, 1), price_range, resistance_distance


# ==========================
# ACCUMULATION DURATION
# ==========================

def get_accumulation_duration(coin_id, score):

    now = datetime.utcnow()

    if score >= MIN_SCORE_ALERT:

        if coin_id not in accumulation_tracker:
            accumulation_tracker[coin_id] = now

        duration = now - accumulation_tracker[coin_id]

    else:
        if coin_id in accumulation_tracker:
            del accumulation_tracker[coin_id]
        return None

    return duration


# ==========================
# FORMAT TELEGRAM ALERT
# ==========================

def format_alert(coin, score, duration, price_range, resistance_distance):

    hours = int(duration.total_seconds() / 3600)
    days = hours // 24
    hours = hours % 24

    duration_str = f"{days}d {hours}h"

    score_display = f"""
██████████████
█ SCORE: {score}/10 █
██████████████
"""

    message = f"""
ACCUMULATION ALERT

{score_display}

Coin: {coin['symbol'].upper()}
Price: ${coin['current_price']}

Accumulation Duration: {duration_str}
Range Tightness: {round(price_range,2)}%
Resistance Distance: {round(resistance_distance,2)}%

24h Change: {coin['price_change_percentage_24h']:.2f}%
Volume: ${coin['total_volume']:,}

Exchange: Coinbase
Signal: Accumulation
"""

    return message


# ==========================
# SEND TELEGRAM ALERT
# ==========================

def send_alert(message):

    bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=message
    )


# ==========================
# MAIN SCANNER LOOP
# ==========================

def scan():

    print("Scanning for accumulation setups...")

    coins = get_coinbase_coins()

    for coin in coins:

        coin_id = coin["id"]

        history = get_price_history(coin_id)

        if history is None:
            continue

        prices, volumes = history

        score, price_range, resistance_distance = calculate_score(
            prices, volumes, coin
        )

        duration = get_accumulation_duration(coin_id, score)

        if duration is None:
            continue

        if score >= MIN_SCORE_ALERT:

            message = format_alert(
                coin,
                score,
                duration,
                price_range,
                resistance_distance
            )

            print(message)

            send_alert(message)


# ==========================
# RUN LOOP
# ==========================

if __name__ == "__main__":

    while True:

        try:
            scan()
            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            print("Error:", e)
            time.sleep(30)
