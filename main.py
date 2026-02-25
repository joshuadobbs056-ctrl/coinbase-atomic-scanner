import os
import time
import math
import requests
from typing import Dict, Any, List, Optional, Tuple

# =========================
# CONFIG
# =========================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

COINBASE_API = "https://api.exchange.coinbase.com"
SCAN_INTERVAL_SECONDS = 60

# Your constraint
MAX_PRICE_USD = 0.25

# Atomic settings (early but filtered)
ATOMIC_LOOKBACK_MINUTES = 15           # breakout vs last 15 minutes high
ATOMIC_AVG_VOL_LOOKBACK = 20           # average volume baseline
ATOMIC_VOL_SPIKE_MULT = 2.5            # must be >= 2.5x average
ATOMIC_MAX_BREAKOUT_EXTENSION = 0.02   # alert only if within 2% above breakout level (avoid late alerts)
ATOMIC_RANGE_EXPANSION_MULT = 1.4      # candle range must be >= 1.4x average range
ATOMIC_COOLDOWN_MINUTES = 30           # don't re-alert atomic too frequently per symbol

# Swing settings (structure + trend + volume)
SWING_RANGE_LOOKBACK_6H = 14           # 14x 6h candles = ~3.5 days range
SWING_VOL_LOOKBACK_6H = 20
SWING_VOL_SPIKE_MULT_6H = 2.0
SWING_DAILY_EMA_PERIOD = 20
SWING_MAX_DAILY_EXTENSION = 0.12       # price can't be > 12% above daily EMA20 (avoid chasing)
SWING_COOLDOWN_HOURS = 12              # don't re-alert swing too frequently per symbol

# Targets / stops (simple, consistent, scalable)
# Atomic tends to move fast: tighter stop, quicker first target
ATOMIC_STOP_PCT = 0.045
ATOMIC_T1_PCT = 0.08
ATOMIC_T2_PCT = 0.15

# Swing holds longer: wider stop, bigger targets
SWING_STOP_PCT = 0.08
SWING_T1_PCT = 0.15
SWING_T2_PCT = 0.30

# =========================
# HELPERS
# =========================

def now_ts() -> int:
    return int(time.time())

def minutes_ago(ts: int) -> float:
    return (now_ts() - ts) / 60.0

def hours_ago(ts: int) -> float:
    return (now_ts() - ts) / 3600.0

def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def ema(values: List[float], period: int) -> Optional[float]:
    """EMA of full series; returns last EMA. Requires len(values) >= period."""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    for v in values[period:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

def avg(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

# =========================
# TELEGRAM
# =========================

def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID env vars.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text
    }
    try:
        requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print("❌ Telegram error:", e)

# =========================
# COINBASE API
# =========================

def cb_get(path: str, params: Optional[dict] = None) -> Any:
    url = f"{COINBASE_API}{path}"
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def fetch_products_usd() -> List[dict]:
    """
    Pull all products and return USD quote pairs that are online and tradable.
    We'll still double-check price <= MAX_PRICE_USD later.
    """
    products = cb_get("/products")
    out = []
    for p in products:
        try:
            if p.get("quote_currency") != "USD":
                continue
            if p.get("status") != "online":
                continue
            if p.get("trading_disabled") is True:
                continue
            out.append(p)
        except Exception:
            continue
    return out

def fetch_ticker(product_id: str) -> Optional[Tuple[float, float]]:
    """
    Returns (price, volume_24h) if available.
    """
    try:
        data = cb_get(f"/products/{product_id}/ticker")
        price = safe_float(data.get("price"))
        vol = safe_float(data.get("volume"))  # base volume 24h
        if price <= 0:
            return None
        return price, vol
    except Exception:
        return None

def fetch_candles(product_id: str, granularity: int, limit: int) -> Optional[List[List[float]]]:
    """
    Coinbase candles endpoint returns list: [time, low, high, open, close, volume]
    Returned order can be newest-first. We'll sort ascending by time.
    """
    try:
        data = cb_get(f"/products/{product_id}/candles", params={"granularity": granularity})
        if not isinstance(data, list) or len(data) == 0:
            return None
        # Sort ascending by time
        data_sorted = sorted(data, key=lambda x: x[0])
        return data_sorted[-limit:] if len(data_sorted) > limit else data_sorted
    except Exception:
        return None

# =========================
# STATE (in-memory)
# =========================

STATE: Dict[str, Dict[str, Any]] = {
    # product_id: {
    #   "atomic": {"stage1_ts": int, "breakout_level": float, "stage1_sent_ts": int, "stage2_sent_ts": int},
    #   "last_atomic_alert_ts": int,
    #   "last_swing_alert_ts": int
    # }
}

def state_for(symbol: str) -> Dict[str, Any]:
    if symbol not in STATE:
        STATE[symbol] = {
            "atomic": {
                "stage1_ts": 0,
                "breakout_level": 0.0,
                "stage1_sent_ts": 0,
                "stage2_sent_ts": 0,
            },
            "last_atomic_alert_ts": 0,
            "last_swing_alert_ts": 0,
        }
    return STATE[symbol]

# =========================
# ATOMIC ENGINE (1m + 5m confirm)
# =========================

def atomic_engine(symbol: str, price: float) -> None:
    st = state_for(symbol)

    # Cooldown
    if minutes_ago(st["last_atomic_alert_ts"]) < ATOMIC_COOLDOWN_MINUTES:
        return

    # Need 1m candles for early breakout detection
    one_min = fetch_candles(symbol, granularity=60, limit=60)
    if not one_min or len(one_min) < max(ATOMIC_AVG_VOL_LOOKBACK + 2, ATOMIC_LOOKBACK_MINUTES + 2):
        return

    # Extract last completed candle as the most recent in list
    # With 60s candles, last item is latest candle (may still be forming, but good enough for this purpose)
    times = [c[0] for c in one_min]
    lows = [safe_float(c[1]) for c in one_min]
    highs = [safe_float(c[2]) for c in one_min]
    opens = [safe_float(c[3]) for c in one_min]
    closes = [safe_float(c[4]) for c in one_min]
    vols = [safe_float(c[5]) for c in one_min]

    i = len(one_min) - 1
    last_close = closes[i]
    last_open = opens[i]
    last_high = highs[i]
    last_low = lows[i]
    last_vol = vols[i]

    # Breakout level = highest high of last N minutes excluding current candle
    lookback_high = max(highs[i - ATOMIC_LOOKBACK_MINUTES:i])  # exclude current
    if lookback_high <= 0:
        return

    # Volume spike vs average of last N candles excluding current
    vol_base = avg(vols[i - ATOMIC_AVG_VOL_LOOKBACK:i])
    if vol_base <= 0:
        return
    vol_spike = last_vol >= vol_base * ATOMIC_VOL_SPIKE_MULT

    # Range expansion (avoid weak candles)
    ranges = [(highs[j] - lows[j]) for j in range(i - ATOMIC_AVG_VOL_LOOKBACK, i)]
    avg_range = avg(ranges) if ranges else 0.0
    last_range = (last_high - last_low)
    range_expanded = (avg_range > 0) and (last_range >= avg_range * ATOMIC_RANGE_EXPANSION_MULT)

    # Breakout condition
    broke_out = last_close > lookback_high

    # Not late: price must still be near the breakout level (within 2%)
    extension = (last_close - lookback_high) / lookback_high
    not_too_late = extension <= ATOMIC_MAX_BREAKOUT_EXTENSION

    if not (broke_out and vol_spike and range_expanded and not_too_late):
        return

    # 5m confirm (optional but helpful): ensure 5m close is also pushing up
    five_min = fetch_candles(symbol, granularity=300, limit=40)
    if not five_min or len(five_min) < 10:
        return
    five_closes = [safe_float(c[4]) for c in five_min]
    # simple confirmation: last 5m close above its 10-period EMA (trend confirmation)
    five_ema10 = ema(five_closes, 10)
    if five_ema10 is None:
        return
    if five_closes[-1] < five_ema10:
        return

    # Stage 1 alert: ATOMIC INITIATED
    breakout_level = lookback_high
    entry_ideal = breakout_level * 1.000  # "ideal" is near breakout level retest
    entry_chase_cap = breakout_level * 1.015  # don't chase beyond +1.5% over breakout level

    stop = entry_ideal * (1 - ATOMIC_STOP_PCT)
    t1 = entry_ideal * (1 + ATOMIC_T1_PCT)
    t2 = entry_ideal * (1 + ATOMIC_T2_PCT)

    msg = (
        f"⚛️ ATOMIC INITIATED (Early)\n"
        f"{symbol}\n\n"
        f"Breakout Level: ${breakout_level:.6f}\n"
        f"Current Price:  ${price:.6f}\n\n"
        f"✅ Entry Window: ${entry_ideal:.6f} → ${entry_chase_cap:.6f}\n"
        f"🎯 Targets: T1 ${t1:.6f} | T2 ${t2:.6f}\n"
        f"🛡 Stop: ${stop:.6f}\n\n"
        f"Notes: Early breakout + volume spike. Best entry is a quick retest near breakout level."
    )
    send_telegram(msg)

    # Store stage tracking for ideal-entry follow-up
    st["atomic"]["stage1_ts"] = now_ts()
    st["atomic"]["breakout_level"] = breakout_level
    st["atomic"]["stage1_sent_ts"] = now_ts()
    st["atomic"]["stage2_sent_ts"] = 0
    st["last_atomic_alert_ts"] = now_ts()


def atomic_followup_entry(symbol: str, price: float) -> None:
    """
    If stage1 fired, watch for pullback into ideal entry zone and alert "IDEAL ENTRY TRIGGERED".
    """
    st = state_for(symbol)
    a = st["atomic"]

    if a["stage1_sent_ts"] == 0:
        return

    # Only watch for follow-up for 60 minutes after stage1
    if minutes_ago(a["stage1_sent_ts"]) > 60:
        return

    if a["stage2_sent_ts"] != 0:
        return

    breakout_level = float(a["breakout_level"])
    if breakout_level <= 0:
        return

    # Ideal entry zone: breakout level +/- 0.6%
    zone_low = breakout_level * 0.994
    zone_high = breakout_level * 1.006

    if zone_low <= price <= zone_high:
        entry = price
        stop = entry * (1 - ATOMIC_STOP_PCT)
        t1 = entry * (1 + ATOMIC_T1_PCT)
        t2 = entry * (1 + ATOMIC_T2_PCT)

        msg = (
            f"✅ IDEAL ENTRY TRIGGERED (Atomic Retest)\n"
            f"{symbol}\n\n"
            f"Entry Zone Hit: ${zone_low:.6f} → ${zone_high:.6f}\n"
            f"Current Price: ${price:.6f}\n\n"
            f"🎯 Targets: T1 ${t1:.6f} | T2 ${t2:.6f}\n"
            f"🛡 Stop: ${stop:.6f}\n\n"
            f"This is the lowest-risk early entry window (breakout retest)."
        )
        send_telegram(msg)
        a["stage2_sent_ts"] = now_ts()

# =========================
# SWING ENGINE (6H + 1D)
# =========================

def swing_engine(symbol: str, price: float) -> None:
    st = state_for(symbol)

    # Cooldown
    if hours_ago(st["last_swing_alert_ts"]) < SWING_COOLDOWN_HOURS:
        return

    # Daily candles (1D)
    daily = fetch_candles(symbol, granularity=86400, limit=60)
    if not daily or len(daily) < (SWING_DAILY_EMA_PERIOD + 5):
        return

    d_closes = [safe_float(c[4]) for c in daily]
    d_ema20 = ema(d_closes, SWING_DAILY_EMA_PERIOD)
    if d_ema20 is None or d_ema20 <= 0:
        return

    # Daily trend filter: price above EMA20
    daily_up = price > d_ema20

    # Not overextended: within +12% of EMA20
    extension = (price - d_ema20) / d_ema20
    not_overextended = extension <= SWING_MAX_DAILY_EXTENSION

    if not (daily_up and not_overextended):
        return

    # 6h candles (Coinbase supports 21600 = 6 hours)
    six_h = fetch_candles(symbol, granularity=21600, limit=80)
    if not six_h or len(six_h) < (SWING_VOL_LOOKBACK_6H + SWING_RANGE_LOOKBACK_6H + 2):
        return

    s_highs = [safe_float(c[2]) for c in six_h]
    s_lows = [safe_float(c[1]) for c in six_h]
    s_closes = [safe_float(c[4]) for c in six_h]
    s_vols = [safe_float(c[5]) for c in six_h]

    i = len(six_h) - 1
    last_close = s_closes[i]
    last_vol = s_vols[i]

    # Structure: higher lows recently (simple but effective)
    # Check last 4 lows are rising (filters chop)
    recent_lows = s_lows[i-4:i]
    if len(recent_lows) < 4:
        return
    higher_lows = recent_lows[0] < recent_lows[1] < recent_lows[2] < recent_lows[3]

    # Breakout: 6h close breaks above prior range highs (last 14 6h candles excluding current)
    range_high = max(s_highs[i - SWING_RANGE_LOOKBACK_6H:i])
    broke_range = last_close > range_high

    # Volume expansion: last vol >= 2x average of last 20 (excluding current)
    vol_base = avg(s_vols[i - SWING_VOL_LOOKBACK_6H:i])
    vol_ok = (vol_base > 0) and (last_vol >= vol_base * SWING_VOL_SPIKE_MULT_6H)

    if not (higher_lows and broke_range and vol_ok):
        return

    # Build swing trade levels
    entry_low = last_close * 0.99
    entry_high = last_close * 1.01
    stop = last_close * (1 - SWING_STOP_PCT)
    t1 = last_close * (1 + SWING_T1_PCT)
    t2 = last_close * (1 + SWING_T2_PCT)

    msg = (
        f"📈 SWING BREAKOUT (Elite Filter)\n"
        f"{symbol}\n\n"
        f"Daily Trend: ✅ Above EMA20 (${d_ema20:.6f})\n"
        f"Not Overextended: ✅ (+{extension*100:.1f}% vs EMA20)\n"
        f"6H Structure: ✅ Higher lows + range breakout\n"
        f"6H Volume: ✅ Expansion\n\n"
        f"Entry Zone: ${entry_low:.6f} → ${entry_high:.6f}\n"
        f"🎯 Targets: T1 ${t1:.6f} | T2 ${t2:.6f}\n"
        f"🛡 Stop: ${stop:.6f}\n"
        f"Hold Window: ~1–7 days (typical)\n"
    )
    send_telegram(msg)
    st["last_swing_alert_ts"] = now_ts()

# =========================
# MAIN LOOP
# =========================

def main() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in Railway Variables.")
    else:
        send_telegram("✅ Atomic + Swing Scanner Online (Atomic 1m + Swing 6H/1D).")

    # Cache product list (refresh periodically)
    products_cache: List[dict] = []
    products_cache_ts = 0

    while True:
        try:
            # refresh products every 6 hours
            if (now_ts() - products_cache_ts) > (6 * 3600) or not products_cache:
                products_cache = fetch_products_usd()
                products_cache_ts = now_ts()
                print(f"Loaded {len(products_cache)} USD products.")

            for p in products_cache:
                symbol = p.get("id")
                if not symbol:
                    continue

                ticker = fetch_ticker(symbol)
                if not ticker:
                    continue

                price, vol24 = ticker

                # enforce price cap
                if price > MAX_PRICE_USD:
                    continue

                # Atomic follow-up first (retest entry)
                atomic_followup_entry(symbol, price)

                # Atomic detection (early)
                atomic_engine(symbol, price)

                # Swing detection (elite filter)
                swing_engine(symbol, price)

            print("Scan complete. Sleeping 60s...")
            time.sleep(SCAN_INTERVAL_SECONDS)

        except Exception as e:
            print("Scanner error:", e)
            time.sleep(10)

if __name__ == "__main__":
    main()
