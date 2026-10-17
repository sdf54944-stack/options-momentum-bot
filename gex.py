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
                iv = getattr(r,
