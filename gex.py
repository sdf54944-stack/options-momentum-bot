import os, math
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
R = 0.045
N_EXPIRIES = 1
SPX_MULT = 10
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


def compute_walls(ticker):
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
                rows.append({"strike": float(strike), "gex": gex})

    if not rows:
        return None

    g = pd.DataFrame(rows)
    by_strike = g.groupby("strike")["gex"].sum().sort_index()
    if by_strike.empty:
        return None

    return {
        "spot": spot,
        "expiry": used_expiry,
        "call_wall": by_strike.idxmax(),   # أعلى GEX موجب = جدار المقاومة
        "put_wall": by_strike.idxmin(),    # أدنى GEX = جدار الدعم
    }


def report(ticker):
    d = compute_walls(ticker)
    stamp = datetime.now(RIYADH).strftime("%d %b %Y · %I:%M %p KSA")
    if d is None:
        send_telegram(f"📈 <b>GEX Walls {ticker}</b>\nلا بيانات كافية.\n{stamp}")
        return
    m = SPX_MULT if ticker == "SPY" else 1
    msg = (
        f"📈 <b>Gamma Walls (تقريبي)</b>\n"
        f"🏷 Ticker: {ticker}  (≈SPX ×{m})\n"
        f"📅 Expiry: {d['expiry']}\n"
        f"💰 Spot: {d['spot']:.2f}  (≈SPX {d['spot'] * m:.0f})\n\n"
        f"<b>الجدران:</b>\n"
        f"🟢 Call Wall {d['call_wall']:.0f}  (≈{d['call_wall'] * m:.0f})\n"
        f"🔴 Put Wall  {d['put_wall']:.0f}  (≈{d['put_wall'] * m:.0f})\n\n"
        f"🕐 {stamp}\n"
        f"⚠️ تقريبي على SPY — ليس بديلاً عن مصدر دقيق."
    )
    send_telegram(msg)


if __name__ == "__main__":
    report(TICKER)
