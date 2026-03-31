import os
import time
import requests
from collections import deque
from typing import Optional, Tuple

# ============================================================
# BTC POSITIVE TURNAROUND ALERT BOT
# ============================================================
# - Monitors BTC-USD only
# - Sends Telegram alerts on bullish turnaround conditions
# - Sends Telegram status updates every 10 minutes
# - No trading logic
# - No ML
# ============================================================

# ================= CONFIG =================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

PRODUCT = os.getenv("PRODUCT", "BTC-USD").strip()

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
STATUS_UPDATE_INTERVAL = int(os.getenv("STATUS_UPDATE_INTERVAL", "600"))
REVERSAL_ALERT_COOLDOWN = int(os.getenv("REVERSAL_ALERT_COOLDOWN", "1800"))

MAX_HISTORY = int(os.getenv("MAX_HISTORY", "120"))

FAST_MA_PERIOD = int(os.getenv("FAST_MA_PERIOD", "20"))
SLOW_MA_PERIOD = int(os.getenv("SLOW_MA_PERIOD", "50"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
BREAKOUT_LOOKBACK = int(os.getenv("BREAKOUT_LOOKBACK", "20"))
VOLUME_MULT = float(os.getenv("VOLUME_MULT", "1.30"))

ENABLE_TELEGRAM_REMOTE_STOP = os.getenv("ENABLE_TELEGRAM_REMOTE_STOP", "true").strip().lower() == "true"
TELEGRAM_COMMAND_POLL_SECONDS = float(os.getenv("TELEGRAM_COMMAND_POLL_SECONDS", "5"))
TELEGRAM_OFFSET_FILE = os.getenv("TELEGRAM_OFFSET_FILE", "btc_alert_telegram_offset.txt")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "BTC-Turnaround-Alert-Bot/1.0"})

price_history = deque(maxlen=MAX_HISTORY)
volume_history = deque(maxlen=MAX_HISTORY)

last_status_update = 0.0
last_reversal_alert = 0.0
last_telegram_command_check = 0.0
telegram_update_offset = 0

# ================= TELEGRAM =================

def send(msg: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(msg)
        return

    try:
        r = SESSION.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=15
        )
        if r.status_code != 200:
            print(f"Telegram error {r.status_code}: {r.text}")
    except Exception as e:
        print(f"Telegram send failed: {e}")

def load_telegram_offset() -> int:
    try:
        if not os.path.exists(TELEGRAM_OFFSET_FILE):
            return 0
        with open(TELEGRAM_OFFSET_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip() or "0")
    except Exception:
        return 0

def save_telegram_offset(offset: int) -> None:
    try:
        with open(TELEGRAM_OFFSET_FILE, "w", encoding="utf-8") as f:
            f.write(str(int(offset)))
    except Exception as e:
        print(f"Failed to save telegram offset: {e}")

def get_latest_telegram_updates() -> list:
    global telegram_update_offset

    if not TELEGRAM_TOKEN or not CHAT_ID:
        return []

    params = {"timeout": 0}
    if telegram_update_offset > 0:
        params["offset"] = telegram_update_offset

    try:
        r = SESSION.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params=params,
            timeout=15
        )
        if r.status_code != 200:
            print(f"Telegram getUpdates error {r.status_code}: {r.text}")
            return []

        payload = r.json()
        updates = payload.get("result", [])

        if updates:
            max_update_id = max(int(u.get("update_id", 0)) for u in updates)
            telegram_update_offset = max_update_id + 1
            save_telegram_offset(telegram_update_offset)

        return updates

    except Exception as e:
        print(f"Telegram command fetch failed: {e}")
        return []

def check_remote_stop_command() -> bool:
    global last_telegram_command_check

    if not ENABLE_TELEGRAM_REMOTE_STOP:
        return False

    now = time.time()
    if now - last_telegram_command_check < TELEGRAM_COMMAND_POLL_SECONDS:
        return False

    last_telegram_command_check = now
    updates = get_latest_telegram_updates()

    for update in updates:
        message = update.get("message", {})
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", "")).strip()

        if chat_id != CHAT_ID:
            continue

        text = str(message.get("text", "")).strip().lower()

        if text in {"/stop", "stop", "/shutdown", "shutdown"}:
            send("🛑 BTC alert bot stop command received. Shutting down.")
            return True

    return False

# ================= MARKET DATA =================

def get_candle(product: str) -> Optional[Tuple[float, float]]:
    url = f"https://api.coinbase.com/api/v3/brokerage/market/products/{product}/candles"
    params = {
        "granularity": "FIVE_MINUTE",
        "limit": 1
    }

    try:
        r = SESSION.get(url, params=params, timeout=20)
        if r.status_code != 200:
            print(f"Coinbase candle error {product}: {r.status_code} {r.text}")
            return None

        payload = r.json()
        candles = payload.get("candles", [])
        if not candles:
            return None

        latest = candles[0]
        close_price = float(latest["close"])
        volume = float(latest["volume"])
        return close_price, volume

    except Exception as e:
        print(f"Failed to fetch candle for {product}: {e}")
        return None

# ================= INDICATORS =================

def sma(values, period: int) -> Optional[float]:
    if len(values) < period:
        return None
    recent = list(values)[-period:]
    return sum(recent) / period

def calculate_rsi(prices, period: int = 14) -> Optional[float]:
    prices = list(prices)
    if len(prices) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(-period, 0):
        change = prices[i] - prices[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def get_recent_high(prices, lookback: int) -> Optional[float]:
    prices = list(prices)
    if len(prices) < lookback + 1:
        return None
    window = prices[-(lookback + 1):-1]
    if not window:
        return None
    return max(window)

def get_average_volume(volumes, lookback: int) -> Optional[float]:
    volumes = list(volumes)
    if len(volumes) < lookback + 1:
        return None
    window = volumes[-(lookback + 1):-1]
    if not window:
        return None
    return sum(window) / len(window)

# ================= SIGNAL LOGIC =================

def get_turnaround_signal() -> Optional[dict]:
    if len(price_history) < max(SLOW_MA_PERIOD, RSI_PERIOD + 1, BREAKOUT_LOOKBACK + 1):
        return None

    current_price = price_history[-1]
    current_volume = volume_history[-1]

    fast_ma = sma(price_history, FAST_MA_PERIOD)
    slow_ma = sma(price_history, SLOW_MA_PERIOD)
    rsi = calculate_rsi(price_history, RSI_PERIOD)
    recent_high = get_recent_high(price_history, BREAKOUT_LOOKBACK)
    avg_volume = get_average_volume(volume_history, BREAKOUT_LOOKBACK)

    if fast_ma is None or slow_ma is None or rsi is None or recent_high is None or avg_volume is None:
        return None

    prev_fast_ma = sma(list(price_history)[:-1], FAST_MA_PERIOD)
    prev_slow_ma = sma(list(price_history)[:-1], SLOW_MA_PERIOD)

    fast_cross_up = (
        prev_fast_ma is not None and
        prev_slow_ma is not None and
        prev_fast_ma <= prev_slow_ma and
        fast_ma > slow_ma
    )

    price_above_slow = current_price > slow_ma
    momentum_ok = rsi >= 52.0
    breakout_ok = current_price > recent_high
    volume_ok = current_volume > (avg_volume * VOLUME_MULT)

    if fast_cross_up and price_above_slow and momentum_ok and breakout_ok and volume_ok:
        return {
            "price": current_price,
            "fast_ma": fast_ma,
            "slow_ma": slow_ma,
            "rsi": rsi,
            "recent_high": recent_high,
            "current_volume": current_volume,
            "avg_volume": avg_volume
        }

    return None

# ================= STATUS =================

def send_status_update() -> None:
    current_price = price_history[-1] if price_history else None
    fast_ma = sma(price_history, FAST_MA_PERIOD)
    slow_ma = sma(price_history, SLOW_MA_PERIOD)
    rsi = calculate_rsi(price_history, RSI_PERIOD)
    recent_high = get_recent_high(price_history, BREAKOUT_LOOKBACK)
    avg_volume = get_average_volume(volume_history, BREAKOUT_LOOKBACK)
    current_volume = volume_history[-1] if volume_history else None

    lines = [
        "📊 BTC 10-MIN STATUS UPDATE",
        f"Product: {PRODUCT}",
        f"Price: {f'${current_price:,.2f}' if current_price is not None else 'n/a'}",
        f"Fast MA ({FAST_MA_PERIOD}): {f'${fast_ma:,.2f}' if fast_ma is not None else 'n/a'}",
        f"Slow MA ({SLOW_MA_PERIOD}): {f'${slow_ma:,.2f}' if slow_ma is not None else 'n/a'}",
        f"RSI ({RSI_PERIOD}): {f'{rsi:.2f}' if rsi is not None else 'n/a'}",
        f"Recent High ({BREAKOUT_LOOKBACK}): {f'${recent_high:,.2f}' if recent_high is not None else 'n/a'}",
        f"Current Volume: {f'{current_volume:,.2f}' if current_volume is not None else 'n/a'}",
        f"Avg Volume: {f'{avg_volume:,.2f}' if avg_volume is not None else 'n/a'}",
        f"Remote Stop: {'ON' if ENABLE_TELEGRAM_REMOTE_STOP else 'OFF'}"
    ]

    send("\n".join(lines))

def send_reversal_alert(signal: dict) -> None:
    send(
        "🚨 BTC POSITIVE TURNAROUND DETECTED 🚨\n"
        f"Price: ${signal['price']:,.2f}\n"
        f"Fast MA ({FAST_MA_PERIOD}): ${signal['fast_ma']:,.2f}\n"
        f"Slow MA ({SLOW_MA_PERIOD}): ${signal['slow_ma']:,.2f}\n"
        f"RSI: {signal['rsi']:.2f}\n"
        f"Recent High: ${signal['recent_high']:,.2f}\n"
        f"Volume: {signal['current_volume']:,.2f}\n"
        f"Avg Volume: {signal['avg_volume']:,.2f}\n\n"
        "Bullish turnaround conditions aligned."
    )

# ================= STARTUP =================

def startup() -> None:
    global telegram_update_offset, last_status_update
    telegram_update_offset = load_telegram_offset()
    last_status_update = time.time()

    send(
        "🚀 BTC POSITIVE TURNAROUND ALERT BOT STARTED\n"
        f"Product: {PRODUCT}\n"
        f"Scan Interval: {SCAN_INTERVAL}s\n"
        f"Status Interval: {STATUS_UPDATE_INTERVAL}s\n"
        f"Reversal Cooldown: {REVERSAL_ALERT_COOLDOWN}s"
    )

# ================= MAIN =================

startup()

while True:
    try:
        if check_remote_stop_command():
            break

        candle = get_candle(PRODUCT)
        if candle:
            price, volume = candle
            price_history.append(price)
            volume_history.append(volume)

            signal = get_turnaround_signal()
            now = time.time()

            if signal and (now - last_reversal_alert >= REVERSAL_ALERT_COOLDOWN):
                send_reversal_alert(signal)
                last_reversal_alert = now

            if now - last_status_update >= STATUS_UPDATE_INTERVAL:
                send_status_update()
                last_status_update = now

        time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        send("🛑 BTC alert bot stopped manually.")
        break
    except Exception as e:
        print(f"Main loop error: {e}")
        time.sleep(5)

send("✅ BTC alert bot offline.")
