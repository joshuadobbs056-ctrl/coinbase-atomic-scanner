import os
import time
import requests
from typing import List, Dict, Any, Optional, Tuple

# =========================
# ENV / CONFIG
# =========================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

COINBASE_API = "https://api.exchange.coinbase.com"
SCAN_INTERVAL_SECONDS = 60

MAX_PRICE_USD = 0.25

# -------- Strict Filters ----------
# Liquidity filters (USD notional volume in last 24h)
MIN_USD_VOL_ATOMIC = 5_000_000
MIN_USD_VOL_SWING = 10_000_000
MIN_USD_VOL_ACCUM = 7_500_000

# Atomic strict rules
ATOMIC_1M_VOL_SPIKE_MULT = 4.0      # last 1m vol >= 400% of 30m avg
ATOMIC_5M_VOL_SPIKE_MULT = 2.5      # last 5m vol >= 250% of 1h avg (5m candles)
ATOMIC_PRICE_POP_3M = 0.025         # +2.5% within 3 minutes
ATOMIC_BREAKOUT_LOOKBACK_1M = 30    # break last 30m high
ATOMIC_MAX_EXTENSION = 0.02         # must be within +2% of breakout (early, not late)
ATOMIC_RSI_MIN = 62                 # RSI>=62
ATOMIC_RSI_PERIOD = 14
ATOMIC_VWAP_LOOKBACK_1M = 30        # VWAP window
ATOMIC_EMA_FAST = 9
ATOMIC_EMA_SLOW = 21

# Accumulation strict rules
ACCUM_LOOKBACK_1M = 90              # analyze last 90 minutes
ACCUM_VOL_UPSHIFT = 1.20            # last 30m avg volume > prior 30m avg * 1.20
ACCUM_NO_SPIKE_MULT = 3.0           # no single 1m candle volume > avg * 3 (avoid already-pumped)
ACCUM_COMPRESSION_RATIO = 0.80      # last 15m avg range < prior 15m avg range * 0.80
ACCUM_EMA_COMPRESS_9_21 = 0.003     # |ema9-ema21| / price < 0.3%
ACCUM_EMA_COMPRESS_21_50 = 0.006    # |ema21-ema50| / price < 0.6%
ACCUM_VWAP_NEAR = 0.005             # price within 0.5% of vwap(30m)
ACCUM_NO_DUMP = -0.02               # no 1m candle <= -2% in last 30m

# Swing strict rules (Coinbase supports 6H, not 4H)
SWING_DAILY_EMA = 20
SWING_6H_EMA_FAST = 20
SWING_6H_EMA_SLOW = 50
SWING_6H_VOL_SPIKE_MULT = 2.0       # last 6H vol >= 2x 20-candle avg
SWING_VOL_RISING_LAST3 = True       # last 3 6H vols increasing
SWING_BREAKOUT_24H = True           # break last 24H high (1H candles)
SWING_ATR_UP = True                 # ATR increasing (last 14 vs prior 14)

# Cooldowns (reduce spam)
COOLDOWN_ATOMIC_MIN = 60
COOLDOWN_ACCUM_MIN = 180
COOLDOWN_SWING_MIN = 720            # 12 hours

# =========================
# UTIL
# =========================

def now_ts() -> int:
    return int(time.time())

def minutes_since(ts: int) -> float:
    if ts <= 0:
        return 1e9
    return (now_ts() - ts) / 60.0

def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def avg(vals: List[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0

def ema_series(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = values[-i] - values[-i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def vwap_from_candles(candles: List[List[float]]) -> Optional[float]:
    # candles: [time, low, high, open, close, volume]
    total_pv = 0.0
    total_v = 0.0
    for c in candles:
        low = safe_float(c[1])
        high = safe_float(c[2])
        close = safe_float(c[4])
        vol = safe_float(c[5])
        typical = (low + high + close) / 3.0
        total_pv += typical * vol
        total_v += vol
    if total_v <= 0:
        return None
    return total_pv / total_v

def true_ranges(candles: List[List[float]]) -> List[float]:
    # TR = max(high-low, abs(high-prev_close), abs(low-prev_close))
    trs = []
    prev_close = None
    for c in candles:
        low = safe_float(c[1])
        high = safe_float(c[2])
        close = safe_float(c[4])
        if prev_close is None:
            trs.append(high - low)
        else:
            trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    return trs

# =========================
# TELEGRAM
# =========================

def send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=15)
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
    # returns (price, base_volume_24h)
    try:
        data = cb_get(f"/products/{product_id}/ticker")
        price = safe_float(data.get("price"))
        vol = safe_float(data.get("volume"))  # base 24h volume
        if price <= 0:
            return None
        return price, vol
    except Exception:
        return None

def fetch_candles(product_id: str, granularity: int, limit: int) -> Optional[List[List[float]]]:
    # returns sorted ascending by time
    try:
        data = cb_get(f"/products/{product_id}/candles", params={"granularity": granularity})
        if not isinstance(data, list) or len(data) == 0:
            return None
        data_sorted = sorted(data, key=lambda x: x[0])
        return data_sorted[-limit:] if len(data_sorted) > limit else data_sorted
    except Exception:
        return None

# =========================
# STATE / COOLDOWNS
# =========================

STATE: Dict[str, Dict[str, int]] = {
    # symbol: {
    #   "atomic": last_ts,
    #   "accum": last_ts,
    #   "swing": last_ts
    # }
}

def st(symbol: str) -> Dict[str, int]:
    if symbol not in STATE:
        STATE[symbol] = {"atomic": 0, "accum": 0, "swing": 0}
    return STATE[symbol]

# =========================
# STRICT SIGNALS
# =========================

def passes_liquidity(price: float, base_vol_24h: float, min_usd: float) -> bool:
    usd_vol = price * base_vol_24h
    return usd_vol >= min_usd

def atomic_breakout(symbol: str, price: float) -> Optional[str]:
    s = st(symbol)
    if minutes_since(s["atomic"]) < COOLDOWN_ATOMIC_MIN:
        return None

    # Need 1m for 30m breakout + 3m pop + VWAP + EMA
    one = fetch_candles(symbol, 60, 120)
    if not one or len(one) < 60:
        return None

    closes = [safe_float(c[4]) for c in one]
    highs = [safe_float(c[2]) for c in one]
    lows = [safe_float(c[1]) for c in one]
    vols = [safe_float(c[5]) for c in one]

    i = len(one) - 1

    # Breakout of last 30m high (excluding current candle)
    lb = ATOMIC_BREAKOUT_LOOKBACK_1M
    if i - lb < 1:
        return None
    breakout_level = max(highs[i - lb:i])
    broke_out = closes[i] > breakout_level

    # Must be early: not > +2% above breakout
    if breakout_level <= 0:
        return None
    extension = (closes[i] - breakout_level) / breakout_level
    early = extension <= ATOMIC_MAX_EXTENSION

    # 1m volume spike: last 1m vol >= 4x 30m avg vol
    vol_avg_30 = avg(vols[i - lb:i])
    if vol_avg_30 <= 0:
        return None
    vol_1m_ok = vols[i] >= vol_avg_30 * ATOMIC_1M_VOL_SPIKE_MULT

    # 5m volume spike: last 5m vol >= 2.5x 1h avg 5m vol
    five = fetch_candles(symbol, 300, 60)
    if not five or len(five) < 15:
        return None
    five_vols = [safe_float(c[5]) for c in five]
    j = len(five) - 1
    avg_1h_5m = avg(five_vols[max(0, j - 12):j])  # prior 12 candles = 60 mins
    if avg_1h_5m <= 0:
        return None
    vol_5m_ok = five_vols[j] >= avg_1h_5m * ATOMIC_5M_VOL_SPIKE_MULT

    # Price pop within 3 minutes
    if i < 3:
        return None
    pop_3m = (closes[i] - closes[i - 3]) / closes[i - 3] if closes[i - 3] > 0 else 0.0
    pop_ok = pop_3m >= ATOMIC_PRICE_POP_3M

    # RSI (use 5m closes for stability), must be >=62 AND rising
    five_closes = [safe_float(c[4]) for c in five]
    r_now = rsi(five_closes, ATOMIC_RSI_PERIOD)
    r_prev = rsi(five_closes[:-1], ATOMIC_RSI_PERIOD) if len(five_closes) > ATOMIC_RSI_PERIOD + 2 else None
    if r_now is None or r_prev is None:
        return None
    rsi_ok = (r_now >= ATOMIC_RSI_MIN) and (r_now > r_prev)

    # VWAP (30m) and EMA filters on 1m
    vwap = vwap_from_candles(one[-ATOMIC_VWAP_LOOKBACK_1M:])
    if vwap is None:
        return None
    ema9 = ema_series(closes[-60:], ATOMIC_EMA_FAST)
    ema21 = ema_series(closes[-60:], ATOMIC_EMA_SLOW)
    if ema9 is None or ema21 is None:
        return None
    above_trend = (price > vwap) and (price > ema9) and (price > ema21)

    # Momentum sanity: last candle not a giant wick-down dump
    last_open = safe_float(one[i][3])
    if last_open > 0:
        last_ret = (closes[i] - last_open) / last_open
    else:
        last_ret = 0.0
    not_red = last_ret > -0.005  # not a nasty red candle

    if not (broke_out and early and vol_1m_ok and vol_5m_ok and pop_ok and rsi_ok and above_trend and not_red):
        return None

    # Build trade levels (atomic)
    entry_low = breakout_level * 0.998
    entry_high = breakout_level * 1.012
    stop = breakout_level * (1 - 0.045)
    t1 = breakout_level * (1 + 0.08)
    t2 = breakout_level * (1 + 0.15)

    score = 9.6  # strict model implies high; keep as informational

    s["atomic"] = now_ts()
    return (
        f"⚛️ ATOMIC BREAKOUT (STRICT)\n"
        f"{symbol}\n\n"
        f"Score: {score:.1f}/10\n"
        f"Breakout: ${breakout_level:.6f}  (ext +{extension*100:.2f}%)\n"
        f"Pop(3m): +{pop_3m*100:.2f}%\n"
        f"RSI(5m): {r_now:.1f} (rising)\n\n"
        f"Entry Zone: ${entry_low:.6f} → ${entry_high:.6f}\n"
        f"Stop: ${stop:.6f}\n"
        f"Targets: T1 ${t1:.6f} | T2 ${t2:.6f}\n\n"
        f"Hold Expectation: ~10m → 6h (momentum window)\n"
        f"Action Speed: FAST"
    )

def accumulation_underway(symbol: str, price: float) -> Optional[str]:
    s = st(symbol)
    if minutes_since(s["accum"]) < COOLDOWN_ACCUM_MIN:
        return None

    one = fetch_candles(symbol, 60, 140)
    if not one or len(one) < (ACCUM_LOOKBACK_1M + 10):
        return None

    window = one[-ACCUM_LOOKBACK_1M:]
    closes = [safe_float(c[4]) for c in window]
    opens = [safe_float(c[3]) for c in window]
    highs = [safe_float(c[2]) for c in window]
    lows = [safe_float(c[1]) for c in window]
    vols = [safe_float(c[5]) for c in window]

    # Volume upshift: last 30m avg > prior 30m avg * 1.20
    if len(vols) < 60:
        return None
    avg_prev30 = avg(vols[-60:-30])
    avg_last30 = avg(vols[-30:])
    if avg_prev30 <= 0:
        return None
    vol_upshift = avg_last30 >= avg_prev30 * ACCUM_VOL_UPSHIFT

    # No pump spike: max volume isn't crazy
    v_avg_total = avg(vols)
    if v_avg_total <= 0:
        return None
    no_spike = max(vols[-30:]) <= v_avg_total * ACCUM_NO_SPIKE_MULT

    # Compression: last 15m avg range < prior 15m avg range * 0.80
    ranges = [(highs[i] - lows[i]) for i in range(len(highs))]
    avg_prev15 = avg(ranges[-30:-15])
    avg_last15 = avg(ranges[-15:])
    if avg_prev15 <= 0:
        return None
    compressing = avg_last15 <= avg_prev15 * ACCUM_COMPRESSION_RATIO

    # EMA compression: 9/21/50 tight (use last 90 closes)
    ema9 = ema_series(closes, 9)
    ema21 = ema_series(closes, 21)
    ema50 = ema_series(closes, 50)
    if ema9 is None or ema21 is None or ema50 is None:
        return None
    if price <= 0:
        return None
    ema_comp_9_21 = abs(ema9 - ema21) / price <= ACCUM_EMA_COMPRESS_9_21
    ema_comp_21_50 = abs(ema21 - ema50) / price <= ACCUM_EMA_COMPRESS_21_50

    # Price near VWAP(30m)
    vwap30 = vwap_from_candles(window[-30:])
    if vwap30 is None or vwap30 <= 0:
        return None
    near_vwap = abs(price - vwap30) / vwap30 <= ACCUM_VWAP_NEAR

    # No dump candle in last 30m: no 1m candle <= -2%
    no_dump = True
    for i in range(len(closes) - 30, len(closes)):
        if opens[i] > 0:
            ret = (closes[i] - opens[i]) / opens[i]
            if ret <= ACCUM_NO_DUMP:
                no_dump = False
                break

    # Gentle higher lows (soft confirmation)
    higher_lows = lows[-10] < lows[-7] < lows[-4] if len(lows) >= 11 else False

    if not (vol_upshift and no_spike and compressing and ema_comp_9_21 and ema_comp_21_50 and near_vwap and no_dump):
        return None

    # Accumulation isn’t a breakout yet -> provide a “watch” plan
    # Watch trigger: break last 60m high
    last60_high = max(highs[-60:]) if len(highs) >= 60 else max(highs)
    watch_trigger = last60_high * 1.001  # tiny buffer
    invalidation = min(lows[-30:]) * 0.995

    s["accum"] = now_ts()
    return (
        f"🟦 ACCUMULATION UNDERWAY\n"
        f"{symbol}\n\n"
        f"Traits: volume upshift + compression + EMA squeeze + near VWAP\n"
        f"Bias: building energy (not a breakout yet)\n\n"
        f"Watch Trigger (breakout): > ${watch_trigger:.6f}\n"
        f"Invalidation (weakness): < ${invalidation:.6f}\n"
        f"Suggested Style: SWING prep / early position\n"
        f"Hold Expectation: ~1–7 days if breakout confirms\n"
        f"Action Speed: MEDIUM"
        + (f"\n\nNote: Higher lows forming." if higher_lows else "")
    )

def swing_trade(symbol: str, price: float) -> Optional[str]:
    s = st(symbol)
    if minutes_since(s["swing"]) < COOLDOWN_SWING_MIN:
        return None

    # Daily trend filter
    daily = fetch_candles(symbol, 86400, 80)
    if not daily or len(daily) < (SWING_DAILY_EMA + 10):
        return None
    d_closes = [safe_float(c[4]) for c in daily]
    d_ema20 = ema_series(d_closes, SWING_DAILY_EMA)
    if d_ema20 is None or d_ema20 <= 0:
        return None
    if price <= d_ema20:
        return None

    # 6H candles for structure + EMA20/EMA50
    six = fetch_candles(symbol, 21600, 120)
    if not six or len(six) < 70:
        return None
    s_closes = [safe_float(c[4]) for c in six]
    s_highs = [safe_float(c[2]) for c in six]
    s_lows = [safe_float(c[1]) for c in six]
    s_vols = [safe_float(c[5]) for c in six]

    ema20_6h = ema_series(s_closes, SWING_6H_EMA_FAST)
    ema50_6h = ema_series(s_closes, SWING_6H_EMA_SLOW)
    if ema20_6h is None or ema50_6h is None:
        return None
    if not (price > ema20_6h and price > ema50_6h):
        return None

    # Volume expansion and rising last 3
    last_vol = s_vols[-1]
    vol_base = avg(s_vols[-21:-1]) if len(s_vols) >= 22 else avg(s_vols[:-1])
    if vol_base <= 0:
        return None
    vol_ok = last_vol >= vol_base * SWING_6H_VOL_SPIKE_MULT

    vol_rising = True
    if SWING_VOL_RISING_LAST3 and len(s_vols) >= 4:
        vol_rising = s_vols[-3] < s_vols[-2] < s_vols[-1]

    # Breakout of last 24h high (use 1H candles last 24)
    broke_24h = True
    if SWING_BREAKOUT_24H:
        oneh = fetch_candles(symbol, 3600, 80)
        if not oneh or len(oneh) < 30:
            return None
        h_highs = [safe_float(c[2]) for c in oneh]
        h_closes = [safe_float(c[4]) for c in oneh]
        last24_high = max(h_highs[-25:-1])  # ~24 hours excluding current
        broke_24h = h_closes[-1] > last24_high

    # ATR increasing (6H)
    atr_ok = True
    if SWING_ATR_UP:
        trs = true_ranges(six[-35:])  # enough for two 14-period averages
        if len(trs) < 30:
            return None
        atr_prev = avg(trs[-28:-14])
        atr_now = avg(trs[-14:])
        atr_ok = atr_now > atr_prev

    # Final swing gate
    if not (vol_ok and vol_rising and broke_24h and atr_ok):
        return None

    # Swing levels (wider)
    entry_low = price * 0.99
    entry_high = price * 1.01
    stop = price * (1 - 0.08)
    t1 = price * (1 + 0.15)
    t2 = price * (1 + 0.30)

    s["swing"] = now_ts()
    return (
        f"📈 SWING TRADE (STRICT)\n"
        f"{symbol}\n\n"
        f"Daily: above EMA20 (${d_ema20:.6f}) ✅\n"
        f"6H: above EMA20/EMA50 ✅\n"
        f"6H Vol: expansion ✅\n"
        f"24H High Break: ✅\n"
        f"ATR: rising ✅\n\n"
        f"Entry Zone: ${entry_low:.6f} → ${entry_high:.6f}\n"
        f"Stop: ${stop:.6f}\n"
        f"Targets: T1 ${t1:.6f} | T2 ${t2:.6f}\n\n"
        f"Hold Expectation: ~1–10 days\n"
        f"Action Speed: SLOW–MEDIUM"
    )

# =========================
# MAIN
# =========================

def main() -> None:
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        send_telegram("✅ Scanner Online: STRICT Atomic + STRICT Swing + Accumulation Underway")
    else:
        print("❌ Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID in Railway Variables.")

    products_cache: List[dict] = []
    cache_ts = 0

    while True:
        try:
            # Refresh products every 6 hours
            if not products_cache or (now_ts() - cache_ts) > 6 * 3600:
                products_cache = fetch_products_usd()
                cache_ts = now_ts()
                print(f"Loaded {len(products_cache)} USD products.")

            alerts_sent = 0

            for p in products_cache:
                symbol = p.get("id")
                if not symbol:
                    continue

                ticker = fetch_ticker(symbol)
                if not ticker:
                    continue

                price, base_vol_24h = ticker

                # Hard cap
                if price > MAX_PRICE_USD:
                    continue

                # Compute USD 24h notional volume
                usd_vol_24h = price * base_vol_24h

                # If liquidity is too low for everything, skip early
                if usd_vol_24h < min(MIN_USD_VOL_ATOMIC, MIN_USD_VOL_ACCUM, MIN_USD_VOL_SWING):
                    continue

                # 1) Atomic (strict) - fastest
                if passes_liquidity(price, base_vol_24h, MIN_USD_VOL_ATOMIC):
                    msg = atomic_breakout(symbol, price)
                    if msg:
                        send_telegram(msg)
                        alerts_sent += 1

                # 2) Accumulation (strict)
                if passes_liquidity(price, base_vol_24h, MIN_USD_VOL_ACCUM):
                    msg = accumulation_underway(symbol, price)
                    if msg:
                        send_telegram(msg)
                        alerts_sent += 1

                # 3) Swing (strict)
                if passes_liquidity(price, base_vol_24h, MIN_USD_VOL_SWING):
                    msg = swing_trade(symbol, price)
                    if msg:
                        send_telegram(msg)
                        alerts_sent += 1

                # Global safety: don't blast too many even if market goes nuts
                if alerts_sent >= 12:
                    break

            print(f"Scan done. Alerts sent: {alerts_sent}. Sleeping {SCAN_INTERVAL_SECONDS}s...")
            time.sleep(SCAN_INTERVAL_SECONDS)

        except Exception as e:
            print("Scanner error:", e)
            time.sleep(10)

if __name__ == "__main__":
    main()
