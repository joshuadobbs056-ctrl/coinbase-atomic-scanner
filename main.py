import os
import time
import json
import math
import statistics
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import requests

# =========================
# CONFIG (ACCUMULATION ONLY)
# =========================

# Core scan timing
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))  # 5 min default
MAX_PRODUCTS = int(os.getenv("MAX_PRODUCTS", "250"))  # cap to avoid rate limits
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Coinbase Advanced Trade public market endpoints
# Docs mention these are public endpoints; we use them without auth. (If Coinbase changes this, add auth later.)
COINBASE_BASE = "https://api.coinbase.com"
CB_PRODUCTS_URL = COINBASE_BASE + "/api/v3/brokerage/market/products"
CB_CANDLES_URL = COINBASE_BASE + "/api/v3/brokerage/market/products/{product_id}/candles"
CB_BOOK_URL = COINBASE_BASE + "/api/v3/brokerage/market/product_book"

# ----- ACCUMULATION HARD GATES -----
# 72h sideways range compression
MAX_RANGE_PCT_72H = 8.0

# Rising volume while sideways
MIN_VOLUME_RATIO_6H_OVER_PRIOR24H = 1.3

# Low volatility
MAX_VOLATILITY_PCT_24H = 3.0

# No pump
MAX_CHANGE_PCT_24H = 5.0

# Liquidity (degenerate mode)
MIN_QUOTE_VOL_24H_USD = 250_000.0  # $250k min (degen)
MIN_MARKET_CAP_USD = 10_000_000.0  # $10M min (degen) if available from Coinbase products feed

# ----- OPTIONAL HUGE FILTER (ORDER BOOK) -----
USE_SLIPPAGE_FILTER = True
SIMULATED_ORDER_SIZE_USD = 200.0
MAX_SPREAD_PCT = 0.6
MAX_SLIPPAGE_PCT = 0.8
BOOK_LIMIT_LEVELS = 50  # depth to request

# ----- ALERT DEDUPE / RE-ALERT LOGIC -----
ALERT_COOLDOWN_HOURS = 8
SCORE_IMPROVEMENT_TO_RE_ALERT = 2  # only re-alert if score improves meaningfully
DURATION_THRESHOLDS_HOURS = [12, 24, 48]  # re-alert when crossing these

# Candles granularity
GRANULARITY = "ONE_HOUR"  # 1h candles give clean 72h/24h windows and low API pressure

# Requests settings
HTTP_TIMEOUT = 20
USER_AGENT = "accumulation-only-scanner/1.0"

# =========================
# DATA MODELS
# =========================

@dataclass
class Candle:
    start: int
    low: float
    high: float
    open: float
    close: float
    volume_base: float  # base units

@dataclass
class AccumulationMetrics:
    product_id: str
    price: float
    range_pct_72h: float
    volatility_pct_24h: float
    change_pct_24h: float
    quote_vol_24h_usd: float
    volume_ratio: float
    spread_pct: Optional[float] = None
    slippage_pct: Optional[float] = None
    accumulation_hours: float = 0.0
    score: int = 0
    market_cap_usd: Optional[float] = None

# =========================
# UTILITIES
# =========================

def now_ts() -> int:
    return int(time.time())

def safe_float(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, str):
            x = x.replace("%", "").strip()
        return float(x)
    except Exception:
        return default

def clamp(n: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, n))

def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state: Dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)

def http_get_json(url: str, params: Optional[Dict] = None) -> Dict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        # If Coinbase caching becomes an issue, you can uncomment the next line:
        # "Cache-Control": "no-cache",
    }
    r = requests.get(url, params=params or {}, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()

# =========================
# COINBASE FETCHERS
# =========================

def list_coinbase_products_usd(max_products: int) -> List[Dict]:
    """
    Pull Coinbase 'market/products' and keep only USD quote pairs that are tradable.
    Endpoint supports pagination via cursor in response.pagination.next_cursor.
    """
    products: List[Dict] = []
    cursor = None

    while len(products) < max_products:
        params = {}
        if cursor:
            params["cursor"] = cursor

        data = http_get_json(CB_PRODUCTS_URL, params=params)
        chunk = data.get("products", []) or []

        for p in chunk:
            # Prefer spot products (avoid perps, etc.). If fields are missing, we keep only simple USD pairs.
            product_id = p.get("product_id") or ""
            quote = (p.get("quote_display_symbol") or "").upper()
            is_disabled = bool(p.get("is_disabled", False))
            trading_disabled = bool(p.get("trading_disabled", False))

            if not product_id:
                continue
            if quote != "USD":
                continue
            if is_disabled or trading_disabled:
                continue

            # Keep mostly spot-ish pairs; product_type varies. We'll accept unknown but reject obvious perps.
            display_name = (p.get("display_name") or "").upper()
            if "PERP" in display_name:
                continue

            products.append(p)
            if len(products) >= max_products:
                break

        pagination = data.get("pagination") or {}
        cursor = pagination.get("next_cursor")
        has_next = bool(pagination.get("has_next", False))
        if not cursor or not has_next:
            break

    return products

def get_candles(product_id: str, start_ts: int, end_ts: int, granularity: str) -> List[Candle]:
    url = CB_CANDLES_URL.format(product_id=product_id)
    params = {
        "start": str(start_ts),
        "end": str(end_ts),
        "granularity": granularity,
    }
    data = http_get_json(url, params=params)
    raw = data.get("candles", []) or []

    candles: List[Candle] = []
    for c in raw:
        candles.append(
            Candle(
                start=int(c["start"]),
                low=safe_float(c["low"]),
                high=safe_float(c["high"]),
                open=safe_float(c["open"]),
                close=safe_float(c["close"]),
                volume_base=safe_float(c["volume"]),
            )
        )

    # Coinbase often returns newest-first; sort ascending by time for easier windowing.
    candles.sort(key=lambda x: x.start)
    return candles

def get_product_book(product_id: str, limit_levels: int = 50) -> Dict:
    params = {
        "product_id": product_id,
        "limit": str(limit_levels),
    }
    return http_get_json(CB_BOOK_URL, params=params)

# =========================
# METRIC CALCS (ACCUMULATION)
# =========================

def compute_quote_volume_usd(candles: List[Candle]) -> List[float]:
    # Approx: quote volume per candle ~= base_volume * close_price
    return [max(0.0, c.volume_base) * max(0.0, c.close) for c in candles]

def pct_change(a: float, b: float) -> float:
    # change from a -> b, in %
    if a <= 0:
        return 0.0
    return (b - a) / a * 100.0

def calc_accumulation_metrics(
    product_id: str,
    candles_72h: List[Candle],
    market_cap_usd: Optional[float],
) -> Optional[AccumulationMetrics]:
    """
    Requires:
      - 72h of 1h candles (or near) so we can slice last 24h, last 6h, previous 24h.
    """
    if len(candles_72h) < 60:  # allow some missing candles
        return None

    # Use most recent close as "price"
    price = candles_72h[-1].close
    if price <= 0:
        return None

    # 72h range compression
    hi_72 = max(c.high for c in candles_72h)
    lo_72 = min(c.low for c in candles_72h)
    range_pct_72h = ((hi_72 - lo_72) / price) * 100.0 if price > 0 else 999.0

    # last 24h candles: last 24 elements (since 1h)
    candles_24h = candles_72h[-24:]
    closes_24h = [c.close for c in candles_24h if c.close > 0]
    if len(closes_24h) < 18:
        return None

    # volatility compression: stddev(close)/price
    vol = statistics.pstdev(closes_24h) if len(closes_24h) > 1 else 0.0
    volatility_pct_24h = (vol / price) * 100.0 if price > 0 else 999.0

    # 24h change: close now vs close 24h ago (first candle in 24h window)
    change_pct_24h = pct_change(candles_24h[0].close, candles_24h[-1].close)

    # Volume ratio: avg quote-volume last 6h vs avg quote-volume previous 24h (excluding last 6h)
    quote_vols_72 = compute_quote_volume_usd(candles_72h)
    quote_vols_24 = quote_vols_72[-24:]
    quote_vol_24h_usd = sum(quote_vols_24)

    recent_6 = quote_vols_72[-6:]
    prev_24 = quote_vols_72[-30:-6]  # 24 hours prior to last 6h

    if len(recent_6) < 6 or len(prev_24) < 18:
        return None

    avg_recent_6 = sum(recent_6) / len(recent_6)
    avg_prev_24 = sum(prev_24) / len(prev_24)
    volume_ratio = (avg_recent_6 / avg_prev_24) if avg_prev_24 > 0 else 0.0

    return AccumulationMetrics(
        product_id=product_id,
        price=price,
        range_pct_72h=range_pct_72h,
        volatility_pct_24h=volatility_pct_24h,
        change_pct_24h=change_pct_24h,
        quote_vol_24h_usd=quote_vol_24h_usd,
        volume_ratio=volume_ratio,
        market_cap_usd=market_cap_usd,
    )

# =========================
# OPTIONAL HUGE FILTER: SPREAD + SLIPPAGE
# =========================

def calc_spread_and_slippage(product_id: str, order_size_usd: float) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """
    Uses order book asks/bids to compute:
      - spread% based on best bid/ask
      - slippage% for a simulated market buy of `order_size_usd` walking the ask book
    Returns (spread_pct, slippage_pct, error_reason)
    """
    try:
        book = get_product_book(product_id, limit_levels=BOOK_LIMIT_LEVELS)
        pb = book.get("pricebook") or {}

        bids = pb.get("bids") or []
        asks = pb.get("asks") or []
        if not bids or not asks:
            return None, None, "empty_orderbook"

        best_bid = safe_float(bids[0].get("price"))
        best_ask = safe_float(asks[0].get("price"))
        if best_bid <= 0 or best_ask <= 0:
            return None, None, "bad_best_prices"

        spread_pct = ((best_ask - best_bid) / best_ask) * 100.0

        # Walk asks to fill order_size_usd
        remaining = float(order_size_usd)
        total_cost = 0.0
        total_base = 0.0

        for lvl in asks:
            price = safe_float(lvl.get("price"))
            size_base = safe_float(lvl.get("size"))
            if price <= 0 or size_base <= 0:
                continue

            lvl_cost = price * size_base
            take_cost = min(remaining, lvl_cost)
            take_base = take_cost / price

            total_cost += take_cost
            total_base += take_base
            remaining -= take_cost

            if remaining <= 1e-9:
                break

        if total_base <= 0 or total_cost <= 0:
            return spread_pct, None, "insufficient_liquidity"
        if remaining > 1e-6:
            # Could not fully fill within depth
            return spread_pct, None, "insufficient_depth"

        avg_fill = total_cost / total_base
        slippage_pct = ((avg_fill - best_ask) / best_ask) * 100.0

        return spread_pct, slippage_pct, None

    except Exception as e:
        return None, None, f"book_error:{type(e).__name__}"

# =========================
# SCORING (1–10)
# =========================

def score_metrics(m: AccumulationMetrics) -> int:
    score = 0

    # Range
    if m.range_pct_72h <= 5.0:
        score += 2
    elif m.range_pct_72h <= 8.0:
        score += 1

    # Volume ratio
    if m.volume_ratio >= 1.5:
        score += 2
    elif m.volume_ratio >= 1.3:
        score += 1

    # Volatility
    if m.volatility_pct_24h <= 2.0:
        score += 2
    elif m.volatility_pct_24h <= 3.0:
        score += 1

    # Liquidity (quote vol 24h)
    if m.quote_vol_24h_usd >= 5_000_000:
        score += 2
    elif m.quote_vol_24h_usd >= 1_000_000:
        score += 1

    # Market cap (if available)
    if m.market_cap_usd is not None:
        if m.market_cap_usd >= 100_000_000:
            score += 2
        elif m.market_cap_usd >= 25_000_000:
            score += 1

    # Optional: tight spread/slippage bonuses
    if m.spread_pct is not None and m.spread_pct <= 0.3:
        score += 1
    if m.slippage_pct is not None and m.slippage_pct <= 0.4:
        score += 1

    return int(clamp(score, 1, 10)) if score > 0 else 1

# =========================
# HARD GATES (ACCUMULATION ONLY)
# =========================

def passes_hard_gates(m: AccumulationMetrics) -> Tuple[bool, List[str]]:
    reasons = []

    if m.range_pct_72h > MAX_RANGE_PCT_72H:
        reasons.append(f"range>{MAX_RANGE_PCT_72H:.1f}%")

    if m.volume_ratio < MIN_VOLUME_RATIO_6H_OVER_PRIOR24H:
        reasons.append(f"vol_ratio<{MIN_VOLUME_RATIO_6H_OVER_PRIOR24H:.2f}")

    if m.volatility_pct_24h > MAX_VOLATILITY_PCT_24H:
        reasons.append(f"volatility>{MAX_VOLATILITY_PCT_24H:.1f}%")

    if m.change_pct_24h > MAX_CHANGE_PCT_24H:
        reasons.append(f"chg24h>{MAX_CHANGE_PCT_24H:.1f}%")

    if m.quote_vol_24h_usd < MIN_QUOTE_VOL_24H_USD:
        reasons.append(f"liq24h<${MIN_QUOTE_VOL_24H_USD:,.0f}")

    # Degenerate market cap floor (only if the feed gives it)
    if m.market_cap_usd is not None and m.market_cap_usd < MIN_MARKET_CAP_USD:
        reasons.append(f"mcap<${MIN_MARKET_CAP_USD:,.0f}")

    # Optional huge filter: spread + slippage
    if USE_SLIPPAGE_FILTER:
        if m.spread_pct is None:
            reasons.append("no_spread")
        elif m.spread_pct > MAX_SPREAD_PCT:
            reasons.append(f"spread>{MAX_SPREAD_PCT:.2f}%")

        if m.slippage_pct is None:
            reasons.append("no_slippage")
        elif m.slippage_pct > MAX_SLIPPAGE_PCT:
            reasons.append(f"slip>{MAX_SLIPPAGE_PCT:.2f}%")

    return (len(reasons) == 0, reasons)

# =========================
# TELEGRAM
# =========================

def telegram_send(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM NOT CONFIGURED: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        print(message)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
    r.raise_for_status()

def fmt_money(x: float) -> str:
    if x >= 1_000_000_000:
        return f"${x/1_000_000_000:.2f}B"
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x/1_000:.2f}K"
    return f"${x:.0f}"

def build_alert_message(m: AccumulationMetrics) -> str:
    # Big bold score
    score_line = f"<b>ACCUMULATION {m.score}/10</b>  •  <b>{m.product_id}</b>"
    price_line = f"Price: <b>${m.price:.6g}</b>"

    core = (
        f"Range(72h): <b>{m.range_pct_72h:.2f}%</b>\n"
        f"Volatility(24h): <b>{m.volatility_pct_24h:.2f}%</b>\n"
        f"24h Change: <b>{m.change_pct_24h:.2f}%</b>\n"
        f"Vol Ratio (6h/prev24h): <b>{m.volume_ratio:.2f}x</b>\n"
        f"24h Liquidity (approx): <b>{fmt_money(m.quote_vol_24h_usd)}</b>\n"
        f"Accumulation duration: <b>{m.accumulation_hours:.1f}h</b>"
    )

    extras = []
    if m.market_cap_usd is not None:
        extras.append(f"Market Cap (feed): <b>{fmt_money(m.market_cap_usd)}</b>")
    if m.spread_pct is not None:
        extras.append(f"Spread: <b>{m.spread_pct:.2f}%</b>")
    if m.slippage_pct is not None:
        extras.append(f"Slippage @ ${SIMULATED_ORDER_SIZE_USD:.0f}: <b>{m.slippage_pct:.2f}%</b>")

    extra_line = ("\n" + "\n".join(extras)) if extras else ""
    return f"{score_line}\n{price_line}\n\n{core}{extra_line}"

# =========================
# STATE / DEDUPE
# =========================

def update_accumulation_duration(state: Dict, product_id: str, is_accumulating: bool, ts: int) -> float:
    """
    Track consecutive time in accumulation state.
    Returns accumulation_hours.
    """
    s = state.setdefault(product_id, {})
    if is_accumulating:
        if not s.get("accum_start_ts"):
            s["accum_start_ts"] = ts
        s["last_accum_ts"] = ts
    else:
        s.pop("accum_start_ts", None)
        s.pop("last_accum_ts", None)

    start = s.get("accum_start_ts")
    if not start:
        return 0.0
    return max(0.0, (ts - int(start)) / 3600.0)

def should_alert(state: Dict, m: AccumulationMetrics, ts: int) -> bool:
    s = state.setdefault(m.product_id, {})
    last_alert_ts = int(s.get("last_alert_ts", 0) or 0)
    last_score = int(s.get("last_alert_score", 0) or 0)
    last_duration_bucket = int(s.get("last_duration_bucket", 0) or 0)

    cooldown_ok = (ts - last_alert_ts) >= int(ALERT_COOLDOWN_HOURS * 3600)

    # Duration bucket crossing
    bucket = 0
    for th in DURATION_THRESHOLDS_HOURS:
        if m.accumulation_hours >= th:
            bucket = th
    crossed_bucket = bucket > last_duration_bucket

    improved_score = (m.score - last_score) >= SCORE_IMPROVEMENT_TO_RE_ALERT

    if last_alert_ts == 0:
        return True  # first ever alert
    if cooldown_ok and (improved_score or crossed_bucket):
        return True

    return False

def record_alert(state: Dict, m: AccumulationMetrics, ts: int) -> None:
    s = state.setdefault(m.product_id, {})
    s["last_alert_ts"] = ts
    s["last_alert_score"] = m.score

    bucket = 0
    for th in DURATION_THRESHOLDS_HOURS:
        if m.accumulation_hours >= th:
            bucket = th
    s["last_duration_bucket"] = bucket

# =========================
# MAIN SCAN LOOP
# =========================

def scan_once(state: Dict) -> None:
    ts = now_ts()

    products = list_coinbase_products_usd(MAX_PRODUCTS)
    print(f"[scan] products={len(products)}")

    # Use 72h window for candles
    end_ts = ts
    start_ts_72h = ts - 72 * 3600

    candidates: List[AccumulationMetrics] = []

    for p in products:
        product_id = p.get("product_id")
        if not product_id:
            continue

        # Market cap from feed (may be missing)
        mcap = p.get("market_cap")
        market_cap_usd = safe_float(mcap, default=0.0) if mcap is not None else None
        if market_cap_usd is not None and market_cap_usd <= 0:
            # treat "0" as unknown; some products won't have it
            market_cap_usd = None

        try:
            candles = get_candles(product_id, start_ts_72h, end_ts, GRANULARITY)
            metrics = calc_accumulation_metrics(product_id, candles, market_cap_usd)
            if not metrics:
                continue

            # Optional huge filter calc (spread + slippage)
            if USE_SLIPPAGE_FILTER:
                spread, slip, err = calc_spread_and_slippage(product_id, SIMULATED_ORDER_SIZE_USD)
                metrics.spread_pct = spread
                metrics.slippage_pct = slip
                # If order book is unavailable, it will likely fail gates anyway.

            # Compute score now (score uses spread/slip bonuses too)
            metrics.score = score_metrics(metrics)

            # Hard gates decide "accumulating"
            ok, _reasons = passes_hard_gates(metrics)

            # Update duration state based on whether it passes gates
            metrics.accumulation_hours = update_accumulation_duration(state, product_id, ok, ts)

            if not ok:
                continue

            candidates.append(metrics)

        except Exception as e:
            # Skip noisy pairs / occasional failures
            print(f"[warn] {product_id} failed: {type(e).__name__}")
            continue

    # Sort best first
    candidates.sort(key=lambda x: (x.score, x.accumulation_hours, x.quote_vol_24h_usd), reverse=True)

    # Alert only on top candidates (avoid spam)
    top = candidates[:10]

    for m in top:
        if should_alert(state, m, ts):
            msg = build_alert_message(m)
            telegram_send(msg)
            record_alert(state, m, ts)
            print(f"[alert] {m.product_id} score={m.score} dur={m.accumulation_hours:.1f}h")

    save_state(state)
    print(f"[scan] candidates={len(candidates)} top={len(top)}")

def main():
    state = load_state()
    print("[start] accumulation-only scanner running")
    while True:
        try:
            scan_once(state)
        except Exception as e:
            print(f"[fatal] scan failed: {type(e).__name__}: {e}")
        time.sleep(SCAN_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
