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
R = 0.045                 # سعر فائدة تقريبي
N_EXPIRIES = 1            # أقرب استحقاق
SPX_MULT = 10             # SPY→SPX (السعر والقيم)
NEAR_PCT = 0.15           # نطاق البحث عن Flip حول السعر (±15%)
RIYADH = ZoneInfo("Asia/Riyadh")


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID,
                                 "text": text, "parse_mode": "HTML"})
    if not r.ok:
        print("telegram error:", r.text)
    return r.ok


def bs_gamma(S, K, T, sigma):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (R + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return norm.pdf(d1) / (S * sigma * math.sqrt(T))


def get_spot(ticker):
    try:
        px = yf.Ticker(ticker).history(period="1d")
        if px.empty:
            return None
        return float(px["Close"].iloc[-1])
    except Exception as e:
        print(f"spot error: {e}")
        return None


def compute_gex(ticker):
    t = yf.Ticker(ticker)
    spot = get_spot(ticker)
    if spot is None:
        return None
    try:
        expiries = t.options[:N_EXPIRIES]
    except Exception as e:
        print(f"options error: {e}")
        return None
    if not expiries:
        return None

    now = datetime.now(timezone.utc)
    rows = []
    used_expiry = expiries[0]
    for exp in expiries:
        try:
            exp_dt = datetime.strptime(exp, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            continue
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
                strike = getattr(r, "strike", 0) or 0
                if iv <= 0 or oi <= 0 or strike <= 0:
                    continue
                gamma = bs_gamma(spot, float(strike), T, float(iv))
                gex = sign * gamma * oi * 100 * spot * spot * 0.01
                rows.append({"strike": float(strike), "gex": gex, "sign": sign})

    if not rows:
        return None

    g = pd.DataFrame(rows)
    by_strike = g.groupby("strike")["gex"].sum().sort_index()
    if by_strike.empty:
        return None

    net = g["gex"].sum()
    call_gex = g[g["sign"] > 0]["gex"].sum()
    put_gex = g[g["sign"] < 0]["gex"].sum()
    call_wall = by_strike.idxmax()
    put_wall = by_strike.idxmin()

    # Gamma Flip: التقاطع الأقرب للسعر بين كل تقاطعات GEX التراكمي
    lo, hi = spot * (1 - NEAR_PCT), spot * (1 + NEAR_PCT)
    near = by_strike[(by_strike.index >= lo) & (by_strike.index <= hi)]
    flip = None
    if not near.empty:
        cum = near.cumsum()
        keys = list(cum.index)
        crossings = []
        for j in range(1, len(keys)):
            if (cum[keys[j - 1]] < 0) != (cum[keys[j]] < 0):
                # النقطة الأقرب للسعر ضمن هذا التقاطع
                k = keys[j] if abs(keys[j] - spot) < abs(keys[j - 1] - spot) else keys[j - 1]
                crossings.append(k)
        if crossings:
            flip = min(crossings, key=lambda k: abs(k - spot))  # الأقرب للسعر
        else:
            flip = near.abs().idxmin()
    else:
        flip = by_strike.abs().idxmin()

    return {
        "spot": spot, "expiry": used_expiry,
        "net": net, "call_gex": call_gex, "put_gex": put_gex,
        "call_wall": call_wall, "put_wall": put_wall, "flip": flip,
    }


def b(x):
    return f"${x * SPX_MULT / 1e9:,.2f}B"   # القيم بمقياس SPX


def report(ticker):
    d = compute_gex(ticker)
    stamp = datetime.now(RIYADH).strftime("%d %b %Y · %I:%M %p KSA")
    if d is None:
        send_telegram(f"📈 <b>GEX {ticker}</b>\nلا بيانات كافية.\n{stamp}")
        return
    m = SPX_MULT if ticker == "SPY" else 1
    msg = (
        f"📈 <b>Gamma Exposure (تقريبي)</b>\n"
        f"🏷 Ticker: {ticker}  (≈SPX ×{m})\n"
        f"📅 Expiry: {d['expiry']}\n"
        f"💰 Spot: {d['spot']:.2f}  (≈SPX {d['spot'] * m:.0f})\n\n"
        f"<b>السيولة:</b>\n"
        f"🟢 Net GEX  {b(d['net'])}\n"
        f"🟢 Call GEX {b(d['call_gex'])}\n"
        f"🔴 Put GEX  {b(d['put_gex'])}\n\n"
        f"<b>الجدران:</b>\n"
        f"🟢 Call Wall {d['call_wall']:.0f}  (≈{d['call_wall'] * m:.0f})\n"
        f"🔴 Put Wall  {d['put_wall']:.0f}  (≈{d['put_wall'] * m:.0f})\n\n"
        f"<b>الفاصل:</b>\n"
        f"⚖️ Gamma Flip {d['flip']:.0f}  (≈{d['flip'] * m:.0f})\n\n"
        f"🕐 {stamp}\n"
        f"⚠️ تقريبي على SPY — ليس بديلاً عن مصدر دقيق."
    )
    send_telegram(msg)


if __name__ == "__main__":
    report(TICKER)
