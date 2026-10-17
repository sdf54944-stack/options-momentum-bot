import os, math, json
import yfinance as yf
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from scipy.stats import norm

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

TICKER = os.environ.get("GEX_TICKER", "SPY")
R = 0.045                      # سعر فائدة تقريبي
N_EXPIRIES = 1                 # أقرب استحقاق (0DTE/أقرب) — GEX عادةً للأقرب
SPX_MULT = 10                  # SPY→SPX للعرض (تقريبي)
RIYADH = ZoneInfo("Asia/Riyadh")

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID,
                                 "text": text, "parse_mode": "HTML"})
    if not r.ok:
        print("telegram error:", r.text)
    return r.ok

def bs_gamma(S, K, T, sigma):
    # Gamma حسب Black-Scholes
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (R + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return norm.pdf(d1) / (S * sigma * math.sqrt(T))

def get_spot(ticker):
    t = yf.Ticker(ticker)
    px = t.history(period="1d")
    if px.empty:
        return None
    return float(px["Close"].iloc[-1])

def compute_gex(ticker):
    t = yf.Ticker(ticker)
    spot = get_spot(ticker)
    if spot is None:
        return None
    expiries = t.options[:N_EXPIRIES]
    if not expiries:
        return None
    now = datetime.now(timezone.utc)
    rows = []
    used_expiry = expiries[0]
    for exp in expiries:
        exp_dt = datetime.strptime(exp, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        T = max((exp_dt - now).total_seconds() / (365 * 24 * 3600), 1e-6)
        try:
            chain = t.option_chain(exp)
        except Exception as e:
            print(f"chain error {exp}: {e}")
            continue
        for df, sign in [(chain.calls, +1), (chain.puts, -1)]:
            for r in df.itertuples():
                iv = getattr(r, "impliedVolatility", 0) or 0
                oi = getattr(r, "openInterest", 0) or 0
                if iv <= 0 or oi <= 0:
                    continue
                gamma = bs_gamma(spot, float(r.strike), T, float(iv))
                # GEX per strike ($ gamma) — تقريب معياري
                gex = sign * gamma * oi * 100 * spot * spot * 0.01
                rows.append({"strike": float(r.strike), "gex": gex, "sign": sign})
    if not rows:
        return None
    g = pd.DataFrame(rows)
    by_strike = g.groupby("strike")["gex"].sum().sort_index()

    net = g["gex"].sum()
    call_gex = g[g["sign"] > 0]["gex"].sum()
    put_gex = g[g["sign"] < 0]["gex"].sum()

    # Call Wall: أعلى GEX موجب | Put Wall: أدنى GEX (أكبر سالب)
    call_wall = by_strike.idxmax()
    put_wall = by_strike.idxmin()

    # Gamma Flip: الـstrike حيث يتحول GEX التراكمي من سالب لموجب
    cum = by_strike.cumsum()
    flip = None
    prev_k = None
    for k, v in cum.items():
        if prev_k is not None and ((cum[prev_k] < 0) != (v < 0)):
            flip = k
            break
        prev_k = k
    if flip is None:
        flip = by_strike.abs().idxmin()

    return {
        "spot": spot, "expiry": used_expiry,
        "net": net, "call_gex": call_gex, "put_gex": put_gex,
        "call_wall": call_wall, "put_wall": put_wall, "flip": flip,
    }

def b(x):
    return f"${x/1e9:,.2f}B"

def report(ticker):
    d = compute_gex(ticker)
    stamp = datetime.now(RIYADH).strftime("%d %b %Y · %I:%M %p KSA")
    if d is None:
        send_telegram(f"📈 <b>GEX {ticker}</b>\nلا بيانات كافية.\n{stamp}")
        return
    disp = SPX_MULT if ticker == "SPY" else 1
    msg = (
        f"📈 <b>Gamma Exposure (تقريبي)</b>\n"
        f"🏷 Ticker: {ticker}  (≈SPX ×{disp})\n"
        f"📅 Expiry: {d['expiry']}\n"
        f"💰 Spot: {d['spot']:.2f}  (≈SPX {d['spot']*disp:.0f})\n\n"
        f"<b>السيولة:</b>\n"
        f"🟢 Net GEX  {b(d['net'])}\n"
        f"🟢 Call GEX {b(d['call_gex'])}\n"
        f"🔴 Put GEX  {b(d['put_gex'])}\n\n"
        f"<b>الجدران:</b>\n"
        f"🟢 Call Wall {d['call_wall']:.0f}  (≈{d['call_wall']*disp:.0f})\n"
        f"🔴 Put Wall  {d['put_wall']:.0f}  (≈{d['put_wall']*disp:.0f})\n\n"
        f"<b>الفاصل:</b>\n"
        f"⚖️ Gamma Flip {d['flip']:.0f}  (≈{d['flip']*disp:.0f})\n\n"
        f"🕐 {stamp}\n"
        f"⚠️ تقريبي على SPY — ليس بديلاً عن مصدر دقيق."
    )
    send_telegram(msg)

if __name__ == "__main__":
    report(TICKER)
