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
MIN_OI = 250
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
    df["score"] = df["vol_oi"]
    return df

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID,
                                 "text": text, "parse_mode": "HTML"})
    if not r.ok:
        print("telegram error:", r.text)
    return r.ok

def build_table(top, new_ids):
    header = f"{'#':<3}{'':<2}{'SYMBOL':<7}{'STRIKE':>7}  {'VOL':>8}{'OI':>7}{'V/OI':>8}{'IV':>5}"
    sep = "-" * len(header)
    lines = [header, sep]
    for i, r in enumerate(top.itertuples(), 1):
        tag = "🆕" if r.contractSymbol in new_ids else "  "
        lines.append(
            f"{i:<3}{tag:<2}{r.ticker:<7}{r.strike:>7g}  "
            f"{int(r.volume):>8}{int(r.openInterest):>7}"
            f"{r.vol_oi:>8.1f}{r.impliedVolatility*100:>4.0f}%"
        )
    return "\n".join(lines)

def scan(side, title, state):
    frames = [f for f in (fetch_contracts(tk, side) for tk in UNIVERSE) if not f.empty]
    if not frames:
        print(f"{side}: no data"); return
    scored = rank_momentum(pd.concat(frames, ignore_index=True))
    if scored.empty:
        print(f"{side}: no qualified contracts"); return
    top = scored.sort_values("score", ascending=False).head(TOP_N)

    # التاريخ موحّد؟ اعرضه في العنوان مرة واحدة
    exp_dates = top["expiry"].unique()
    date_note = f" | Exp {exp_dates[0]}" if len(exp_dates) == 1 else ""

    current_ids = top["contractSymbol"].tolist()
    prev_ids = set(state.get(side, []))
    state[side] = current_ids
    if not prev_ids:
        print(f"{side}: first run - seeding state, no alert"); return
    new_ids = [cid for cid in current_ids if cid not in prev_ids]
    if not new_ids:
        print(f"{side}: no new entries"); return

    table = build_table(top, new_ids)
    msg = (f"<b>{title}{date_note}</b>\n"
           f"New in Top 10: {len(new_ids)}\n"
           f"<pre>{table}</pre>")
    send_telegram(msg)

if __name__ == "__main__":
    if not market_is_open():
        print("market closed - skipping")
        raise SystemExit(0)
    state = load_state()
    ts = et_stamp()
    scan("calls", f"CALL momentum - {ts}", state)
    scan("puts", f"PUT momentum - {ts}", state)
    save_state(state)
