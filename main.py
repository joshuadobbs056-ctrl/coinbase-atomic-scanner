import requests
import time
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

COINBASE_API = "https://api.exchange.coinbase.com/products"
BINANCE_API = "https://api.binance.com/api/v3/ticker/24hr"

sent_alerts = {}

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    requests.post(url, data=data)

def get_coinbase_pairs():
    r = requests.get(COINBASE_API)
    products = r.json()
    return [p['id'].replace("-USD","USDT") for p in products if "USD" in p['id']]

def get_market_data():
    r = requests.get(BINANCE_API)
    return r.json()

def calculate_score(coin):

    price = float(coin['lastPrice'])
    volume = float(coin['quoteVolume'])
    change = float(coin['priceChangePercent'])

    score = 0

    if change > 5:
        score += 2

    if change > 10:
        score += 3

    if volume > 500000:
        score += 2

    if volume > 2000000:
        score += 3

    return score

def detect_atomic_pump(coin):

    change = float(coin['priceChangePercent'])
    volume = float(coin['quoteVolume'])

    if change > 15 and volume > 1000000:
        return True

    return False

def ideal_entry(price):

    entry = price * 0.98
    exit_target = price * 1.12
    stop_loss = price * 0.94

    return entry, exit_target, stop_loss

def scan():

    coinbase_pairs = get_coinbase_pairs()
    market = get_market_data()

    for coin in market:

        symbol = coin['symbol']

        if symbol not in coinbase_pairs:
            continue

        price = float(coin['lastPrice'])

        if price > 0.25:
            continue

        score = calculate_score(coin)

        if score >= 5:

            entry, target, stop = ideal_entry(price)

            alert_id = symbol + "breakout"

            if alert_id not in sent_alerts:

                message = f"""
🚀 BREAKOUT DETECTED

Coin: {symbol}
Price: ${price:.6f}

Ideal Entry: ${entry:.6f}
Target Exit: ${target:.6f}
Stop Loss: ${stop:.6f}

Momentum Score: {score}
"""

                send_telegram(message)
                sent_alerts[alert_id] = True

        if detect_atomic_pump(coin):

            entry, target, stop = ideal_entry(price)

            alert_id = symbol + "atomic"

            if alert_id not in sent_alerts:

                message = f"""
⚠️ ATOMIC PUMP DETECTED ⚠️

Coin: {symbol}
Price: ${price:.6f}

Explosive movement detected

Ideal Entry: ${entry:.6f}
Target Exit: ${target:.6f}
Stop Loss: ${stop:.6f}
"""

                send_telegram(message)
                sent_alerts[alert_id] = True


send_telegram("🚀 Atomic pump scanner is LIVE")

while True:

    try:
        scan()
        time.sleep(60)

    except Exception as e:
        print(e)
        time.sleep(60)
