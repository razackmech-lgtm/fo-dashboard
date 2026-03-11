#!/usr/bin/env python3
"""
OptionsPulse Pro - Advanced F&O Trading Dashboard
Real-time analysis with GrowAPI / NSE fallback
Run: pip install requests flask flask-cors pandas numpy scipy
Then: python fo_dashboard.py
Open: http://localhost:5050
"""

import json
import math
import time
import threading
import datetime
import random
from collections import defaultdict

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from flask import Flask, jsonify, request, render_template_string
    from flask_cors import CORS
    HAS_FLASK = True
except ImportError:
    HAS_FLASK = False

# ============================================================
#  CONFIGURATION
# ============================================================
DEFAULT_API_KEY  = ""          # Set your GrowAPI key here or pass via UI
GROWAPI_BASE     = "https://growapi.groww.in/v1"
NSE_BASE         = "https://www.nseindia.com/api"
REFRESH_INTERVAL = 30          # seconds between auto-refresh

SYMBOLS = {
    "NIFTY":      {"step": 50,  "lot": 25,   "type": "INDEX"},
    "BANKNIFTY":  {"step": 100, "lot": 15,   "type": "INDEX"},
    "FINNIFTY":   {"step": 50,  "lot": 40,   "type": "INDEX"},
    "MIDCPNIFTY": {"step": 25,  "lot": 75,   "type": "INDEX"},
    "SENSEX":     {"step": 100, "lot": 10,   "type": "INDEX"},
    "RELIANCE":   {"step": 20,  "lot": 250,  "type": "STOCK"},
    "TCS":        {"step": 50,  "lot": 150,  "type": "STOCK"},
    "INFY":       {"step": 20,  "lot": 300,  "type": "STOCK"},
    "HDFCBANK":   {"step": 20,  "lot": 550,  "type": "STOCK"},
}

SPOT_PRICES = {
    "NIFTY":      24350, "BANKNIFTY": 52100, "FINNIFTY": 23800,
    "MIDCPNIFTY": 13400, "SENSEX":    80200, "RELIANCE": 2980,
    "TCS":         3850, "INFY":       1820, "HDFCBANK": 1620,
}

# ============================================================
#  DATA LAYER  (GrowAPI / NSE / Mock)
# ============================================================

class DataFetcher:
    def __init__(self):
        self.session = requests.Session() if HAS_REQUESTS else None
        self.nse_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "application/json",
            "Referer": "https://www.nseindia.com",
        }
        self.cache = {}
        self.source = "Mock"

    # ---- GrowAPI -------------------------------------------------------
    def fetch_growapi(self, symbol, expiry, api_key):
        if not self.session or not api_key:
            return None
        try:
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            url = f"{GROWAPI_BASE}/option-chain?symbol={symbol}&expiry={expiry}"
            r = self.session.get(url, headers=headers, timeout=8)
            if r.status_code == 200:
                self.source = "GrowAPI"
                return r.json()
        except Exception:
            pass
        return None

    # ---- NSE Public API ------------------------------------------------
    def fetch_nse(self, symbol):
        if not self.session:
            return None
        try:
            # Prime cookie
            self.session.get("https://www.nseindia.com", headers=self.nse_headers, timeout=6)
            ep = "option-chain-indices" if SYMBOLS.get(symbol, {}).get("type") == "INDEX" else "option-chain-equities"
            url = f"{NSE_BASE}/{ep}?symbol={symbol}"
            r = self.session.get(url, headers=self.nse_headers, timeout=8)
            if r.status_code == 200:
                self.source = "NSE India"
                return r.json()
        except Exception:
            pass
        return None

    # ---- Realistic mock ------------------------------------------------
    def generate_mock(self, symbol, expiry):
        self.source = "Simulated"
        cfg   = SYMBOLS.get(symbol, {"step": 50, "lot": 25})
        spot  = SPOT_PRICES.get(symbol, 24000) * (1 + random.uniform(-0.003, 0.003))
        step  = cfg["step"]
        atm   = round(spot / step) * step
        rows  = []

        # Simulate market regime randomly for demo
        regime = random.choice(["BULL", "BEAR", "NEUTRAL"])
        for i in range(-7, 8):
            strike = atm + i * step
            mn = (strike - spot) / spot
            base = 4e6 * math.exp(-abs(mn) * 18)

            # OI skew based on regime
            call_mult = 0.65 if regime == "BULL" else (1.4 if regime == "BEAR" else 1.0)
            put_mult  = 1.4  if regime == "BULL" else (0.65 if regime == "BEAR" else 1.0)

            call_oi = int(base * call_mult * (0.85 + random.random() * 0.3) * (0.5 if i < 0 else 1.2))
            put_oi  = int(base * put_mult  * (0.85 + random.random() * 0.3) * (1.2 if i < 0 else 0.5))

            # Change in OI (simulate buildup/unwind)
            call_chg_oi = int(call_oi * random.uniform(-0.12, 0.18))
            put_chg_oi  = int(put_oi  * random.uniform(-0.12, 0.18))

            # IV surface - smile
            iv_base = 16 + abs(mn) * 120 + random.uniform(-1, 1)
            call_iv = iv_base + (2 if i > 0 else -1)
            put_iv  = iv_base + (2 if i < 0 else -1)

            # LTP
            T = 7 / 365
            call_ltp = max(0.3, bs_call(spot, strike, T, 0.065, call_iv / 100))
            put_ltp  = max(0.3, bs_put(spot,  strike, T, 0.065, put_iv  / 100))

            # Greeks
            call_delta = bs_delta(spot, strike, T, 0.065, call_iv / 100, "call")
            put_delta  = bs_delta(spot, strike, T, 0.065, put_iv  / 100, "put")
            gamma      = bs_gamma(spot, strike, T, 0.065, call_iv / 100)
            theta_c    = bs_theta(spot, strike, T, 0.065, call_iv / 100, "call")
            theta_p    = bs_theta(spot, strike, T, 0.065, put_iv  / 100, "put")
            vega_val   = bs_vega(spot, strike, T, 0.065, call_iv / 100)

            rows.append({
                "strikePrice": strike,
                "CE": {
                    "openInterest": call_oi, "changeinOpenInterest": call_chg_oi,
                    "lastPrice": round(call_ltp, 2), "impliedVolatility": round(call_iv, 1),
                    "delta": round(call_delta, 3), "gamma": round(gamma, 4),
                    "theta": round(theta_c, 2), "vega": round(vega_val, 2),
                    "bidprice": round(call_ltp - 0.5, 2), "askPrice": round(call_ltp + 0.5, 2),
                    "totalTradedVolume": int(call_oi * random.uniform(0.05, 0.25)),
                },
                "PE": {
                    "openInterest": put_oi, "changeinOpenInterest": put_chg_oi,
                    "lastPrice": round(put_ltp, 2), "impliedVolatility": round(put_iv, 1),
                    "delta": round(put_delta, 3), "gamma": round(gamma, 4),
                    "theta": round(theta_p, 2), "vega": round(vega_val, 2),
                    "bidprice": round(put_ltp - 0.5, 2), "askPrice": round(put_ltp + 0.5, 2),
                    "totalTradedVolume": int(put_oi * random.uniform(0.05, 0.25)),
                },
            })
        return {"records": {"underlyingValue": round(spot, 2), "data": rows}, "_regime": regime}

    def get_chain(self, symbol, expiry, api_key=""):
        raw = None
        # 1. Try GrowAPI if key provided
        if api_key:
            raw = self.fetch_growapi(symbol, expiry, api_key)
        # 2. Try NSE public API
        if raw is None:
            raw = self.fetch_nse(symbol)
        # 3. Validate raw has actual data rows before accepting it
        if raw is not None:
            try:
                rows = raw.get("records", {}).get("data", [])
                if not rows or len(rows) == 0:
                    raw = None  # force mock
            except Exception:
                raw = None
        # 4. Always fall back to mock - never return empty
        if raw is None:
            raw = self.generate_mock(symbol, expiry)
        return raw

# ============================================================
#  BLACK-SCHOLES HELPERS
# ============================================================

def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def _d1d2(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return 0, 0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2

def bs_call(S, K, T, r, sigma):
    if T <= 0: return max(0, S - K)
    d1, d2 = _d1d2(S, K, T, r, sigma)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)

def bs_put(S, K, T, r, sigma):
    if T <= 0: return max(0, K - S)
    d1, d2 = _d1d2(S, K, T, r, sigma)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

def bs_delta(S, K, T, r, sigma, opt="call"):
    if T <= 0: return (1 if S > K else 0) if opt == "call" else (-1 if S < K else 0)
    d1, _ = _d1d2(S, K, T, r, sigma)
    return _norm_cdf(d1) if opt == "call" else _norm_cdf(d1) - 1

def bs_gamma(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0: return 0
    d1, _ = _d1d2(S, K, T, r, sigma)
    return _norm_pdf(d1) / (S * sigma * math.sqrt(T))

def bs_theta(S, K, T, r, sigma, opt="call"):
    if T <= 0 or sigma <= 0: return 0
    d1, d2 = _d1d2(S, K, T, r, sigma)
    t1 = -S * _norm_pdf(d1) * sigma / (2 * math.sqrt(T))
    if opt == "call":
        return (t1 - r * K * math.exp(-r * T) * _norm_cdf(d2)) / 365
    else:
        return (t1 + r * K * math.exp(-r * T) * _norm_cdf(-d2)) / 365

def bs_vega(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0: return 0
    d1, _ = _d1d2(S, K, T, r, sigma)
    return S * _norm_pdf(d1) * math.sqrt(T) / 100

# ============================================================
#  ANALYSIS ENGINE
# ============================================================

def normalise_chain(raw):
    try:
        if not raw:
            return None, []
        # Support both NSE schema and GrowAPI schema
        records = raw.get("records") or raw.get("data", {})
        if isinstance(records, dict):
            spot = (records.get("underlyingValue") or
                    records.get("spotPrice") or
                    raw.get("underlyingValue") or 24000)
            data = records.get("data") or records.get("optionChain") or []
        else:
            spot = raw.get("underlyingValue", 24000)
            data = records if isinstance(records, list) else []

        # Filter out rows with no strikePrice
        data = [d for d in data if d.get("strikePrice") or d.get("strike")]

        if not data:
            return None, []

        rows = []
        for d in data:
            strike = d.get("strikePrice") or d.get("strike", 0)
            ce = d.get("CE") or {}
            pe = d.get("PE") or {}
            rows.append({
                "strike":      float(strike),
                "call_oi":     float(ce.get("openInterest", 0) or 0),
                "call_chg_oi": float(ce.get("changeinOpenInterest", 0) or 0),
                "call_ltp":    float(ce.get("lastPrice", 0) or 0),
                "call_iv":     float(ce.get("impliedVolatility", 0) or 0),
                "call_delta":  float(ce.get("delta", 0) or 0),
                "call_gamma":  float(ce.get("gamma", 0) or 0),
                "call_theta":  float(ce.get("theta", 0) or 0),
                "call_vega":   float(ce.get("vega", 0) or 0),
                "call_volume": float(ce.get("totalTradedVolume", 0) or 0),
                "call_bid":    float(ce.get("bidprice", 0) or 0),
                "call_ask":    float(ce.get("askPrice", 0) or 0),
                "put_oi":      float(pe.get("openInterest", 0) or 0),
                "put_chg_oi":  float(pe.get("changeinOpenInterest", 0) or 0),
                "put_ltp":     float(pe.get("lastPrice", 0) or 0),
                "put_iv":      float(pe.get("impliedVolatility", 0) or 0),
                "put_delta":   float(pe.get("delta", 0) or 0),
                "put_gamma":   float(pe.get("gamma", 0) or 0),
                "put_theta":   float(pe.get("theta", 0) or 0),
                "put_vega":    float(pe.get("vega", 0) or 0),
                "put_volume":  float(pe.get("totalTradedVolume", 0) or 0),
                "put_bid":     float(pe.get("bidprice", 0) or 0),
                "put_ask":     float(pe.get("askPrice", 0) or 0),
            })
        rows.sort(key=lambda x: x["strike"])
        return float(spot), rows
    except Exception as e:
        print(f"normalise_chain error: {e}")
        return None, []


def analyse(spot, rows, symbol, expiry):
    if not rows:
        return {}

    cfg = SYMBOLS.get(symbol, {"step": 50, "lot": 25})
    step = cfg["step"]

    # ATM
    atm_row = min(rows, key=lambda r: abs(r["strike"] - spot))
    atm = atm_row["strike"]
    atm_idx = rows.index(atm_row)

    # ---- Basic OI metrics ----
    total_call_oi = sum(r["call_oi"] for r in rows)
    total_put_oi  = sum(r["put_oi"]  for r in rows)
    pcr = (total_put_oi / total_call_oi) if total_call_oi else 1.0

    # ---- OI Change (buildup vs unwind) ----
    call_oi_buildup = sum(r["call_chg_oi"] for r in rows if r["call_chg_oi"] > 0)
    put_oi_buildup  = sum(r["put_chg_oi"]  for r in rows if r["put_chg_oi"]  > 0)
    call_oi_unwind  = sum(abs(r["call_chg_oi"]) for r in rows if r["call_chg_oi"] < 0)
    put_oi_unwind   = sum(abs(r["put_chg_oi"])  for r in rows if r["put_chg_oi"]  < 0)

    # ---- Max Pain ----
    mp_losses = {}
    for cand in rows:
        loss = 0
        for r in rows:
            loss += max(0, cand["strike"] - r["strike"]) * r["call_oi"]
            loss += max(0, r["strike"] - cand["strike"]) * r["put_oi"]
        mp_losses[cand["strike"]] = loss
    max_pain = min(mp_losses, key=mp_losses.get)

    # ---- Resistance / Support (max OI strikes) ----
    max_call_strike = max(rows, key=lambda r: r["call_oi"])["strike"]  # resistance
    max_put_strike  = max(rows, key=lambda r: r["put_oi"])["strike"]   # support

    # ---- Near ATM OI (+-3 strikes) ----
    near = rows[max(0, atm_idx-3): atm_idx+4]
    near_call_oi = sum(r["call_oi"] for r in near)
    near_put_oi  = sum(r["put_oi"]  for r in near)

    # ---- IV Skew (put IV - call IV at ATM) ----
    atm_call_iv = atm_row["call_iv"]
    atm_put_iv  = atm_row["put_iv"]
    iv_skew = atm_put_iv - atm_call_iv   # +ve = fear premium, bearish

    # ---- IV percentile approximation ----
    all_ivs = [r["call_iv"] for r in rows if r["call_iv"] > 0] + \
              [r["put_iv"]  for r in rows if r["put_iv"]  > 0]
    avg_iv = sum(all_ivs) / len(all_ivs) if all_ivs else 20
    iv_environment = "HIGH" if avg_iv > 25 else ("LOW" if avg_iv < 14 else "NORMAL")

    # ---- Volume analysis ----
    total_call_vol = sum(r["call_volume"] for r in rows)
    total_put_vol  = sum(r["put_volume"]  for r in rows)
    vol_pcr = (total_put_vol / total_call_vol) if total_call_vol else 1.0

    # ---- Greeks at ATM ----
    atm_delta = atm_row["call_delta"]
    atm_gamma = atm_row["call_gamma"]
    atm_theta = atm_row["call_theta"]
    atm_vega  = atm_row["call_vega"]

    # ---- Gamma exposure (GEX) ----
    gex = sum(r["call_gamma"] * r["call_oi"] * cfg["lot"] * spot * spot / 100
              - r["put_gamma"] * r["put_oi"] * cfg["lot"] * spot * spot / 100
              for r in rows)
    gex_signal = "LONG GEX (stabilising)" if gex > 0 else "SHORT GEX (volatile)"

    # ---- Trend via spot vs ATM ----
    spot_vs_atm = spot - atm
    pct_from_atm = (spot_vs_atm / spot) * 100

    # ---- Days to expiry ----
    try:
        exp_date = datetime.datetime.strptime(expiry, "%Y-%m-%d")
        dte = max(0, (exp_date - datetime.datetime.now()).days)
    except Exception:
        dte = 7

    # ---- Theta decay warning ----
    theta_warning = dte <= 3

    # ==== SCORE-BASED SIGNAL ENGINE ====
    bull_score = 0
    bear_score = 0
    signals_bull = []
    signals_bear = []
    signals_neutral = []

    # 1. PCR
    if pcr > 1.3:
        bull_score += 2
        signals_bull.append(f"PCR={pcr:.2f} (>1.3) - heavy put writing, strong support")
    elif pcr > 1.1:
        bull_score += 1
        signals_bull.append(f"PCR={pcr:.2f} (>1.1) - mild bullish bias")
    elif pcr < 0.75:
        bear_score += 2
        signals_bear.append(f"PCR={pcr:.2f} (<0.75) - call overload, resistance forming")
    elif pcr < 0.9:
        bear_score += 1
        signals_bear.append(f"PCR={pcr:.2f} (<0.9) - mild bearish bias")
    else:
        signals_neutral.append(f"PCR={pcr:.2f} - neutral zone")

    # 2. Volume PCR
    if vol_pcr > 1.2:
        bull_score += 1
        signals_bull.append(f"Volume PCR={vol_pcr:.2f} - more puts traded (protective buying or short covering)")
    elif vol_pcr < 0.8:
        bear_score += 1
        signals_bear.append(f"Volume PCR={vol_pcr:.2f} - heavy call buying on downside, bearish")

    # 3. Max Pain vs Spot
    if spot < max_pain - step:
        bull_score += 2
        signals_bull.append(f"Spot {spot:.0f} < Max Pain {max_pain} - expect upward drift to Max Pain")
    elif spot > max_pain + step:
        bear_score += 2
        signals_bear.append(f"Spot {spot:.0f} > Max Pain {max_pain} - expect downward pull to Max Pain")
    else:
        signals_neutral.append(f"Spot near Max Pain {max_pain} - possible consolidation")

    # 4. OI Buildup/Unwind
    if put_oi_buildup > call_oi_buildup * 1.3:
        bull_score += 1
        signals_bull.append(f"Put OI building up faster - shorts adding protection (floor forming)")
    if call_oi_buildup > put_oi_buildup * 1.3:
        bear_score += 1
        signals_bear.append(f"Call OI building up faster - shorts adding calls (ceiling forming)")
    if put_oi_unwind > call_oi_unwind * 1.5:
        bear_score += 1
        signals_bear.append(f"Put writers unwinding - support weakening")
    if call_oi_unwind > put_oi_unwind * 1.5:
        bull_score += 1
        signals_bull.append(f"Call writers unwinding - resistance breaking")

    # 5. IV Skew
    if iv_skew > 3:
        bear_score += 1
        signals_bear.append(f"IV Skew={iv_skew:.1f}% - fear premium on puts, bearish sentiment")
    elif iv_skew < -2:
        bull_score += 1
        signals_bull.append(f"IV Skew={iv_skew:.1f}% - unusual call premium, aggressive bullishness")
    else:
        signals_neutral.append(f"IV Skew={iv_skew:.1f}% - balanced")

    # 6. Resistance/Support proximity
    dist_to_resistance = ((max_call_strike - spot) / spot) * 100
    dist_to_support    = ((spot - max_put_strike)  / spot) * 100
    if 0 < dist_to_resistance < 0.5:
        bear_score += 1
        signals_bear.append(f"Spot very close to call resistance at {max_call_strike}")
    if 0 < dist_to_support < 0.5:
        bull_score += 1
        signals_bull.append(f"Spot sitting on put support at {max_put_strike}")

    # 7. GEX
    if gex < 0:
        signals_neutral.append("Negative GEX - expect larger intraday swings, good for straddle")

    # ---- FINAL SIGNAL ----
    if bull_score >= 4:
        direction = "BUY"
        option_type = "CALL"
        confidence = "HIGH" if bull_score >= 6 else "MODERATE"
    elif bear_score >= 4:
        direction = "SELL/BUY"
        option_type = "PUT"
        confidence = "HIGH" if bear_score >= 6 else "MODERATE"
    elif bull_score >= 2 and bull_score > bear_score:
        direction = "BUY"
        option_type = "CALL"
        confidence = "LOW"
    elif bear_score >= 2 and bear_score > bull_score:
        direction = "SELL/BUY"
        option_type = "PUT"
        confidence = "LOW"
    else:
        direction = "WAIT"
        option_type = "NONE"
        confidence = "VERY LOW"

    # ---- Strike recommendation ----
    if option_type == "CALL":
        # Slightly OTM call: 1-2 strikes above spot
        otm_idx = min(atm_idx + (1 if dte > 5 else 2), len(rows) - 1)
        rec_strike = rows[otm_idx]["strike"]
        rec_ltp    = rows[otm_idx]["call_ltp"]
        rec_delta  = rows[otm_idx]["call_delta"]
        rec_iv     = rows[otm_idx]["call_iv"]
    elif option_type == "PUT":
        otm_idx = max(atm_idx - (1 if dte > 5 else 2), 0)
        rec_strike = rows[otm_idx]["strike"]
        rec_ltp    = rows[otm_idx]["put_ltp"]
        rec_delta  = rows[otm_idx]["put_delta"]
        rec_iv     = rows[otm_idx]["put_iv"]
    else:
        rec_strike = atm
        rec_ltp    = 0
        rec_delta  = 0
        rec_iv     = atm_call_iv

    # ---- HOLD / EXIT recommendation ----
    if direction == "WAIT":
        hold_advice = "DO NOT ENTER - No clear signal. Wait for PCR/OI confluence."
        hold_color  = "yellow"
    elif confidence in ("HIGH",):
        hold_advice = "GOOD TO ENTER & HOLD - Strong signal. Set SL at 30% of premium."
        hold_color  = "green"
    elif confidence == "MODERATE":
        hold_advice = "ENTER WITH CAUTION - Keep strict SL. Review on next refresh."
        hold_color  = "orange"
    else:
        hold_advice = "WEAK SIGNAL - Very small position only. High chance of reversal."
        hold_color  = "red"

    # ---- SL / Target ----
    sl_pct = 30
    tgt_pct = 60
    sl_price  = round(rec_ltp * (1 - sl_pct / 100), 2)  if rec_ltp else 0
    tgt_price = round(rec_ltp * (1 + tgt_pct / 100), 2) if rec_ltp else 0

    # ---- Lot value ----
    lot_value = round(rec_ltp * cfg["lot"], 2)

    return {
        "spot":             round(spot, 2),
        "atm":              atm,
        "expiry":           expiry,
        "dte":              dte,
        "symbol":           symbol,
        "direction":        direction,
        "option_type":      option_type,
        "confidence":       confidence,
        "rec_strike":       rec_strike,
        "rec_ltp":          rec_ltp,
        "rec_delta":        rec_delta,
        "rec_iv":           rec_iv,
        "sl_price":         sl_price,
        "tgt_price":        tgt_price,
        "lot_value":        lot_value,
        "lot_size":         cfg["lot"],
        "hold_advice":      hold_advice,
        "hold_color":       hold_color,
        "pcr":              round(pcr, 3),
        "vol_pcr":          round(vol_pcr, 3),
        "max_pain":         max_pain,
        "max_call_strike":  max_call_strike,
        "max_put_strike":   max_put_strike,
        "iv_skew":          round(iv_skew, 2),
        "avg_iv":           round(avg_iv, 2),
        "iv_environment":   iv_environment,
        "gex":              round(gex / 1e9, 2),
        "gex_signal":       gex_signal,
        "atm_call_iv":      atm_call_iv,
        "atm_put_iv":       atm_put_iv,
        "atm_delta":        round(atm_delta, 3),
        "atm_gamma":        round(atm_gamma, 4),
        "atm_theta":        round(atm_theta, 2),
        "atm_vega":         round(atm_vega, 2),
        "total_call_oi":    total_call_oi,
        "total_put_oi":     total_put_oi,
        "call_oi_buildup":  call_oi_buildup,
        "put_oi_buildup":   put_oi_buildup,
        "call_oi_unwind":   call_oi_unwind,
        "put_oi_unwind":    put_oi_unwind,
        "bull_score":       bull_score,
        "bear_score":       bear_score,
        "signals_bull":     signals_bull,
        "signals_bear":     signals_bear,
        "signals_neutral":  signals_neutral,
        "theta_warning":    theta_warning,
        "pct_from_atm":     round(pct_from_atm, 2),
        "dist_to_resistance": round(dist_to_resistance, 2),
        "dist_to_support":    round(dist_to_support, 2),
        "rows":             rows,
        "source":           fetcher.source if 'fetcher' in dir() else "Unknown",
        "timestamp":        datetime.datetime.now().strftime("%H:%M:%S"),
    }

# ============================================================
#  FLASK APP  +  DASHBOARD HTML
# ============================================================

DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>OptionsPulse Pro -- F&O Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=Epilogue:wght@400;600;800;900&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#04080f;--s1:#080f1c;--s2:#0c1525;--border:#162035;
  --g:#00ff88;--r:#ff3056;--b:#2d9cff;--y:#ffd55a;--p:#b06aff;
  --text:#dce8ff;--muted:#4a607f;--card:#0a1220;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Epilogue',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden}

body::before{content:'';position:fixed;inset:0;
  background:radial-gradient(ellipse 80% 50% at 20% 0%,rgba(45,156,255,.07),transparent),
             radial-gradient(ellipse 60% 40% at 80% 100%,rgba(0,255,136,.05),transparent);
  pointer-events:none;z-index:0}

.wrap{position:relative;z-index:1;max-width:1280px;margin:0 auto;padding:20px 16px}

/* HEADER */
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;flex-wrap:wrap;gap:12px}
.brand{display:flex;align-items:center;gap:10px}
.brand-icon{width:36px;height:36px;background:linear-gradient(135deg,var(--b),var(--g));border-radius:10px;
  display:flex;align-items:center;justify-content:center;font-size:1.1rem;font-weight:900;color:#000}
.brand h1{font-size:1.3rem;font-weight:900;letter-spacing:-1px}
.brand h1 span{color:var(--b)}
.brand sub{font-size:.55rem;font-family:'IBM Plex Mono',monospace;color:var(--muted);letter-spacing:2px;display:block;margin-top:-2px}

.hdr-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.live-dot{display:flex;align-items:center;gap:5px;font-family:'IBM Plex Mono',monospace;font-size:.65rem;
  color:var(--g);background:rgba(0,255,136,.08);border:1px solid rgba(0,255,136,.2);padding:4px 10px;border-radius:20px}
.live-dot::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--g);
  animation:blink 1.4s ease-in-out infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}
.clock{font-family:'IBM Plex Mono',monospace;font-size:.7rem;color:var(--muted);
  background:var(--s1);border:1px solid var(--border);padding:5px 10px;border-radius:7px}

/* CONTROLS */
.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-bottom:20px;
  background:var(--s1);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.ctrl{display:flex;flex-direction:column;gap:4px}
.ctrl label{font-size:.6rem;text-transform:uppercase;letter-spacing:1px;color:var(--muted);font-weight:600}
select,input[type=text],input[type=password]{
  background:var(--s2);border:1px solid var(--border);color:var(--text);
  padding:7px 10px;border-radius:7px;font-family:'IBM Plex Mono',monospace;font-size:.75rem;
  outline:none;transition:border-color .2s;min-width:140px}
select:focus,input:focus{border-color:var(--b)}
.btn{padding:8px 18px;border-radius:8px;border:none;font-family:'Epilogue',sans-serif;
  font-weight:800;font-size:.78rem;cursor:pointer;transition:all .2s;letter-spacing:.3px}
.btn-go{background:var(--b);color:#000;box-shadow:0 0 16px rgba(45,156,255,.3)}
.btn-go:hover{box-shadow:0 0 28px rgba(45,156,255,.5);transform:translateY(-1px)}
.btn-go:disabled{opacity:.45;cursor:not-allowed;transform:none;box-shadow:none}
.auto-badge{font-family:'IBM Plex Mono',monospace;font-size:.6rem;color:var(--y);
  background:rgba(255,213,90,.1);border:1px solid rgba(255,213,90,.2);padding:3px 8px;border-radius:5px}

/* LOADER */
.loader{display:none;text-align:center;padding:50px;color:var(--muted);font-family:'IBM Plex Mono',monospace;font-size:.8rem}
.spin{width:28px;height:28px;border:2px solid var(--border);border-top-color:var(--b);
  border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 12px}
@keyframes spin{to{transform:rotate(360deg)}}

/* ERROR */
.errmsg{display:none;background:rgba(255,48,86,.08);border:1px solid rgba(255,48,86,.25);
  border-radius:9px;padding:12px 16px;font-size:.78rem;color:#ff7595;margin-bottom:16px}

/* SIGNAL HERO */
.signal-hero{display:none;border-radius:14px;padding:20px 22px;margin-bottom:20px;
  border:1.5px solid;position:relative;overflow:hidden;animation:slideIn .35s ease}
@keyframes slideIn{from{opacity:0;transform:translateY(-10px)}to{opacity:1;transform:translateY(0)}}
.signal-hero.bull{background:rgba(0,255,136,.05);border-color:rgba(0,255,136,.25)}
.signal-hero.bear{background:rgba(255,48,86,.05);border-color:rgba(255,48,86,.25)}
.signal-hero.wait{background:rgba(255,213,90,.05);border-color:rgba(255,213,90,.2)}
.signal-hero::before{content:'';position:absolute;top:0;left:0;right:0;height:3px}
.signal-hero.bull::before{background:linear-gradient(90deg,var(--g),rgba(0,255,136,0))}
.signal-hero.bear::before{background:linear-gradient(90deg,var(--r),rgba(255,48,86,0))}
.signal-hero.wait::before{background:linear-gradient(90deg,var(--y),rgba(255,213,90,0))}

.sh-top{display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px;margin-bottom:16px}
.sh-action{font-size:2rem;font-weight:900;letter-spacing:-2px;line-height:1}
.sh-action.bull{color:var(--g)}
.sh-action.bear{color:var(--r)}
.sh-action.wait{color:var(--y)}
.sh-badges{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.badge{padding:4px 10px;border-radius:20px;font-family:'IBM Plex Mono',monospace;font-size:.65rem;font-weight:700;letter-spacing:.5px}
.badge-conf-high{background:rgba(0,255,136,.15);color:var(--g);border:1px solid rgba(0,255,136,.3)}
.badge-conf-mod{background:rgba(255,213,90,.15);color:var(--y);border:1px solid rgba(255,213,90,.3)}
.badge-conf-low{background:rgba(255,48,86,.15);color:var(--r);border:1px solid rgba(255,48,86,.3)}
.badge-dte{background:rgba(45,156,255,.1);color:var(--b);border:1px solid rgba(45,156,255,.2)}
.badge-iv{background:rgba(176,106,255,.1);color:var(--p);border:1px solid rgba(176,106,255,.2)}

.sh-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px}
.sh-item{background:rgba(255,255,255,.03);border-radius:8px;padding:10px 12px}
.sh-item-label{font-size:.58rem;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:4px}
.sh-item-val{font-family:'IBM Plex Mono',monospace;font-size:.95rem;font-weight:700}
.c-g{color:var(--g)} .c-r{color:var(--r)} .c-b{color:var(--b)} .c-y{color:var(--y)} .c-p{color:var(--p)} .c-w{color:var(--text)}

.hold-bar{border-radius:8px;padding:10px 14px;font-size:.8rem;font-weight:600;display:flex;align-items:center;gap:8px}
.hold-bar.green{background:rgba(0,255,136,.1);color:var(--g);border:1px solid rgba(0,255,136,.2)}
.hold-bar.orange{background:rgba(255,160,50,.1);color:#ffa032;border:1px solid rgba(255,160,50,.2)}
.hold-bar.red{background:rgba(255,48,86,.1);color:var(--r);border:1px solid rgba(255,48,86,.2)}
.hold-bar.yellow{background:rgba(255,213,90,.1);color:var(--y);border:1px solid rgba(255,213,90,.2)}
.hold-icon{font-size:1rem}

/* SCORE BAR */
.score-section{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:20px}
.score-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.score-title{font-size:.62rem;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px;font-weight:700}
.score-title.bull-t{color:var(--g)} .score-title.bear-t{color:var(--r)}
.score-bar-wrap{height:6px;background:var(--border);border-radius:3px;margin-bottom:10px;overflow:hidden}
.score-bar{height:100%;border-radius:3px;transition:width .6s ease}
.score-bar.bull{background:linear-gradient(90deg,var(--g),rgba(0,255,136,.4))}
.score-bar.bear{background:linear-gradient(90deg,var(--r),rgba(255,48,86,.4))}
.score-num{font-family:'IBM Plex Mono',monospace;font-size:1.4rem;font-weight:700;margin-bottom:8px}
.signal-list{list-style:none;display:flex;flex-direction:column;gap:5px}
.signal-list li{font-size:.7rem;color:var(--muted);line-height:1.4;padding-left:10px;position:relative}
.signal-list li::before{content:'>';position:absolute;left:0;font-weight:700}
.signal-list.bull li::before{color:var(--g)}
.signal-list.bear li::before{color:var(--r)}

/* METRICS GRID */
.metrics-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:20px}
.mc{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 14px;transition:border-color .2s}
.mc:hover{border-color:rgba(45,156,255,.3)}
.mc-label{font-size:.58rem;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:5px}
.mc-val{font-family:'IBM Plex Mono',monospace;font-size:1rem;font-weight:700}
.mc-sub{font-family:'IBM Plex Mono',monospace;font-size:.62rem;margin-top:3px;color:var(--muted)}

/* GREEKS ROW */
.greeks-row{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px}
.greek-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 14px;text-align:center}
.greek-sym{font-size:1.2rem;font-weight:900;margin-bottom:4px}
.greek-label{font-size:.6rem;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px}
.greek-val{font-family:'IBM Plex Mono',monospace;font-size:.9rem;font-weight:700}
.greek-desc{font-size:.6rem;color:var(--muted);margin-top:3px}

/* SECTION TITLE */
.stitle{font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;
  color:var(--muted);margin-bottom:10px;display:flex;align-items:center;gap:8px}
.stitle::after{content:'';flex:1;height:1px;background:var(--border)}

/* CHAIN TABLE */
.chain-wrap{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden;margin-bottom:20px;overflow-x:auto}
table{width:100%;border-collapse:collapse;min-width:700px}
thead tr{background:var(--s1)}
th{padding:9px 12px;font-size:.58rem;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);
   font-weight:700;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap}
th.center{text-align:center}
tbody tr{border-bottom:1px solid rgba(22,32,53,.6);transition:background .12s}
tbody tr:hover{background:rgba(45,156,255,.04)}
tbody tr.atm-row{background:rgba(255,213,90,.04)}
tbody tr.atm-row td.strike-col{color:var(--y);font-weight:700}
td{padding:8px 12px;font-family:'IBM Plex Mono',monospace;font-size:.72rem;text-align:right;white-space:nowrap}
td.center{text-align:center}
.oi-wrap{display:flex;align-items:center;gap:5px;justify-content:flex-end}
.oi-spark{height:3px;border-radius:2px;min-width:2px}
.spark-c{background:var(--r)} .spark-p{background:var(--g)}
.chg-pos{color:var(--g)} .chg-neg{color:var(--r)} .chg-zero{color:var(--muted)}

/* FOOTER */
.footer{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.fp{background:var(--s1);border:1px solid var(--border);border-radius:5px;
  padding:4px 10px;font-size:.62rem;color:var(--muted);font-family:'IBM Plex Mono',monospace}
.fp span{color:var(--text)}
.disc{font-size:.62rem;color:var(--muted);border-top:1px solid var(--border);padding-top:14px;line-height:1.7}

/* RESPONSIVE */
@media(max-width:640px){
  .greeks-row{grid-template-columns:repeat(2,1fr)}
  .score-section{grid-template-columns:1fr}
}
</style>
</head>
<body>
<div class="wrap">

<header>
  <div class="brand">
    <div class="brand-icon">FO</div>
    <div>
      <h1>Options<span>Pulse</span> Pro</h1>
      <sub>ADVANCED F&O DASHBOARD</sub>
    </div>
  </div>
  <div class="hdr-right">
    <div class="live-dot" id="liveBadge">LIVE</div>
    <div class="clock" id="clock">--:--:--</div>
  </div>
</header>

<!-- CONTROLS -->
<div class="controls">
  <div class="ctrl">
    <label>API Key (GrowAPI)</label>
    <input type="password" id="apiKey" placeholder="Bearer key (optional)" style="min-width:180px">
  </div>
  <div class="ctrl">
    <label>Symbol</label>
    <select id="symbol">
      <option value="NIFTY">NIFTY</option>
      <option value="BANKNIFTY">BANKNIFTY</option>
      <option value="FINNIFTY">FINNIFTY</option>
      <option value="MIDCPNIFTY">MIDCPNIFTY</option>
      <option value="SENSEX">SENSEX</option>
      <option value="RELIANCE">RELIANCE</option>
      <option value="TCS">TCS</option>
      <option value="INFY">INFY</option>
      <option value="HDFCBANK">HDFCBANK</option>
    </select>
  </div>
  <div class="ctrl">
    <label>Expiry</label>
    <select id="expiry"></select>
  </div>
  <div class="ctrl">
    <label>Auto-refresh</label>
    <select id="autoRef">
      <option value="0">Off</option>
      <option value="30" selected>30s</option>
      <option value="60">60s</option>
      <option value="120">2 min</option>
    </select>
  </div>
  <div class="ctrl" style="justify-content:flex-end">
    <label>&nbsp;</label>
    <button class="btn btn-go" id="fetchBtn" onclick="fetchData()">Fetch + Analyse</button>
  </div>
  <div class="auto-badge" id="autoLabel" style="display:none">AUTO ON</div>
</div>

<div class="errmsg" id="errMsg"></div>
<div class="loader" id="loader"><div class="spin"></div>Fetching live F&O data...</div>

<!-- SIGNAL HERO -->
<div class="signal-hero" id="sigHero">
  <div class="sh-top">
    <div>
      <div class="sh-action" id="shAction">BUY CALL</div>
      <div style="font-size:.75rem;color:var(--muted);margin-top:4px" id="shSub">Recommended strike &amp; entry</div>
    </div>
    <div class="sh-badges" id="shBadges"></div>
  </div>
  <div class="sh-grid" id="shGrid"></div>
  <div class="hold-bar" id="holdBar"><span class="hold-icon">-</span><span id="holdText">-</span></div>
</div>

<!-- BULL / BEAR SCORE -->
<div class="score-section" id="scoreSection" style="display:none">
  <div class="score-card">
    <div class="score-title bull-t">Bullish Signals <span id="bullScoreNum" style="font-family:'IBM Plex Mono',monospace"></span></div>
    <div class="score-bar-wrap"><div class="score-bar bull" id="bullBar"></div></div>
    <ul class="signal-list bull" id="bullList"></ul>
  </div>
  <div class="score-card">
    <div class="score-title bear-t">Bearish Signals <span id="bearScoreNum" style="font-family:'IBM Plex Mono',monospace"></span></div>
    <div class="score-bar-wrap"><div class="score-bar bear" id="bearBar"></div></div>
    <ul class="signal-list bear" id="bearList"></ul>
  </div>
</div>

<!-- METRICS -->
<div id="metricsSection" style="display:none">
  <div class="stitle">Market Parameters</div>
  <div class="metrics-grid" id="metricsGrid"></div>

  <div class="stitle">ATM Greeks (Call side)</div>
  <div class="greeks-row" id="greeksRow"></div>

  <div class="stitle">Option Chain</div>
  <div class="chain-wrap"><table>
    <thead>
      <tr>
        <th>Chg OI</th><th>Call OI</th><th>Call Vol</th>
        <th>IV%</th><th>Call LTP</th>
        <th class="center">Strike</th>
        <th>Put LTP</th><th>IV%</th>
        <th>Put Vol</th><th>Put OI</th><th>Chg OI</th>
      </tr>
    </thead>
    <tbody id="chainBody"></tbody>
  </table></div>

  <div class="footer" id="footerRow"></div>
  <div class="disc">
    WARNING: This tool is for educational and informational purposes only.
    Signals are generated using OI, PCR, Max Pain, IV Skew, GEX and Greeks analysis.
    They do NOT constitute SEBI-registered financial advice.
    Options trading carries substantial risk of loss. Always consult a qualified advisor.
  </div>
</div>

</div><!-- /wrap -->

<script>
var autoTimer = null;

// Clock
function tick(){
  var d=new Date();
  document.getElementById('clock').textContent=
    d.toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
}
setInterval(tick,1000);tick();

// Expiry generator
function loadExpiries(){
  var sym=document.getElementById('symbol').value;
  var sel=document.getElementById('expiry');
  sel.innerHTML='';
  var now=new Date();
  var dayTarget=(sym==='BANKNIFTY'||sym==='SENSEX')?3:4;
  var dates=[];
  var d=new Date(now);
  for(var w=0;w<8;w++){
    d=new Date(now);
    var diff=(dayTarget-d.getDay()+7)%7;
    if(diff===0) diff=7;
    d.setDate(d.getDate()+diff+w*7);
    var iso=d.toISOString().split('T')[0];
    if(!dates.includes(iso)) dates.push(iso);
  }
  dates.slice(0,5).forEach(function(dt,i){
    var o=document.createElement('option');
    o.value=dt;
    var dd=new Date(dt);
    o.textContent=dd.toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'});
    if(i===0) o.selected=true;
    sel.appendChild(o);
  });
}
loadExpiries();
document.getElementById('symbol').addEventListener('change',loadExpiries);

// Auto-refresh
document.getElementById('autoRef').addEventListener('change',function(){
  if(autoTimer) clearInterval(autoTimer);
  autoTimer=null;
  var v=parseInt(this.value);
  var lb=document.getElementById('autoLabel');
  if(v>0){
    lb.style.display='flex';
    autoTimer=setInterval(fetchData,v*1000);
  } else {
    lb.style.display='none';
  }
});

// Format helpers
function fmtOI(n){
  if(!n&&n!==0) return '--';
  if(n>=10000000) return (n/10000000).toFixed(2)+'Cr';
  if(n>=100000)   return (n/100000).toFixed(1)+'L';
  if(n>=1000)     return (n/1000).toFixed(1)+'K';
  return n.toString();
}
function fmtP(n){
  if(!n&&n!==0) return '--';
  return 'Rs.'+Number(n).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
}
function fmtChg(n){
  if(!n) return '<span class="chg-zero">0</span>';
  var c=n>0?'chg-pos':'chg-neg';
  return '<span class="'+c+'">'+(n>0?'+':'')+fmtOI(n)+'</span>';
}

// MAIN FETCH
function fetchData(){
  var sym=document.getElementById('symbol').value;
  var exp=document.getElementById('expiry').value;
  var key=document.getElementById('apiKey').value.trim();
  var btn=document.getElementById('fetchBtn');
  btn.disabled=true; btn.textContent='Fetching...';
  document.getElementById('loader').style.display='block';
  document.getElementById('errMsg').style.display='none';
  document.getElementById('sigHero').style.display='none';
  document.getElementById('scoreSection').style.display='none';
  document.getElementById('metricsSection').style.display='none';

  var url='/api/analyse?symbol='+sym+'&expiry='+exp+'&api_key='+encodeURIComponent(key);
  fetch(url)
    .then(function(r){return r.json();})
    .then(function(d){
      document.getElementById('loader').style.display='none';
      btn.disabled=false; btn.textContent='Fetch + Analyse';
      if(d.error){
        document.getElementById('errMsg').textContent=d.error;
        document.getElementById('errMsg').style.display='block';
        return;
      }
      renderAll(d);
    })
    .catch(function(e){
      document.getElementById('loader').style.display='none';
      btn.disabled=false; btn.textContent='Fetch + Analyse';
      document.getElementById('errMsg').textContent='Request failed: '+e.message;
      document.getElementById('errMsg').style.display='block';
    });
}

function renderAll(d){
  renderHero(d);
  renderScores(d);
  renderMetrics(d);
  renderGreeks(d);
  renderChain(d);
  renderFooter(d);
  document.getElementById('metricsSection').style.display='block';
}

function renderHero(d){
  var hero=document.getElementById('sigHero');
  hero.className='signal-hero';
  var isWait=d.direction==='WAIT';
  var isBull=d.direction==='BUY';
  hero.classList.add(isWait?'wait':(isBull?'bull':'bear'));
  hero.style.display='block';

  var action=isWait?'WAIT / NEUTRAL':(d.direction+' '+d.option_type);
  var actionEl=document.getElementById('shAction');
  actionEl.textContent=action;
  actionEl.className='sh-action '+(isWait?'wait':(isBull?'bull':'bear'));
  document.getElementById('shSub').textContent=
    isWait?'No clear directional signal - stay on sidelines':
    'Recommended entry at strike '+d.rec_strike;

  // Badges
  var bc=d.confidence==='HIGH'?'badge-conf-high':(d.confidence==='MODERATE'?'badge-conf-mod':'badge-conf-low');
  document.getElementById('shBadges').innerHTML=
    '<span class="badge '+bc+'">'+d.confidence+' CONFIDENCE</span>'+
    '<span class="badge badge-dte">DTE: '+d.dte+'</span>'+
    '<span class="badge badge-iv">IV: '+d.avg_iv+'%</span>'+
    (d.theta_warning?'<span class="badge badge-conf-low">THETA RISK</span>':'');

  // Grid
  var items=[
    {l:'Rec. Strike',v:isWait?'N/A':fmtP(d.rec_strike),c:isBull?'c-g':(isWait?'c-y':'c-r')},
    {l:'Option Type', v:d.option_type,       c:d.option_type==='CALL'?'c-g':(d.option_type==='PUT'?'c-r':'c-y')},
    {l:'Entry (LTP)', v:d.rec_ltp?fmtP(d.rec_ltp):'--', c:'c-w'},
    {l:'Stop Loss',   v:d.sl_price?fmtP(d.sl_price):'--', c:'c-r'},
    {l:'Target',      v:d.tgt_price?fmtP(d.tgt_price):'--', c:'c-g'},
    {l:'Lot Value',   v:d.lot_value?fmtP(d.lot_value):'--', c:'c-b'},
    {l:'Spot Price',  v:fmtP(d.spot),         c:'c-w'},
    {l:'Delta',       v:d.rec_delta?d.rec_delta:'--',  c:'c-p'},
  ];
  document.getElementById('shGrid').innerHTML=items.map(function(it){
    return '<div class="sh-item"><div class="sh-item-label">'+it.l+'</div>'+
           '<div class="sh-item-val '+it.c+'">'+it.v+'</div></div>';
  }).join('');

  // Hold bar
  var hb=document.getElementById('holdBar');
  hb.className='hold-bar '+d.hold_color;
  document.getElementById('holdBar').querySelector('.hold-icon').textContent=
    d.hold_color==='green'?'[HOLD]':(d.hold_color==='red'?'[EXIT]':'[WAIT]');
  document.getElementById('holdText').textContent=d.hold_advice;
}

function renderScores(d){
  document.getElementById('scoreSection').style.display='grid';
  var maxScore=8;
  document.getElementById('bullScoreNum').textContent='('+d.bull_score+'/'+maxScore+')';
  document.getElementById('bearScoreNum').textContent='('+d.bear_score+'/'+maxScore+')';
  document.getElementById('bullBar').style.width=Math.min(100,(d.bull_score/maxScore)*100)+'%';
  document.getElementById('bearBar').style.width=Math.min(100,(d.bear_score/maxScore)*100)+'%';
  document.getElementById('bullList').innerHTML=
    (d.signals_bull.length?d.signals_bull:['No bullish signals detected']).map(function(s){
      return '<li>'+s+'</li>';}).join('');
  document.getElementById('bearList').innerHTML=
    (d.signals_bear.length?d.signals_bear:['No bearish signals detected']).map(function(s){
      return '<li>'+s+'</li>';}).join('');
}

function renderMetrics(d){
  var items=[
    {l:'Spot Price',       v:fmtP(d.spot),          c:'c-w'},
    {l:'ATM Strike',       v:fmtP(d.atm),            c:'c-y'},
    {l:'Max Pain',         v:fmtP(d.max_pain),       c:'c-b', sub:d.spot<d.max_pain?'Spot BELOW (bull)':'Spot ABOVE (bear)'},
    {l:'PCR (OI)',         v:d.pcr,                  c:d.pcr>1.2?'c-g':(d.pcr<0.85?'c-r':'c-w'),
      sub:d.pcr>1.2?'Bullish':(d.pcr<0.85?'Bearish':'Neutral')},
    {l:'PCR (Vol)',        v:d.vol_pcr,              c:d.vol_pcr>1.1?'c-g':(d.vol_pcr<0.9?'c-r':'c-w')},
    {l:'Resistance',       v:fmtP(d.max_call_strike),c:'c-r',  sub:'Max Call OI'},
    {l:'Support',          v:fmtP(d.max_put_strike), c:'c-g',  sub:'Max Put OI'},
    {l:'IV Skew (P-C)',    v:d.iv_skew+'%',          c:d.iv_skew>3?'c-r':(d.iv_skew<-2?'c-g':'c-w')},
    {l:'Avg IV',           v:d.avg_iv+'%',           c:'c-p',  sub:d.iv_environment},
    {l:'GEX (B$)',         v:d.gex,                  c:d.gex>0?'c-g':'c-r', sub:d.gex_signal},
    {l:'Call OI Buildup',  v:fmtOI(d.call_oi_buildup), c:'c-r'},
    {l:'Put OI Buildup',   v:fmtOI(d.put_oi_buildup),  c:'c-g'},
    {l:'Call OI Unwind',   v:fmtOI(d.call_oi_unwind),  c:'c-y'},
    {l:'Put OI Unwind',    v:fmtOI(d.put_oi_unwind),   c:'c-y'},
    {l:'Dist Resistance',  v:d.dist_to_resistance+'%', c:'c-r'},
    {l:'Dist Support',     v:d.dist_to_support+'%',    c:'c-g'},
  ];
  document.getElementById('metricsGrid').innerHTML=items.map(function(it){
    return '<div class="mc"><div class="mc-label">'+it.l+'</div>'+
           '<div class="mc-val '+it.c+'">'+it.v+'</div>'+
           (it.sub?'<div class="mc-sub">'+it.sub+'</div>':'')+
           '</div>';
  }).join('');
}

function renderGreeks(d){
  var gs=[
    {sym:'D',name:'Delta',val:d.atm_delta,
     desc:d.atm_delta>0.5?'Deep ITM - high directional':'Standard ATM delta ~0.5'},
    {sym:'G',name:'Gamma',val:d.atm_gamma,
     desc:'Rate of delta change - '+(d.atm_gamma>0.01?'High near expiry':'Normal')},
    {sym:'T',name:'Theta',val:d.atm_theta,
     desc:'Daily decay: '+fmtP(d.atm_theta)+' per lot/day'},
    {sym:'V',name:'Vega', val:d.atm_vega,
     desc:'IV sensitivity: '+(d.avg_iv>25?'High IV = sell premium':'Low IV = buy premium')},
  ];
  var cols=['c-g','c-b','c-r','c-p'];
  document.getElementById('greeksRow').innerHTML=gs.map(function(g,i){
    return '<div class="greek-card">'+
           '<div class="greek-sym '+cols[i]+'">'+g.sym+'</div>'+
           '<div class="greek-label">'+g.name+'</div>'+
           '<div class="greek-val '+cols[i]+'">'+g.val+'</div>'+
           '<div class="greek-desc">'+g.desc+'</div>'+
           '</div>';
  }).join('');
}

function renderChain(d){
  if(!d.rows||!d.rows.length) return;
  var maxOI=Math.max.apply(null,d.rows.map(function(r){return Math.max(r.call_oi,r.put_oi)}));
  var atmIdx=d.rows.findIndex(function(r){return r.strike===d.atm});
  var start=Math.max(0,atmIdx-5);
  var slice=d.rows.slice(start,start+11);

  document.getElementById('chainBody').innerHTML=slice.map(function(r){
    var isATM=r.strike===d.atm;
    var cSpark=Math.round((r.call_oi/maxOI)*50);
    var pSpark=Math.round((r.put_oi/maxOI)*50);
    return '<tr class="'+(isATM?'atm-row':'')+'">'+
      '<td>'+fmtChg(r.call_chg_oi)+'</td>'+
      '<td><div class="oi-wrap">'+fmtOI(r.call_oi)+
        '<div class="oi-spark spark-c" style="width:'+cSpark+'px"></div></div></td>'+
      '<td>'+fmtOI(r.call_volume)+'</td>'+
      '<td class="c-p">'+r.call_iv+'</td>'+
      '<td class="c-r">'+fmtP(r.call_ltp)+'</td>'+
      '<td class="center strike-col">'+r.strike.toLocaleString('en-IN')+(isATM?' *':'')+'</td>'+
      '<td class="c-g">'+fmtP(r.put_ltp)+'</td>'+
      '<td class="c-p">'+r.put_iv+'</td>'+
      '<td>'+fmtOI(r.put_volume)+'</td>'+
      '<td><div class="oi-wrap"><div class="oi-spark spark-p" style="width:'+pSpark+'px"></div>'+
        fmtOI(r.put_oi)+'</div></td>'+
      '<td>'+fmtChg(r.put_chg_oi)+'</td>'+
      '</tr>';
  }).join('');
}

function renderFooter(d){
  document.getElementById('footerRow').innerHTML=
    '<div class="fp">Symbol: <span>'+d.symbol+'</span></div>'+
    '<div class="fp">Expiry: <span>'+d.expiry+'</span></div>'+
    '<div class="fp">DTE: <span>'+d.dte+'</span></div>'+
    '<div class="fp">Updated: <span>'+d.timestamp+'</span></div>'+
    '<div class="fp">Source: <span>'+d.source+'</span></div>'+
    '<div class="fp">Lot: <span>'+d.lot_size+'</span></div>';
}

// Auto-load
window.addEventListener('load',function(){setTimeout(fetchData,500);});
</script>
</body>
</html>
"""

# ============================================================
#  FLASK ROUTES
# ============================================================

if HAS_FLASK:
    app  = Flask(__name__)
    CORS(app)
    fetcher = DataFetcher()

    @app.route("/")
    def index():
        return render_template_string(DASHBOARD_HTML)

    @app.route("/api/analyse")
    def api_analyse():
        try:
            symbol  = request.args.get("symbol",  "NIFTY").upper()
            expiry  = request.args.get("expiry",  "")
            api_key = request.args.get("api_key", DEFAULT_API_KEY)

            if symbol not in SYMBOLS:
                symbol = "NIFTY"  # safe fallback instead of error

            if not expiry:
                # Auto-compute nearest expiry Thursday (or Wednesday for BN/SENSEX)
                now = datetime.datetime.now()
                day_target = 3 if symbol in ("BANKNIFTY", "SENSEX") else 4
                days_ahead = (day_target - now.weekday()) % 7
                if days_ahead == 0:
                    days_ahead = 7
                d = now + datetime.timedelta(days=days_ahead)
                expiry = d.strftime("%Y-%m-%d")

            # Fetch with full fallback chain
            raw = fetcher.get_chain(symbol, expiry, api_key)

            # If somehow still None, force mock
            if raw is None:
                raw = fetcher.generate_mock(symbol, expiry)

            spot, rows = normalise_chain(raw)

            # Ultimate safety net - if rows still empty, generate fresh mock
            if not rows or spot is None:
                raw   = fetcher.generate_mock(symbol, expiry)
                spot, rows = normalise_chain(raw)

            # If still empty after all fallbacks, return a clear error
            if not rows:
                return jsonify({"error": "Could not generate data. Please try again."}), 500

            result = analyse(spot, rows, symbol, expiry)
            result["source"] = fetcher.source
            return jsonify(result)

        except Exception as e:
            import traceback
            print("API error:", traceback.format_exc())
            # Last resort: return mock data silently
            try:
                sym = request.args.get("symbol", "NIFTY").upper()
                if sym not in SYMBOLS:
                    sym = "NIFTY"
                exp = request.args.get("expiry", datetime.datetime.now().strftime("%Y-%m-%d"))
                raw = fetcher.generate_mock(sym, exp)
                spot, rows = normalise_chain(raw)
                result = analyse(spot, rows, sym, exp)
                result["source"] = "Simulated (fallback)"
                return jsonify(result)
            except Exception:
                return jsonify({"error": f"Server error: {str(e)}"}), 500

    @app.route("/api/symbols")
    def api_symbols():
        return jsonify(list(SYMBOLS.keys()))

# ============================================================
#  ENTRY POINT
# ============================================================

def check_deps():
    missing = []
    if not HAS_REQUESTS: missing.append("requests")
    if not HAS_FLASK:    missing.append("flask flask-cors")
    return missing

if __name__ == "__main__":
    print("=" * 60)
    print("  OptionsPulse Pro -- Advanced F&O Dashboard")
    print("=" * 60)

    missing = check_deps()
    if missing:
        print(f"\n[!] Missing packages. Run:\n    pip install {' '.join(missing)}\n")
        import sys; sys.exit(1)

    print("\n[OK] All dependencies found.")
    print("[>>] Starting dashboard server on http://localhost:5050")
    print("[>>] Press Ctrl+C to stop.\n")
    print("  Supported Symbols:", ", ".join(SYMBOLS.keys()))
    print("  GrowAPI Key  : Set DEFAULT_API_KEY in script or enter in UI")
    print("  Data fallback: NSE India (public) -> Simulated data")
    print()

    import os
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
