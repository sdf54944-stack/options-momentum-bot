import os, time, json
import yaml
import yfinance as yf
import pandas as pd
import numpy as np
import requests
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

with open("config.yaml", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
FORCE = os.environ.get("FORCE_PHASE", "none").strip().lower()

UNIVERSE = CFG["universe"]
TOP_N = CFG["filters"]["top_n"]
MIN_VOLUME = CFG["filters"]["min_volume"]
MIN_OI = CFG["filters"]["min_open_interest"]
MAX_EXPIRIES = CFG["filters"]["max_expiries"]
RANKING = CFG["ranking"]
WEIGHTS = CFG["composite_weights"]
FEAT = CFG["features"]
NEWS_LOOKBACK = CFG["news"]["lookback_hours"]
NEWS_MAX_PER = CFG["news"]["max_per_ticker"]
NEWS_MAX_TOTAL = CFG["news"]["max_total"]

STATE_FILE = "state/top10.json"
ET = ZoneInfo("America/New_York")
RIYADH = ZoneInfo("Asia/Riyadh")

def _hm(s):
    h, m = map(int, s.split(":"))
    return dtime(h, m)

MKT_OPEN = _hm(CFG["market"]["open"])
MKT_CLOSE = _hm(CFG["market"]["close"])
PREMARKET_TIME = _hm(CFG["schedule"]["premarket_news_time"])
CLOSING_TIME = _hm(CFG["schedule"]["closing_summary_time"])

def now_et():
    return datetime.now(ET)

def market_is_open():
    et = now_et()
    if et.weekday() >= 5:
        return False
    return MKT_OPEN <= et.time() <= MKT_CLOSE

def et_stamp():
    return now_et().strftime("%H:%M ET")

def saudi_of(hour, minute):
    return now_et().replace(hour=hour, minute=minute, second=0, microsecond=0)\
                   .astimezone(RIYADH).strftime("%H:%M")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def fired_today(state, key):
    return state.get(key) == now_et().strftime("%Y-%m-%d")

def mark_fired(state, key):
    state[key] = now_et().strftime("%Y-%m-%d")

def send_telegram(text, preview=True):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if not preview:
        data["disable_web_page_preview"] = "true"
    r = requests.post(url, data=data)
    if not r.ok:
        print("telegram error:", r.text)
    return r.ok

def health_alert_once(state, kind, msg):
    key = f"alert_{kind}_date"
    if fired_today(state, key):
        return
    send_telegram(msg)
    mark_fired(state, key)

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
    else:
        df["score"] = (df["vol_oi"].rank(pct=True) * WEIGHTS["vol_oi"] +
                       df["volume"].rank(pct=True) * WEIGHTS["volume"])
    return df

def build_table(top, new_ids):
    header = f"{'#':<3} {'':<3} {'SYMBOL':<7}{'STRIKE':>7}  {'VOL':>8}{'OI':>7}{'V/OI':>8}{'IV':>5}"
    lines = [header, "-" * len(header)]
    for i, r in enumerate(top.itertuples(), 1):
        tag = "NEW" if r.contractSymbol in new_ids else "   "
        lines.append(
            f"{i:<3} {tag:<3} {r.ticker:<7}{r.strike:>7g}  "
            f"{int(r.volume):>8}{int(r.openInterest):>7}"
            f"{r.vol_oi:>8.1f}{r.impliedVolatility*100:>4.0f}%"
        )
    return "\n".join(lines)

def top_snapshot(side):
    ok = [r for r in (fetch_contracts(tk, side) for tk in UNIVERSE) if not r.empty]
    if not ok:
        return None, ""
    scored = rank_contracts(pd.concat(ok, ignore_index=True))
    if scored.empty:
        return None, ""
    top = scored.sort_values("score", ascending=False).head(TOP_N)
    exps = top["expiry"].unique()
    note = f" | Exp {exps[0]}" if len(exps) == 1 else ""
    return top, note

def fetch_news(ticker, since_dt):
    try:
        items = yf.Ticker(ticker).news or []
    except Exception as e:
        print(f"news skip {ticker}: {e}")
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        c = it.get("content", it)
        title = c.get("title") or it.get("title")
        if not title:
            continue
        ts = None
        raw = c.get("pubDate") or c.get("displayTime")
        if raw:
            try:
                ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except Exception:
                ts = None
        elif it.get("providerPublishTime"):
            ts = datetime.fromtimestamp(it["providerPublishTime"], tz=timezone.utc)
        prov = c.get("provider")
        pub = prov.get("displayName", "") if isinstance(prov, dict) else it.get("publisher", "")
        cu = c.get("canonicalUrl") or c.get("clickThroughUrl")
        link = cu.get("url", "") if isinstance(cu, dict) else it.get("link", "")
        if ts is not None and ts < since_dt:
            continue
        out.append({"title": title, "pub": pub, "link": link})
    return out

def phase_premarket(state, force=False):
    if not force and (not (PREMARKET_TIME <= now_et().time() < dtime(12, 0))
                      or fired_today(state, "premarket_date")):
        return
    since = datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK)
    blocks, total = [], 0
    for tk in UNIVERSE:
        if total >= NEWS_MAX_TOTAL:
            break
        items = fetch_news(tk, since)[:NEWS_MAX_PER]
        if not items:
            continue
        lines = [f"<b>{tk}</b>"]
        for it in items:
            if total >= NEWS_MAX_TOTAL:
                break
            title = it["title"][:90]
            pub = f" — {it['pub']}" if it["pub"] else ""
            lines.append(f"• <a href=\"{it['link']}\">{title}</a>{pub}" if it["link"]
                         else f"• {title}{pub}")
            total += 1
        blocks.append("\n".join(lines))
    date = now_et().strftime("%Y-%m-%d")
    head = (f"📰 <b>ملخص ما قبل الافتتاح — {date}</b>\n"
            f"السوق يفتح {saudi_of(9, 30)} بتوقيت الرياض ✅")
    msg = head + ("\n\n" + "\n\n".join(blocks) if blocks
                  else "\n\nلا عناوين مهمة في آخر الساعات — البوت حيّ.")
    send_telegram(msg, preview=False)
    if not force:
        mark_fired(state, "premarket_date")

def phase_intraday(state, force=False):
    ts = et_stamp()
    oks = []
    for side, label in [("calls", "CALL"), ("puts", "PUT")]:
        top, note = top_snapshot(side)
        if top is None:
            print(f"{side}: no data / no qualified")
            oks.append(False)
            continue
        oks.append(True)
        current = top["contractSymbol"].tolist()
        prev = set(state.get(side, []))
        if not force:
            state[side] = current
        if not prev:
            print(f"{side}: seeding")
            continue
        new_ids = [c for c in current if c not in prev] if not force else current
        if not new_ids:
            print(f"{side}: no new")
            continue
        table = build_table(top, new_ids)
        send_telegram(f"<b>{label} momentum - {ts}{note}</b>\n"
                      f"New in Top 10: {len(new_ids)}\n<pre>{table}</pre>")
    if not any(oks):
        health_alert_once(state, "nodata",
                          f"⚠️ fetch failed for all symbols at {ts}. "
                          f"Yahoo may be blocking cloud IPs — consider Tradier.")

def phase_closing(state, force=False):
    if not force and (now_et().time() < CLOSING_TIME or fired_today(state, "closing_date")):
        return
    date = now_et().strftime("%Y-%m-%d")
    for side, label in [("calls", "CALL"), ("puts", "PUT")]:
        top, note = top_snapshot(side)
        if top is None:
            send_telegram(f"<b>🔔 الملخص الختامي {label} — {date}</b>\nلا بيانات.")
            continue
        table = build_table(top, [])
        send_telegram(f"<b>🔔 الملخص الختامي {label} — {date}{note}</b>\n"
                      f"إغلاق {saudi_of(16, 0)} بتوقيت الرياض\n<pre>{table}</pre>")
    if not force:
        mark_fired(state, "closing_date")

if __name__ == "__main__":
    state = load_state()
    forced = {
        "premarket": lambda: phase_premarket(state, force=True),
        "closing": lambda: phase_closing(state, force=True),
        "intraday": lambda: phase_intraday(state, force=True),
    }
    if FORCE in forced:
        print(f"FORCED TEST: {FORCE} (state not saved)")
        try:
            forced[FORCE]()
        except Exception as e:
            print(f"forced {FORCE} error: {e}")
        raise SystemExit(0)
    for name, fn in [
        ("premarket", lambda: phase_premarket(state) if FEAT["premarket_news"] else None),
        ("intraday", lambda: phase_intraday(state) if FEAT["intraday_alerts"] and market_is_open() else None),
        ("closing", lambda: phase_closing(state) if FEAT["closing_summary"] else None),
    ]:
        try:
            fn()
        except Exception as e:
            print(f"phase {name} error: {e}")
    save_state(state)
