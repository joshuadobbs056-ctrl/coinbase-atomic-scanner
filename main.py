import requests
import time
import os

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

COINBASE_URL = "https://api.exchange.coinbase.com/products"

SCAN_INTERVAL = 60

# Elite thresholds (swing optimized)
ATOMIC_SCORE_THRESHOLD = 9.5
BREAKOUT_SCORE_THRESHOLD = 7.5

print("Coinbase Atomic Scanner started...")

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }

        requests.post(url, json=payload, timeout=10)

    except Exception as e:
        print("Telegram error:", e)


def fetch_products():
    try:
        r = requests.get(COINBASE_URL, timeout=10)
        return r.json()
    except Exception as e:
        print("Fetch products error:", e)
        return []


def fetch_ticker(product_id):
    try:
        url = f"https://api.exchange.coinbase.com/products/{product_id}/ticker"
        r = requests.get(url, timeout=10)
        data = r.json()

        price = data.get("price")
        volume = data.get("volume")

        if price is None or volume is None:
            return None

        return {
            "price": float(price),
            "volume": float(volume)
        }

    except Exception as e:
        print("Ticker error:", e)
        return None


def calculate_score(volume):
    score = min(10, volume / 1000000 * 10)
    return score


def atomic_alert(symbol, price, score):

    entry = price * 0.985
    target = price * 1.08
    stop = price * 0.965

    message = f"""🚨🚨🚨 ATOMIC BREAKOUT DETECTED 🚨🚨🚨

Symbol: {symbol}

Price: ${price:.6f}

Entry: ${entry:.6f}
Target: ${target:.6f}
Stop: ${stop:.6f}

Momentum Score: {score:.2f}

⚠️ PRIORITY ALERT ⚠️
Explosive move potential detected
"""

    send_telegram(message)


def breakout_alert(symbol, price, score):

    entry = price * 0.99
    target = price * 1.04
    stop = price * 0.975

    message = f"""🚀 Breakout Detected

Symbol: {symbol}

Price: ${price:.6f}

Entry: ${entry:.6f}
Target: ${target:.6f}
Stop: ${stop:.6f}

Momentum Score: {score:.2f}
"""

    send_telegram(message)


def scan():

    products = fetch_products()

    for product in products:

        symbol = product.get("id")

        if symbol is None:
            continue

        if not symbol.endswith("USD"):
            continue

        ticker = fetch_ticker(symbol)

        if ticker is None:
            continue

        price = ticker["price"]
        volume = ticker["volume"]

        score = calculate_score(volume)

        print(symbol, "Score:", score)

        if score >= ATOMIC_SCORE_THRESHOLD:

            atomic_alert(symbol, price, score)

        elif score >= BREAKOUT_SCORE_THRESHOLD:

            breakout_alert(symbol, price, score)


while True:

    try:

        scan()

        print("Scan complete. Sleeping 60 seconds...")

        time.sleep(SCAN_INTERVAL)

    except Exception as e:

        print("Scanner error:", e)

        time.sleep(10)
