python
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

# ---- بوابة السوق (تعالج التوقيت الصيفي تلقائيًا) ----
def market_is_open():
    et = datetime.now(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return False
    return dtime(9, 30) <= et.time() <= dtime(16, 0)

def et_stamp():
    return datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M ET")

# ---- الحالة ----
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ---- الجلب ----
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
                print(f"تخطّي {ticker}: {e}")
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
        print("خطأ تيليجرام:", r.text)
    return r.ok

def row_line(i, r, is_new):
    tag = "🆕 " if is_new else ""
    return (f"{i}. {tag}<b>{r.ticker}</b> {r.strike:g} {r.expiry} | "
            f"Vol {int(r.volume)} / OI {int(r.openInterest)} "
            f"(V/OI {r.vol_oi:.1f}) | IV {r.impliedVolatility*100:.0f}%")

def scan(side, title, state):
    frames = [f for f in (fetch_contracts(tk, side) for tk in UNIVERSE) if not f.empty]
    if not frames:
        print(f"{side}: لا بيانات."); return
    scored = rank_momentum(pd.concat(frames, ignore_index=True))
    if scored.empty:
        print(f"{side}: لا عقود مؤهلة."); return
    top = scored.sort_values("score", ascending=False).head(TOP_N)

    current_ids = top["contractSymbol"].tolist()
    prev_ids = set(state.get(side, []))
    state[side] = current_ids                      # حدّث الحالة دائمًا

    if not prev_ids:                               # أول تشغيل: ازرع بصمت
        print(f"{side}: أول تشغيل — زرع الحالة بلا تنبيه."); return

    new_ids = [cid for cid in current_ids if cid not in prev_ids]
    if not new_ids:                                # لا جديد → صمت
        print(f"{side}: لا دخول جديد."); return

    lines = [f"<b>{title}</b>", f"دخل التوب 10: {len(new_ids)} عقد جديد\n"]
    for i, r in enumerate(top.itertuples(), 1):
        lines.append(row_line(i, r, r.contractSymbol in new_ids))
    send_telegram("\n".join(lines))

if __name__ == "__main__":
    if not market_is_open():
        print("السوق مغلق — تخطّي."); raise SystemExit(0)
    state = load_state()
    ts = et_stamp()
    scan("calls", f"🔥 كول عليها زخم — {ts}", state)
    scan("puts",  f"🔻 بوت عليها زخم — {ts}", state)
    save_state(state)
.github/workflows/scan.yml الكامل (استبدله)
yaml
name: options-momentum-scan

on:
  schedule:
    - cron: "0,30 13-21 * * 1-5"
  workflow_dispatch:

permissions:
  contents: write            # مطلوب ليعيد البوت حفظ ملف الحالة

jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - name: Run scanner
        env:
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python scanner.py
      - name: Commit state
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add state/top10.json
          if git diff --staged --quiet; then
            echo "لا تغيير في الحالة."
          else
            git commit -m "update state [skip ci]"
            git pull --rebase --autostash origin main
            git push
          fi
