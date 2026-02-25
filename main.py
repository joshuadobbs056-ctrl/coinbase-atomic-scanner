import requests
import time
import os

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

COINBASE_PRODUCTS = "https://api.exchange.coinbase.com/products"
SCAN_INTERVAL = 60

# STRICT THRESHOLDS
MIN_SWING_SCORE = 8.0
MIN_DAY_SCORE = 8.8
MIN_ATOMIC_SCORE = 9.5

MIN_VOLUME = 1000000  # liquidity protection

print("Institutional Atomic Scanner Running...")

# =========================
# TELEGRAM
# =========================

def send_telegram(message):

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }

    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass


# =========================
# FETCH PRODUCTS
# =========================

def get_products():

    try:

        r = requests.get(COINBASE_PRODUCTS, timeout=10)
        return r.json()

    except:

        return []


# =========================
# FETCH TICKER
# =========================

def get_ticker(symbol):

    try:

        url = f"https://api.exchange.coinbase.com/products/{symbol}/ticker"

        r = requests.get(url, timeout=10)

        data = r.json()

        price = data.get("price")
        volume = data.get("volume")

        if price is None or volume is None:
            return None

        return float(price), float(volume)

    except:

        return None


# =========================
# SCORE CALCULATION
# =========================

def calculate_score(volume):

    score = volume / 500000

    if score > 10:
        score = 10

    return score


# =========================
# TRADE CLASSIFICATION
# =========================

def classify_trade(score, volume):

    if volume < MIN_VOLUME:
        return None

    if score >= MIN_ATOMIC_SCORE:
        return "ATOMIC"

    if score >= MIN_DAY_SCORE:
        return "DAY"

    if score >= MIN_SWING_SCORE:
        return "SWING"

    return None


# =========================
# BUILD ALERT
# =========================

def build_alert(symbol, price, score, trade_type):

    entry_low = price * 0.995
    entry_high = price * 1.01

    target1 = price * 1.06
    target2 = price * 1.12

    stop = price * 0.97

    if trade_type == "ATOMIC":

        return f"""🚨 ATOMIC BREAKOUT 🚨

Symbol: {symbol}

Type: EXPLOSIVE

Score: {score:.2f}

Price: ${price:.6f}

Entry Zone:
${entry_low:.6f} - ${entry_high:.6f}

Targets:
${target1:.6f}
${target2:.6f}

Stop Loss:
${stop:.6f}

Highest confidence breakout.
"""

    if trade_type == "DAY":

        return f"""⚡ DAY TRADE SIGNAL

Symbol: {symbol}

Score: {score:.2f}

Price: ${price:.6f}

Entry Zone:
${entry_low:.6f} - ${entry_high:.6f}

Targets:
${target1:.6f}
${target2:.6f}

Stop Loss:
${stop:.6f}
"""

    if trade_type == "SWING":

        return f"""🚀 SWING TRADE SIGNAL

Symbol: {symbol}

Score: {score:.2f}

Price: ${price:.6f}

Entry Zone:
${entry_low:.6f} - ${entry_high:.6f}

Targets:
${target1:.6f}
${target2:.6f}

Stop Loss:
${stop:.6f}
"""


# =========================
# SCANNER
# =========================

def scan():

    products = get_products()

    for product in products:

        symbol = product.get("id")

        if symbol is None:
            continue

        if not symbol.endswith("USD"):
            continue

        ticker = get_ticker(symbol)

        if ticker is None:
            continue

        price, volume = ticker

        score = calculate_score(volume)

        print(symbol, "Score:", score)

        trade_type = classify_trade(score, volume)

        if trade_type is None:
            continue

        alert = build_alert(symbol, price, score, trade_type)

        send_telegram(alert)


# =========================
# MAIN LOOP
# =========================

while True:

    try:

        scan()

        print("Scan complete")

        time.sleep(SCAN_INTERVAL)

    except Exception as e:

        print("Error:", e)

        time.sleep(10)
