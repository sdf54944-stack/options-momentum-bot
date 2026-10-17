import os
import yfinance as yf
import pandas as pd
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
RIYADH = ZoneInfo("Asia/Riyadh")

MARKET = ("مؤشر السوق", "SPY")

# القطاعات والمواضيع: (الاسم، الرمز)
SECTORS = [
    ("التقنية", "XLK"),
    ("المالي", "XLF"),
    ("الرعاية الصحية", "XLV"),
    ("الطاقة", "XLE"),
    ("السلع الكمالية", "XLY"),
    ("السلع الأساسية", "XLP"),
    ("الصناعي", "XLI"),
    ("العقارات", "XLRE"),
    ("المواد الأساسية", "XLB"),
    ("المرافق العامة", "XLU"),
    ("خدمات الاتصالات", "XLC"),
    ("أشباه الموصلات", "SMH"),
    ("الحوسبة السحابية", "SKYY"),
    ("الفضاء", "ARKX"),
    ("تعدين البتكوين", "WGMI"),
    ("الحوسبة الكمية", "QTUM"),
]


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID,
                                 "text": text, "parse_mode": "HTML"})
    if not r.ok:
        print("telegram error:", r.text)
    return r.ok


def pct_change(ticker):
    # تغيّر آخر جلسة مكتملة: إغلاق اليوم مقابل إغلاق الأمس
    try:
        h = yf.Ticker(ticker).history(period="5d")
        closes = h["Close"].dropna()
        if len(closes) < 2:
            return None
        last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
        if prev == 0:
            return None
        return (last - prev) / prev * 100
    except Exception as e:
        print(f"pct error {ticker}: {e}")
        return None


def dot(p):
    return "🟢" if p >= 0 else "🔴"


def report():
    stamp = datetime.now(RIYADH).strftime("%d %b %Y · %I:%M %p KSA")

    mkt_name, mkt_sym = MARKET
    mkt_p = pct_change(mkt_sym)
    mkt_px = None
    try:
        c = yf.Ticker(mkt_sym).history(period="1d")["Close"].dropna()
        mkt_px = float(c.iloc[-1]) if not c.empty else None
    except Exception:
        pass

    # احسب كل القطاعات ثم رتّب تنازليًا
    data = []
    for name, sym in SECTORS:
        p = pct_change(sym)
        if p is not None:
            data.append((name, p))
    data.sort(key=lambda x: x[1], reverse=True)

    if mkt_p is None and not data:
        send_telegram(f"📊 <b>أداء القطاعات</b>\nلا بيانات كافية.\n{stamp}")
        return

    lines = ["📊 <b>أداء القطاعات (آخر جلسة)</b>", ""]
    if mkt_p is not None:
        px_txt = f" {mkt_px:,.2f}" if mkt_px else ""
        lines.append(f"{dot(mkt_p)} <b>{mkt_name}</b>{px_txt}  ({mkt_p:+.2f}%)")
        lines.append("")
    for name, p in data:
        lines.append(f"{dot(p)} {name}  ({p:+.2f}%)")
    lines.append("")
    lines.append(f"🕐 {stamp}")

    send_telegram("\n".join(lines))


if __name__ == "__main__":
    report()
