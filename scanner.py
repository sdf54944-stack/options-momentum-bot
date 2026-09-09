import os, time, json
import yaml
import yfinance as yf
import pandas as pd
import numpy as np
import requests
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

# ===== تحميل الإعدادات =====
with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

UNIVERSE = CFG["universe"]
TOP_N = CFG["filters"]["top_n"]
MIN_VOLUME = CFG["filters"]["min_volume"]
MIN_OI = CFG["filters"]["min_open_interest"]
MAX_EXPIRIES = CFG["filters"]["max_expiries"]
RANKING = CFG["ranking"]
WEIGHTS = CFG["composite_weights"]
STATE_FILE = "state/top10.json"
ET = ZoneInfo("America/New_York")

def _parse_hm(s):
    h, m = map(int, s.split(":"))
    return dtime(h, m)

MKT_OPEN = _parse_hm(CFG["market"]["open"])
MKT_CLOSE = _parse_hm(CFG["market"]["close"])
NO_DATA_RATIO = CFG["health"]["no_data_alert_ratio"]
HEARTBEAT_AFTER = _parse_hm(CFG["health"]["heartbeat_after"])

# ===== أدوات الوقت =====
def now_et():
    return datetime.now(ET)

def market_is_open():
    et = now_et()
    if et.weekday() >= 5:
        return False
    return MKT_OPEN <= et.time() <= MKT_CLOSE

def et_stamp():
    return now_et().strftime("%H:%M ET")

# ===== الحالة =====
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ===== تيليجرام =====
def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID,
                                 "text": text, "parse_mode": "HTML"})
    if not r.ok:
        print("telegram error:", r.text)
    return r.ok

# ===== الجلب =====
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

def rank_contracts(df):
    df = df.copy()
    df["volume"] = df["volume"].fillna(0)
    df["openInterest"] = df["openInterest"].fillna(0)
    df = df[(df["volume"] >= MIN_VOLUME) & (df["openInterest"] >= MIN_OI)]
    if df.empty:
        return df
    df["vol_oi"] = df["volume"] / df["openInterest"].replace(0, np.nan)
    if RANKING == "vol_oi":
        df["score"] = df["vol_oi"]
    elif RANKING == "volume":
        df["score"] = df["volume"]
    else:  # composite
        df["score"] = (df["vol_oi"].rank(pct=True) * WEIGHTS["vol_oi"] +
                       df["volume"].rank(pct=True) * WEIGHTS["volume"])
    return df

# ===== العرض =====
def build_table(top, new_ids):
    header = f"{'#':<3} {'':<3} {'SYMBOL':<7}{'STRIKE':>7}  {'VOL':>8}{'OI':>7}{'V/OI':>8}{'IV':>5}"
    sep = "-" * len(header)
    lines = [header, sep]
    for i, r in enumerate(top.itertuples(), 1):
        tag = "NEW" if r.contractSymbol in new_ids else "   "
        lines.append(
            f"{i:<3} {tag:<3} {r.ticker:<7}{r.strike:>7g}  "
            f"{int(r.volume):>8}{int(r.openInterest):>7}"
            f"{r.vol_oi:>8.1f}{r.impliedVolatility*100:>4.0f}%"
        )
    return "\n".join(lines)

# ===== المسح =====
# يُرجع (نجح_الجلب, أرسل_تنبيه)
def scan(side, title, state):
    results = [fetch_contracts(tk, side) for tk in UNIVERSE]
    ok = [r for r in results if not r.empty]
    fetch_ratio = 1 - (len(ok) / len(UNIVERSE))  # نسبة الفشل

    if not ok:
        print(f"{side}: no data (fetch failed for all)")
        return False, False

    scored = rank_contracts(pd.concat(ok, ignore_index=True))
    if scored.empty:
        print(f"{side}: no qualified contracts")
        return True, False

    top = scored.sort_values("score", ascending=False).head(TOP_N)
    exp_dates = top["expiry"].unique()
    date_note = f" | Exp {exp_dates[0]}" if len(exp_dates) == 1 else ""

    current_ids = top["contractSymbol"].tolist()
    prev_ids = set(state.get(side, []))
    state[side] = current_ids

    if not prev_ids:
        print(f"{side}: first run - seeding state, no alert")
        return True, False

    new_ids = [cid for cid in current_ids if cid not in prev_ids]
    if not new_ids:
        print(f"{side}: no new entries")
        return True, False

    table = build_table(top, new_ids)
    send_telegram(f"<b>{title}{date_note}</b>\nNew in Top 10: {len(new_ids)}\n<pre>{table}</pre>")
    return True, True

# ===== التحصين =====
def maybe_heartbeat(state):
    et = now_et()
    today = et.strftime("%Y-%m-%d")
    # نبض مرة واحدة يوميًا، في أول تشغيل بعد heartbeat_after
    if state.get("heartbeat_date") == today:
        return
    if et.time() >= HEARTBEAT_AFTER:
        send_telegram(f"✅ Bot alive — {et_stamp()} | universe: {len(UNIVERSE)} | rank: {RANKING}")
        state["heartbeat_date"] = today

def health_alert_once(state, kind, msg):
    # يمنع تكرار نفس التنبيه أكثر من مرة يوميًا
    today = now_et().strftime("%Y-%m-%d")
    key = f"alert_{kind}_date"
    if state.get(key) == today:
        return
    send_telegram(msg)
    state[key] = today

# ===== main =====
if __name__ == "__main__":
    if not market_is_open():
        print("market closed - skipping")
        raise SystemExit(0)

    state = load_state()
    maybe_heartbeat(state)
    ts = et_stamp()

    calls_ok, _ = scan("calls", f"CALL momentum - {ts}", state)
    puts_ok, _ = scan("puts", f"PUT momentum - {ts}", state)

    # كشف عطل الجلب: إن فشل كلا الجانبين كليًا → تنبيه صريح
    if not calls_ok and not puts_ok:
        health_alert_once(
            state, "nodata",
            f"⚠️ ALERT: fetch failed for all symbols at {ts}. "
            f"Yahoo may be blocking cloud IPs. Consider switching to Tradier."
        )

    save_state(state)
