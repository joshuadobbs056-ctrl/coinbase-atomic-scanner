import os, time, json, math, statistics, requests
from datetime import datetime, timezone

# =========================
# Config (tune these)
# =========================

COINBASE_BEARER = os.getenv("COINBASE_BEARER_TOKEN", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Universe filters
QUOTE_ALLOW = {"USD", "USDC"}
MIN_24H_USD_VOL = float(os.getenv("MIN_24H_USD_VOL", "2000000"))  # $2M default
MAX_COINS_SCAN = int(os.getenv("MAX_COINS_SCAN", "400"))          # safety cap

# Alert dedupe/cooldowns (seconds)
COOLDOWN_ACCUM = int(os.getenv("COOLDOWN_ACCUM", str(24*3600)))    # 24h
COOLDOWN_BREAKOUT = int(os.getenv("COOLDOWN_BREAKOUT", str(12*3600)))  # 12h
COOLDOWN_ATOMIC = int(os.getenv("COOLDOWN_ATOMIC", str(2*3600)))   # 2h

STATE_FILE = "state.json"

# Atomic scan settings
ATOMIC_LOOKBACK_MIN = int(os.getenv("ATOMIC_LOOKBACK_MIN", "120"))  # last 120 minutes
ATOMIC_VOL_SPIKE = float(os.getenv("ATOMIC_VOL_SPIKE", "5.0"))      # 5x median
ATOMIC_RANGE_SPIKE = float(os.getenv("ATOMIC_RANGE_SPIKE", "3.0"))  # 3x median range
ATOMIC_CLOSE_NEAR_HIGH = float(os.getenv("ATOMIC_CLOSE_NEAR_HIGH", "0.75")) # top 25% of candle

# Swing/accumulation settings (daily + 1h)
ACCUM_DAYS = int(os.getenv("ACCUM_DAYS", "20"))
ACCUM_ATR_PCT_MAX = float(os.getenv("ACCUM_ATR_PCT_MAX", "6.0"))     # "quiet" regime
ACCUM_OBV_SLOPE_MIN = float(os.getenv("ACCUM_OBV_SLOPE_MIN", "0.0")) # >0 = rising

# Strict breakout settings (daily)
BREAKOUT_LOOKBACK_DAYS = int(os.getenv("BREAKOUT_LOOKBACK_DAYS", "20"))
BREAKOUT_VOL_MULT = float(os.getenv("BREAKOUT_VOL_MULT", "2.5"))     # vol > 2.5x avg
BREAKOUT_CLOSE_NEAR_HIGH = float(os.getenv("BREAKOUT_CLOSE_NEAR_HIGH", "0.7"))

# =========================
# Helpers
# =========================

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_alert": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def now_ts():
    return int(time.time())

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=20)
    if r.status_code != 200:
        print("Telegram error:", r.status_code, r.text)

def cb_headers():
    return {"Authorization": f"Bearer {COINBASE_BEARER}"}

def cb_list_products():
    # Coinbase Advanced Trade API: List Products
    # https://api.coinbase.com/api/v3/brokerage/products  (Bearer required)
    url = "https://api.coinbase.com/api/v3/brokerage/products"
    out = []
    cursor = None
    while True:
        params = {}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(url, headers=cb_headers(), params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        products = data.get("products", [])
        out.extend(products)
        cursor = data.get("cursor")
        if not cursor:
            break
    return out

def cb_get_candles(product_id, start_ts, end_ts, granularity):
    # Coinbase Advanced Trade API: Get Product Candles
    url = f"https://api.coinbase.com/api/v3/brokerage/products/{product_id}/candles"
    params = {"start": str(start_ts), "end": str(end_ts), "granularity": granularity}
    r = requests.get(url, headers=cb_headers(), params=params, timeout=30)
    r.raise_for_status()
    candles = r.json().get("candles", [])
    # candles fields: start, low, high, open, close, volume (strings)
    # Sort oldest->newest
    candles_sorted = sorted(candles, key=lambda x: int(x["start"]))
    return [
        {
            "t": int(c["start"]),
            "o": float(c["open"]),
            "h": float(c["high"]),
            "l": float(c["low"]),
            "c": float(c["close"]),
            "v": float(c["volume"]),
        }
        for c in candles_sorted
    ]

def pct(a, b):
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for x in values[1:]:
        e = x * k + e * (1 - k)
    return e

def atr_pct(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i-1]["c"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    atr = sum(trs[-period:]) / period
    price = candles[-1]["c"]
    return (atr / price) * 100.0 if price else None

def obv(candles):
    if not candles:
        return []
    obv_series = [0.0]
    for i in range(1, len(candles)):
        if candles[i]["c"] > candles[i-1]["c"]:
            obv_series.append(obv_series[-1] + candles[i]["v"])
        elif candles[i]["c"] < candles[i-1]["c"]:
            obv_series.append(obv_series[-1] - candles[i]["v"])
        else:
            obv_series.append(obv_series[-1])
    return obv_series

def slope(values):
    # simple slope using endpoints
    if len(values) < 2:
        return 0.0
    return values[-1] - values[0]

def can_alert(state, product_id, alert_type, cooldown):
    key = f"{product_id}:{alert_type}"
    last = state["last_alert"].get(key, 0)
    return now_ts() - last >= cooldown

def mark_alert(state, product_id, alert_type):
    key = f"{product_id}:{alert_type}"
    state["last_alert"][key] = now_ts()

# =========================
# Signal logic
# =========================

def signal_accumulation(daily):
    # "quiet base + rising OBV" concept
    if len(daily) < ACCUM_DAYS:
        return None

    last = daily[-1]
    atrp = atr_pct(daily, 14)
    if atrp is None or atrp > ACCUM_ATR_PCT_MAX:
        return None

    closes = [c["c"] for c in daily[-ACCUM_DAYS:]]
    highs = [c["h"] for c in daily[-ACCUM_DAYS:]]
    lows  = [c["l"] for c in daily[-ACCUM_DAYS:]]
    rng_pct = pct(max(highs), min(lows))

    # Avoid already-breaking out
    if last["c"] >= max(highs[:-1]):
        return None

    obv_series = obv(daily[-ACCUM_DAYS:])
    obv_sl = slope(obv_series)
    if obv_sl <= ACCUM_OBV_SLOPE_MIN:
        return None

    # Small uptrend bias: higher lows in last N bars
    hl_ok = lows[-1] >= min(lows[:-5]) if len(lows) >= 6 else True

    score = 0
    score += max(0, 10 - atrp)          # quieter = better
    score += min(10, rng_pct / 2)       # base width
    score += 5 if hl_ok else 0
    score += 5 if obv_sl > 0 else 0

    return {"score": round(score, 2), "atr_pct": round(atrp, 2), "range_pct": round(rng_pct, 2)}

def signal_strict_breakout(daily):
    if len(daily) < BREAKOUT_LOOKBACK_DAYS + 1:
        return None

    window = daily[-(BREAKOUT_LOOKBACK_DAYS+1):]
    last = window[-1]
    prior_high = max(c["h"] for c in window[:-1])
    if last["c"] <= prior_high:
        return None

    vols = [c["v"] for c in window[:-1]]
    avg_vol = sum(vols) / len(vols) if vols else 0
    if avg_vol <= 0:
        return None
    if last["v"] < BREAKOUT_VOL_MULT * avg_vol:
        return None

    # Close near high (avoid wick fake)
    candle_range = last["h"] - last["l"]
    if candle_range <= 0:
        return None
    close_pos = (last["c"] - last["l"]) / candle_range
    if close_pos < BREAKOUT_CLOSE_NEAR_HIGH:
        return None

    score = (last["v"] / avg_vol) + close_pos * 2
    return {"score": round(score, 2), "prior_high": prior_high, "vol_mult": round(last["v"]/avg_vol, 2)}

def signal_atomic(min1):
    # last candle vs median of recent candles
    if len(min1) < 30:
        return None
    last = min1[-1]
    look = min1[-ATOMIC_LOOKBACK_MIN:] if len(min1) >= ATOMIC_LOOKBACK_MIN else min1[:]

    vols = [c["v"] for c in look[:-1] if c["v"] > 0]
    ranges = [(c["h"]-c["l"]) for c in look[:-1] if (c["h"]-c["l"]) > 0]
    if len(vols) < 10 or len(ranges) < 10:
        return None

    med_vol = statistics.median(vols)
    med_rng = statistics.median(ranges)
    if med_vol <= 0 or med_rng <= 0:
        return None

    vol_mult = last["v"] / med_vol
    rng_mult = (last["h"] - last["l"]) / med_rng

    if vol_mult < ATOMIC_VOL_SPIKE or rng_mult < ATOMIC_RANGE_SPIKE:
        return None

    # Close near high
    candle_range = last["h"] - last["l"]
    close_pos = (last["c"] - last["l"]) / candle_range if candle_range else 0
    if close_pos < ATOMIC_CLOSE_NEAR_HIGH:
        return None

    score = vol_mult + rng_mult + close_pos
    return {"score": round(score, 2), "vol_mult": round(vol_mult, 2), "rng_mult": round(rng_mult, 2)}

# =========================
# Main loop
# =========================

def main():
    if not COINBASE_BEARER:
        print("❌ Missing COINBASE_BEARER_TOKEN")
        return

    state = load_state()

    # 1) Coinbase-only universe
    products = cb_list_products()
    universe = []
    for p in products:
        # fields vary; keep it defensive
        pid = p.get("product_id") or p.get("productId") or p.get("id")
        if not pid or "-" not in pid:
            continue
        base, quote = pid.split("-", 1)
        if quote not in QUOTE_ALLOW:
            continue
        status = (p.get("status") or "").lower()
        if status and status != "online":
            continue

        # 24h volume filter (best effort)
        # Coinbase may provide volume fields per product; keep fallback
        vol_usd = 0.0
        for key in ["approximate_quote_24h_volume", "quote_volume_24h", "volume_24h", "usd_volume_24h"]:
            if key in p:
                try:
                    vol_usd = float(p[key])
                    break
                except Exception:
                    pass
        if vol_usd and vol_usd < MIN_24H_USD_VOL:
            continue

        universe.append(pid)

    universe = universe[:MAX_COINS_SCAN]
    print(f"Scanning Coinbase products: {len(universe)}")

    # 2) Scan loop
    while True:
        ts_end = now_ts()

        for pid in universe:
            try:
                # Daily candles for accumulation + strict breakout (last ~60 days)
                d_start = ts_end - 90*24*3600
                daily = cb_get_candles(pid, d_start, ts_end, "ONE_DAY")

                acc = signal_accumulation(daily)
                if acc and can_alert(state, pid, "ACCUM", COOLDOWN_ACCUM):
                    msg = (
                        f"🧲 <b>ACCUMULATION UNDERWAY</b>\n"
                        f"<b>{pid}</b>\n"
                        f"Score: <b>{acc['score']}</b>\n"
                        f"ATR%: {acc['atr_pct']} | Base range%: {acc['range_pct']}\n"
                        f"Price: {daily[-1]['c']}"
                    )
                    send_telegram(msg)
                    mark_alert(state, pid, "ACCUM")

                br = signal_strict_breakout(daily)
                if br and can_alert(state, pid, "BREAKOUT", COOLDOWN_BREAKOUT):
                    msg = (
                        f"🚀 <b>STRICT BREAKOUT</b>\n"
                        f"<b>{pid}</b>\n"
                        f"Score: <b>{br['score']}</b>\n"
                        f"Prior high: {br['prior_high']}\n"
                        f"Vol multiple: {br['vol_mult']}\n"
                        f"Price: {daily[-1]['c']}"
                    )
                    send_telegram(msg)
                    mark_alert(state, pid, "BREAKOUT")

                # 1-minute candles for atomic (last few hours)
                m_start = ts_end - (ATOMIC_LOOKBACK_MIN + 10) * 60
                min1 = cb_get_candles(pid, m_start, ts_end, "ONE_MINUTE")

                at = signal_atomic(min1)
                if at and can_alert(state, pid, "ATOMIC", COOLDOWN_ATOMIC):
                    msg = (
                        f"⚠️ <b>ATOMIC BREAKOUT</b>\n"
                        f"<b>{pid}</b>\n"
                        f"Score: <b>{at['score']}</b>\n"
                        f"Vol spike: {at['vol_mult']}x | Range spike: {at['rng_mult']}x\n"
                        f"Price: {min1[-1]['c']}"
                    )
                    send_telegram(msg)
                    mark_alert(state, pid, "ATOMIC")

            except Exception as e:
                print(f"{pid} error: {e}")

        save_state(state)
        print("Scan complete. Sleeping 60s...")
        time.sleep(60)

if __name__ == "__main__":
    main()
