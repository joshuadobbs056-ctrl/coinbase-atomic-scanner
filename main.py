import requests
import time
import pandas as pd
import numpy as np
import asyncio
from datetime import datetime, timedelta
from telegram import Bot

# =====================================
# CONFIG
# =====================================

TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"

COINGECKO_URL = "https://api.coingecko.com/api/v3"

SCAN_INTERVAL = 300  # 5 minutes
MIN_SCORE_ALERT = 7

# Track accumulation start times
accumulation_tracker = {}

# Prevent duplicate alerts
last_alert_time = {}

ALERT_COOLDOWN = 3600  # seconds (1 hour)

# =====================================
# TELEGRAM SEND FUNCTION (ASYNC SAFE)
# =====================================

async def send_alert_async(message):
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=message
    )

def send_alert(message):
    asyncio.run(send_alert_async(message))


# =====================================
# FETCH COINBASE COINS
# =====================================

def get_coinbase_coins():

    url = f"{COINGECKO_URL}/coins/markets"

    params = {
        "vs_currency": "usd",
        "order": "volume_desc",
        "per_page": 250,
        "page": 1,
        "sparkline": False
    }

    response = requests.get(url)

    if response.status_code != 200:
        return []

    data = response.json()

    coinbase_coins = []

    for coin in data:

        # Filter for strong volume and legit coins
        if coin["total_volume"] > 1000000:
            coinbase_coins.append(coin)

    return coinbase_coins


# =====================================
# FETCH PRICE HISTORY
# =====================================

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


# =====================================
# CALCULATE ACCUMULATION SCORE
# =====================================

def calculate_score(prices, volumes, coin):

    score = 0

    prices = np.array(prices)
    volumes = np.array(volumes)

    if len(prices) < 24:
        return 0, 0, 0

    recent_prices = prices[-12:]
    recent_volumes = volumes[-12:]

    # PRICE COMPRESSION
    price_range = (
        (max(recent_prices) - min(recent_prices))
        / np.mean(recent_prices)
        * 100
    )

    if price_range < 2.5:
        score += 2
    elif price_range < 4:
        score += 1

    # VOLUME TREND
    slope = np.polyfit(range(len(recent_volumes)), recent_volumes, 1)[0]

    if slope > 0:
        score += 2

    # HIGHER LOWS
    lows = pd.Series(recent_prices).rolling(3).min()

    if lows.iloc[-1] > lows.iloc[0]:
        score += 2

    # RESISTANCE DISTANCE
    resistance = max(prices[-24:])
    current_price = prices[-1]

    resistance_distance = (
        (resistance - current_price)
        / resistance
        * 100
    )

    if resistance_distance < 3:
        score += 2
    elif resistance_distance < 6:
        score += 1

    # VOLUME STABILITY
    volume_std = np.std(recent_volumes) / np.mean(recent_volumes)

    if volume_std < 0.5:
        score += 1

    # MARKET CAP BONUS
    if coin["market_cap"] and coin["market_cap"] < 5000000000:
        score += 1

    return round(score, 1), price_range, resistance_distance


# =====================================
# ACCUMULATION DURATION
# =====================================

def get_accumulation_duration(coin_id, score):

    now = datetime.utcnow()

    if score >= MIN_SCORE_ALERT:

        if coin_id not in accumulation_tracker:
            accumulation_tracker[coin_id] = now

        duration = now - accumulation_tracker[coin_id]

        return duration

    else:

        if coin_id in accumulation_tracker:
            del accumulation_tracker[coin_id]

        return None


# =====================================
# FORMAT ALERT
# =====================================

def format_alert(coin, score, duration, price_range, resistance_distance):

    total_hours = int(duration.total_seconds() / 3600)

    days = total_hours // 24
    hours = total_hours % 24

    duration_str = f"{days}d {hours}h"

    score_block = f"""
██████████████
█ SCORE: {score}/10 █
██████████████
"""

    message = f"""
ACCUMULATION ALERT

{score_block}

Coin: {coin['symbol'].upper()}
Price: ${coin['current_price']}

Accumulation Duration: {duration_str}

Range Tightness: {round(price_range,2)}%
Resistance Distance: {round(resistance_distance,2)}%

24h Change: {coin['price_change_percentage_24h']:.2f}%

Volume: ${coin['total_volume']:,}

Signal Type: Accumulation
"""

    return message


# =====================================
# SHOULD ALERT CHECK
# =====================================

def should_alert(coin_id):

    now = time.time()

    if coin_id not in last_alert_time:
        last_alert_time[coin_id] = now
        return True

    if now - last_alert_time[coin_id] > ALERT_COOLDOWN:
        last_alert_time[coin_id] = now
        return True

    return False


# =====================================
# MAIN SCAN LOOP
# =====================================

def scan():

    print("Scanning accumulation setups...")

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

            if not should_alert(coin_id):
                continue

            message = format_alert(
                coin,
                score,
                duration,
                price_range,
                resistance_distance
            )

            print(message)

            send_alert(message)


# =====================================
# RUN FOREVER
# =====================================

if __name__ == "__main__":

    while True:

        try:

            scan()

            time.sleep(SCAN_INTERVAL)

        except Exception as e:

            print("Error:", e)

            time.sleep(30)
