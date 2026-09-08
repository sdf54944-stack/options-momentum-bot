import os, time, json
import yfinance as yf
import pandas as pd
import numpy as np
import requests
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TOP_N = 10
MIN_VOLUME = 100
MIN_OI = 50
MAX_EXPIRIES = 4
STATE_FILE = "state/top10.json"

UNIVERSE = ["AAPL","TSLA","NVDA","AMD","META","MSFT","AMZN","GOOGL",
            "SPY","QQQ","NFLX","COIN","PLTR","SMCI","MARA","AVGO"]

def market_is_open():
    et = datetime.now(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return False
    return dtime(9, 30) <= et.time() <= dtime(16, 0)

def et_stamp():
    return datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M ET")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def fetch_contracts(ticker, side="calls", retries=2):
    for attempt in range(retries + 1):
        try:
            t = yf.Ticker(ticker)
            rows = []
            for exp in t.options[:MAX_EXPIRIES]:
                chain = t.option_chain(exp)
                df = (chain.calls if side == "calls" else chain.puts).copy()
                df["ticker"], df["expiry"] = ticker, exp
                rows.append(df)
            time.sleep(0.5)
            return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
        except Exception as e:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
            else:
                print(f"skip {ticker}: {e}")
    return pd.DataFrame()

def rank_momentum(df):
    df = df.copy()
    df["volume"] = df["volume"].fillna(0)
    df["openInterest"] = df["openInterest"].fillna(0)
    df = df[(df["volume"] >= MIN_VOLUME) & (df["openInterest"] >= MIN_OI)]
    if df.empty:
        return df
    df["vol_oi"] = df["volume"] / df["openInterest"].replace(0, np.nan)
    df["score"] = df["vol_oi"].rank(pct=True) * 0.6 + df["volume"].rank(pct=True) * 0.4
    return df

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID,
                                 "text": text, "parse_mode": "HTML"})
    if not r.ok:
        print("telegram error:", r.text)
    return r.ok

def row_line(i, r, is_new):
    tag = "NEW " if is_new else ""
    return (f"{i}. {tag}<b>{r.ticker}</b> {r.strike:g} {r.expiry} | "
            f"Vol {int(r.volume)} / OI {int(r.openInterest)} "
            f"(V/OI {r.vol_oi:.1f}) | IV {r.impliedVolatility*100:.0f}%")

def scan(side, title, state):
    frames = [f for f in (fetch_contracts(tk, side) for tk in UNIVERSE) if not f.empty]
    if not frames:
        print(f"{side}: no data"); return
    scored = rank_momentum(pd.concat(frames, ignore_index=True))
    if scored.empty:
        print(f"{side}: no qualified contracts"); return
    top = scored.sort_values("score", ascending=False).head(TOP_N)

    current_ids = top["contractSymbol"].tolist()
    prev_ids = set(state.get(side, []))
    state[side] = current_ids

    if not prev_ids:
        print(f"{side}: first run - seeding state, no alert"); return

    new_ids = [cid for cid in current_ids if cid not in prev_ids]
    if not new_ids:
        print(f"{side}: no new entries"); return

    lines = [f"<b>{title}</b>", f"New in Top 10: {len(new_ids)}\n"]
    for i, r in enumerate(top.itertuples(), 1):
        lines.append(row_line(i, r, r.contractSymbol in new_ids))
    send_telegram("\n".join(lines))

if __name__ == "__main__":
    if not market_is_open():
        print("market closed - skipping")
        raise SystemExit(0)
    state = load_state()
    ts = et_stamp()
    scan("calls", f"CALL momentum - {ts}", state)
    scan("puts", f"PUT momentum - {ts}", state)
    save_state(state)
