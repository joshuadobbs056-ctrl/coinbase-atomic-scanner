import os
import time
import math
import json
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests
import numpy as np
import pandas as pd


# =========================
# Config
# =========================

COINBASE_BASE = "https://api.exchange.coinbase.com"  # public market data
UA = "money-printer-scanner/1.0"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MAX_COINS = int(os.getenv("MAX_COINS", "250"))
MIN_24H_USD_VOL = float(os.getenv("MIN_24H_USD_VOL", "2000000"))  # $2M/day liquidity gate

ATOMIC_COOLDOWN_MIN = int(os.getenv("ATOMIC_COOLDOWN_MIN", "180"))
ACC_COOLDOWN_MIN = int(os.getenv("ACC_COOLDOWN_MIN", "360"))
SWING_COOLDOWN_MIN = int(os.getenv("SWING_COOLDOWN_MIN", "720"))

# scan cadence
ATOMIC_EVERY_SEC = 60
ACC_EVERY_SEC = 10 * 60
SWING_EVERY_SEC = 60 * 60

# strict filters
STABLE_BASES = {"USDC", "USDT", "DAI", "TUSD", "USDP", "FDUSD", "EURC"}  # exclude stablecoin bases
EXCLUDE_TOKENS = {"WBTC"}  # optional

# =========================
# Helpers
# =========================

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "application/json"})


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(path: str, params: Optional[dict] = None, timeout: int = 15):
    url = f"{COINBASE_BASE}{path}"
    r = session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def ema(arr: np.ndarray, period: int) -> np.ndarray:
    if len(arr) < period:
        return np.array([])
    s = pd.Series(arr)
    return s.ewm(span=period, adjust=False).mean().to_numpy()


def rsi(close: np.ndarray, period: int = 14) -> float:
    if len(close) < period + 1:
        return float("nan")
    diff = np.diff(close)
    gain = np.where(diff > 0, diff, 0.0)
    loss = np.where(diff < 0, -diff, 0.0)
    avg_gain = pd.Series(gain).rolling(period).mean().iloc[-1]
    avg_loss = pd.Series(loss).rolling(period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    if len(close) < period + 1:
        return float("nan")
    prev_close = close[:-1]
    tr = np.maximum(high[1:] - low[1:], np.maximum(np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)))
    return pd.Series(tr).rolling(period).mean().iloc[-1]


def pct(a: float, b: float) -> float:
    # percent change a -> b
    if a == 0:
        return 0.0
    return (b - a) / a * 100.0


def now_ts() -> int:
    return int(time.time())


# =========================
# Telegram
# =========================

def telegram_send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("❌ Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID — not sending.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
    except Exception as e:
        log(f"❌ Telegram send failed: {e}")


# =========================
# Coinbase data
# =========================

@dataclass
class Product:
    product_id: str  # e.g. "ALEO-USD"
    base: str        # e.g. "ALEO"
    quote: str       # e.g. "USD"


def get_products_usd() -> List[Product]:
    products = http_get("/products")  # public
    out: List[Product] = []
    for p in products:
        pid = p.get("id") or ""
        if not pid or "-" not in pid:
            continue
        base, quote = pid.split("-", 1)
        if quote != "USD":
            continue
        if base in STABLE_BASES or base in EXCLUDE_TOKENS:
            continue
        # some products can be disabled; if present and false -> skip
        if p.get("trading_disabled") is True:
            continue
        out.append(Product(product_id=pid, base=base, quote=quote))
    return out


def get_stats(product_id: str) -> Optional[dict]:
    # 24h stats endpoint exists on Exchange API
    try:
        return http_get(f"/products/{product_id}/stats")
    except Exception:
        return None


def get_candles(product_id: str, granularity: int, limit: int) -> Optional[pd.DataFrame]:
    """
    Exchange candles: returns list of [time, low, high, open, close, volume]
    granularity in seconds: 60, 300, 900, 3600, 21600, 86400
    """
    try:
        data = http_get(f"/products/{product_id}/candles", params={"granularity": granularity})
        if not isinstance(data, list) or len(data) == 0:
            return None
        # Coinbase returns newest-first; sort ascending by time
        data = sorted(data, key=lambda x: x[0])
        df = pd.DataFrame(data, columns=["time", "low", "high", "open", "close", "volume"])
        df = df.tail(limit).reset_index(drop=True)
        return df
    except Exception:
        return None


# =========================
# De-dup / cooldown cache
# =========================

class Cooldown:
    def __init__(self):
        self.last_sent: Dict[str, int] = {}

    def key(self, product_id: str, signal: str) -> str:
        return f"{product_id}:{signal}"

    def allowed(self, product_id: str, signal: str, cooldown_min: int) -> bool:
        k = self.key(product_id, signal)
        t = now_ts()
        last = self.last_sent.get(k, 0)
        return (t - last) >= cooldown_min * 60

    def mark(self, product_id: str, signal: str) -> None:
        self.last_sent[self.key(product_id, signal)] = now_ts()


cooldown = Cooldown()


# =========================
# Signal Logic (STRICT)
# =========================

def liquidity_ok(product_id: str) -> Tuple[bool, float]:
    st = get_stats(product_id)
    if not st:
        return False, 0.0
    # stats fields vary; try common ones
    # volume in base units; last price; we approximate USD vol = volume * last
    try:
        vol = float(st.get("volume", 0.0))
        last = float(st.get("last", st.get("last_price", 0.0)))
        usd_vol = vol * last
        return usd_vol >= MIN_24H_USD_VOL, usd_vol
    except Exception:
        return False, 0.0


def signal_atomic_breakout(product_id: str) -> Optional[dict]:
    """
    Goal: catch the BEGINNING, not after the pop.
    Very strict:
      - 1m candles (last ~120)
      - last 5m move >= +2.2%
      - last 1m volume >= 8x median(60m)
      - price NOT already up huge: last close <= +7.5% from 30m low
      - close is near 5m high (within 0.35%) => breakout is happening now
    """
    df = get_candles(product_id, granularity=60, limit=120)
    if df is None or len(df) < 80:
        return None

    close = df["close"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    vol = df["volume"].to_numpy(float)

    last = close[-1]
    low_30m = np.min(low[-30:])
    high_5m = np.max(high[-5:])

    move_5m = pct(close[-6], last)  # ~5 minutes span
    if move_5m < 2.2:
        return None

    med_vol_60 = float(np.median(vol[-60:]))
    if med_vol_60 <= 0:
        return None
    vol_spike = vol[-1] / med_vol_60
    if vol_spike < 8.0:
        return None

    already_moved = pct(low_30m, last)
    if already_moved > 7.5:
        return None

    near_break = abs(pct(high_5m, last))  # last is close to 5m high
    if near_break > 0.35:
        return None

    # micro trend filter: 9 EMA > 21 EMA (on 1m)
    e9 = ema(close, 9)
    e21 = ema(close, 21)
    if len(e9) == 0 or len(e21) == 0 or not (e9[-1] > e21[-1]):
        return None

    stop = low_30m * 0.995  # just under 30m low
    target = last * 1.05    # conservative initial target; you can scale out

    return {
        "signal": "ATOMIC BREAKOUT",
        "price": last,
        "move_5m_pct": move_5m,
        "vol_spike_x": vol_spike,
        "stop": stop,
        "target": target,
        "note": "Strict early-breakout filter (tries to avoid post-pop).",
    }


def signal_accumulation(product_id: str) -> Optional[dict]:
    """
    Accumulation Underway (tight range + subtle demand)
    Uses 15m candles ~ last 7 days (limit 450).
    Strict:
      - Range compression: ATR% < 1.25%
      - Price within top 40% of 7d range (not dead bottom)
      - Volume trend up: last 2 days avg vol > prior 2 days avg vol by 20%
      - RSI rising and between 45-65 (not already overbought)
      - 20EMA slope positive (recent)
    """
    df = get_candles(product_id, granularity=900, limit=450)
    if df is None or len(df) < 200:
        return None

    close = df["close"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    vol = df["volume"].to_numpy(float)

    last = close[-1]
    lo = float(np.min(low))
    hi = float(np.max(high))
    if hi <= lo:
        return None

    # where in range (0 bottom -> 1 top)
    pos = (last - lo) / (hi - lo)
    if pos < 0.60:  # needs to be in upper 40% of range
        return None

    a = atr(high, low, close, 14)
    if math.isnan(a) or a <= 0:
        return None
    atr_pct = a / last * 100.0
    if atr_pct > 1.25:
        return None

    # volume trend: compare last 192 candles (~2 days) vs prior 192 (~2 days)
    if len(vol) < 400:
        return None
    v_recent = float(np.mean(vol[-192:]))
    v_prior = float(np.mean(vol[-384:-192]))
    if v_prior <= 0:
        return None
    if (v_recent / v_prior) < 1.20:
        return None

    r_now = rsi(close, 14)
    r_prev = rsi(close[:-20], 14) if len(close) > 60 else float("nan")
    if math.isnan(r_now) or math.isnan(r_prev):
        return None
    if not (45.0 <= r_now <= 65.0):
        return None
    if r_now <= r_prev:
        return None

    e20 = ema(close, 20)
    if len(e20) == 0:
        return None
    # slope: last 10 vs prior 10
    slope = float(np.mean(e20[-10:]) - np.mean(e20[-20:-10]))
    if slope <= 0:
        return None

    # “trigger” level: near range high
    trigger = hi * 1.002
    stop = lo * 0.995

    return {
        "signal": "ACCUMULATION UNDERWAY",
        "price": last,
        "atr_pct": atr_pct,
        "range_pos": pos,
        "vol_trend_x": (v_recent / v_prior),
        "trigger": trigger,
        "stop": stop,
        "note": "Tight range + improving demand; watch for breakout above trigger.",
    }


def signal_swing(product_id: str) -> Optional[dict]:
    """
    Swing Setup (few-day hold)
    Uses 1h candles (last ~30 days, limit 720).
    Strict:
      - Price above 200EMA (trend filter)
      - 20EMA > 50EMA (trend confirmation)
      - Breakout: last close > highest high of prior 48h (excluding last candle)
      - Volume confirmation: last 6h avg vol > prior 24h avg vol by 30%
      - RSI 50-70 (not stretched)
    """
    df = get_candles(product_id, granularity=3600, limit=720)
    if df is None or len(df) < 300:
        return None

    close = df["close"].to_numpy(float)
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    vol = df["volume"].to_numpy(float)
    last = close[-1]

    e20 = ema(close, 20)
    e50 = ema(close, 50)
    e200 = ema(close, 200)
    if len(e20) == 0 or len(e50) == 0 or len(e200) == 0:
        return None

    if not (last > e200[-1] and e20[-1] > e50[-1]):
        return None

    if len(high) < 60:
        return None
    prior_48h_high = float(np.max(high[-49:-1]))  # exclude last candle
    if last <= prior_48h_high:
        return None

    v6 = float(np.mean(vol[-6:]))
    v24 = float(np.mean(vol[-24:]))
    if v24 <= 0 or (v6 / v24) < 1.30:
        return None

    r = rsi(close, 14)
    if math.isnan(r) or not (50.0 <= r <= 70.0):
        return None

    # risk model: stop under 24h low; target 1.8R
    low_24h = float(np.min(low[-24:]))
    stop = low_24h * 0.995
    risk = max(0.00000001, last - stop)
    target = last + 1.8 * risk

    return {
        "signal": "SWING SETUP",
        "price": last,
        "rsi": r,
        "vol_boost_x": (v6 / v24),
        "stop": stop,
        "target": target,
        "note": "Trend + breakout + volume confirmation (few-day hold style).",
    }


# =========================
# Alert formatting
# =========================

def fmt_money(x: float) -> str:
    if x >= 1:
        return f"${x:,.4f}"
    return f"${x:.6f}"


def build_message(product_id: str, usd_vol: float, payload: dict) -> str:
    sig = payload["signal"]
    price = payload.get("price", 0.0)

    lines = []
    lines.append(f"💸 MONEY PRINTER — {sig}")
    lines.append(f"Coin: {product_id}")
    lines.append(f"Price: {fmt_money(price)}")
    lines.append(f"24h Liquidity (approx): ${usd_vol:,.0f}")

    # show key fields
    for k in ["move_5m_pct", "vol_spike_x", "atr_pct", "range_pos", "vol_trend_x", "vol_boost_x"]:
        if k in payload:
            val = payload[k]
            if "pct" in k:
                lines.append(f"{k}: {val:.2f}%")
            else:
                lines.append(f"{k}: {val:.2f}")

    # levels
    if "trigger" in payload:
        lines.append(f"Trigger: {fmt_money(payload['trigger'])}")
    if "target" in payload:
        lines.append(f"Target: {fmt_money(payload['target'])}")
    if "stop" in payload:
        lines.append(f"Stop: {fmt_money(payload['stop'])}")

    note = payload.get("note")
    if note:
        lines.append(f"Note: {note}")

    return "\n".join(lines)


# =========================
# Main loops
# =========================

def pick_universe() -> List[Product]:
    all_usd = get_products_usd()

    # Filter by liquidity (strict) and then take top by volume estimate
    scored = []
    for p in all_usd:
        ok, usd_vol = liquidity_ok(p.product_id)
        if not ok:
            continue
        scored.append((usd_vol, p))
        # small jitter to avoid hammering
        time.sleep(0.08)

    scored.sort(key=lambda x: x[0], reverse=True)
    universe = [p for _, p in scored[:MAX_COINS]]
    log(f"Universe size: {len(universe)} (MAX_COINS={MAX_COINS})")
    return universe


def scan_atomic(universe: List[Product]):
    for p in universe:
        if not cooldown.allowed(p.product_id, "ATOMIC", ATOMIC_COOLDOWN_MIN):
            continue
        ok, usd_vol = liquidity_ok(p.product_id)
        if not ok:
            continue

        payload = signal_atomic_breakout(p.product_id)
        if payload:
            msg = build_message(p.product_id, usd_vol, payload)
            telegram_send(msg)
            cooldown.mark(p.product_id, "ATOMIC")

        time.sleep(0.15)


def scan_accumulation(universe: List[Product]):
    for p in universe:
        if not cooldown.allowed(p.product_id, "ACC", ACC_COOLDOWN_MIN):
            continue
        ok, usd_vol = liquidity_ok(p.product_id)
        if not ok:
            continue

        payload = signal_accumulation(p.product_id)
        if payload:
            msg = build_message(p.product_id, usd_vol, payload)
            telegram_send(msg)
            cooldown.mark(p.product_id, "ACC")

        time.sleep(0.20)


def scan_swing(universe: List[Product]):
    for p in universe:
        if not cooldown.allowed(p.product_id, "SWING", SWING_COOLDOWN_MIN):
            continue
        ok, usd_vol = liquidity_ok(p.product_id)
        if not ok:
            continue

        payload = signal_swing(p.product_id)
        if payload:
            msg = build_message(p.product_id, usd_vol, payload)
            telegram_send(msg)
            cooldown.mark(p.product_id, "SWING")

        time.sleep(0.25)


def main():
    telegram_send("✅ MONEY PRINTER scanner booted. (Atomic/Accumulation/Swing)")
    universe = pick_universe()

    t_atomic = 0
    t_acc = 0
    t_swing = 0
    t_refresh_universe = 0

    while True:
        t = now_ts()

        # refresh universe every 6 hours
        if (t - t_refresh_universe) > 6 * 3600:
            try:
                universe = pick_universe()
            except Exception as e:
                log(f"Universe refresh failed: {e}")
            t_refresh_universe = t

        # ATOMIC every 60s
        if (t - t_atomic) >= ATOMIC_EVERY_SEC:
            log("Running ATOMIC scan...")
            try:
                scan_atomic(universe)
            except Exception as e:
                log(f"ATOMIC scan error: {e}")
            t_atomic = t

        # ACC every 10m
        if (t - t_acc) >= ACC_EVERY_SEC:
            log("Running ACCUMULATION scan...")
            try:
                scan_accumulation(universe)
            except Exception as e:
                log(f"ACC scan error: {e}")
            t_acc = t

        # SWING every 60m
        if (t - t_swing) >= SWING_EVERY_SEC:
            log("Running SWING scan...")
            try:
                scan_swing(universe)
            except Exception as e:
                log(f"SWING scan error: {e}")
            t_swing = t

        time.sleep(2)


if __name__ == "__main__":
    main()
