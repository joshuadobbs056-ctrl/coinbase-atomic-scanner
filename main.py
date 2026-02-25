import requests
import time
import os

# Environment variables from Railway
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# Coinbase API endpoint
COINBASE_URL = "https://api.exchange.coinbase.com/products"

# Track last prices
last_prices = {}

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": CHAT_ID,
            "text": message
        }
        requests.post(url, data=data, timeout=10)
        print(f"Telegram alert sent: {message}")
    except Exception as e:
        print(f"Telegram error: {e}")

def get_coinbase_prices():
    try:
        response = requests.get(COINBASE_URL, timeout=10)
        products = response.json()

        prices = {}

        for product in products:
            symbol = product["id"]

            ticker_url = f"https://api.exchange.coinbase.com/products/{symbol}/ticker"
            ticker = requests.get(ticker_url, timeout=10).json()

            if "price" in ticker:
                prices[symbol] = float(ticker["price"])

        return prices

    except Exception as e:
        print(f"Coinbase fetch error: {e}")
        return {}

print("Coinbase Atomic Scanner started...")

# Main loop runs forever
while True:
    try:
        prices = get_coinbase_prices()

        for symbol, price in prices.items():

            if symbol not in last_prices:
                last_prices[symbol] = price
                continue

            old_price = last_prices[symbol]

            percent_change = ((price - old_price) / old_price) * 100

            # Alert threshold
            if percent_change >= 2:
                message = f"🚀 PUMP DETECTED\n{symbol}\nPrice: {price}\nChange: {percent_change:.2f}%"
                send_telegram(message)

            last_prices[symbol] = price

        print("Scan complete. Sleeping 60 seconds...")
        time.sleep(60)

    except Exception as e:
        print(f"Main loop error: {e}")
        time.sleep(60)
