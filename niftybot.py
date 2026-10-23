import os
import time
import pyotp
import requests
import logging
import math
import threading
from flask import Flask
from datetime import datetime, timezone, timedelta
from SmartApi.smartConnect import SmartConnect

# ========== RENDER PORT FIX ==========
app = Flask(__name__)
@app.route('/')
def home():
    return f"Nifty Bot Live - {datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%H:%M:%S')} IST"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

IST = timezone(timedelta(hours=5, minutes=30))
API_KEY = os.getenv("ANGEL_API_KEY") or os.getenv("API_KEY")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID") or os.getenv("CLIENT_ID")
MPIN = os.getenv("ANGEL_MPIN") or os.getenv("MPIN")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET") or os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PAPER_TRADING = True  # ippo True, Monday False
LOT_SIZE = 75
PRODUCT_TYPE = "CARRYFORWARD"
MAX_TRADES = 2
TP_RR = 1.5

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

bot_instance = None  # Global for /info

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=10)
    except: pass

# ========== TELEGRAM LISTENER - /start /info ku reply ==========
def telegram_listener():
    offset = 0
    logger.info("✅ Telegram Listener Started - /info /start working")
    while True:
        try:
            if not TELEGRAM_BOT_TOKEN:
                time.sleep(10)
                continue
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=30"
            r = requests.get(url, timeout=35).json()
            if not r.get("result"):
                time.sleep(2)
                continue
            for upd in r["result"]:
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                text = msg.get("text", "").strip()
                if not text: continue
                
                now = datetime.now(IST).strftime('%H:%M:%S')
                logger.info(f"📩 TG Received: {text}")
                
                if "/start" in text:
                    reply = f"👋 <b>Nifty Bot Ready!</b>\n\nTime: {now} IST\nClient: {CLIENT_ID}\n\nCommands:\n/info - Bot status & ORB\n/start - This menu\n\nBot is Live ✅"
                    send_telegram(reply)
                elif "/info" in text:
                    orb_h = bot_instance.orb_high if bot_instance and bot_instance.orb_high else "Not Marked (Market opens 9:15)"
                    orb_l = bot_instance.orb_low if bot_instance and bot_instance.orb_low else "Not Marked"
                    trades = bot_instance.trade_count if bot_instance else 0
                    reply = f"🤖 <b>Nifty Bot Info</b>\n⏰ {now} IST\n\n📦 ORB High: {orb_h}\n📦 ORB Low: {orb_l}\n📊 Trades: {trades}/{MAX_TRADES}\n👤 Client: {CLIENT_ID}\n🟢 Status: Running\n\nNext ORB at 9:15 AM"
                    send_telegram(reply)
        except Exception as e:
            print(f"Listener error {e}")
            time.sleep(5)

def calc_delta(spot, strike, time_to_expiry=3/365, vol=0.20, r=0.06, option_type="CE"):
    try:
        d1 = (math.log(spot/strike) + (r + 0.5*vol*vol)*time_to_expiry) / (vol*math.sqrt(time_to_expiry))
        def N(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
        return N(d1) if option_type=="CE" else N(d1)-1
    except: return 0.5

class NiftyBot:
    def __init__(self):
        self.smartApi = None
        self.orb_high = None
        self.orb_low = None
        self.trade_count = 0
        self.traded_today = False
        self.last_date = datetime.now(IST).date()
    def login(self):
        try:
            self.smartApi = SmartConnect(api_key=API_KEY)
            totp = pyotp.TOTP(TOTP_SECRET).now()
            session = self.smartApi.generateSession(CLIENT_ID, MPIN, totp)
            if session['status'] == False:
                send_telegram(f"❌ Login Failed {session['message']}")
                return False
            logger.info(f"✅ LOGIN SUCCESS {CLIENT_ID}")
            send_telegram(f"✅ <b>Login Success</b> {CLIENT_ID} {datetime.now(IST).strftime('%H:%M:%S')}")
            return True
        except Exception as e:
            logger.error(f"Login {e}")
            return False
    def get_candle_data(self, exchange, symboltoken, interval, fromdate, todate):
        try:
            params = {"exchange": exchange, "symboltoken": symboltoken, "interval": interval, "fromdate": fromdate, "todate": todate}
            data = self.smartApi.getCandleData(params)
            return data['data'] if data['data'] else []
        except: return []
    def get_nifty_spot(self):
        try:
            data = self.smartApi.ltpData("NSE", "NIFTY", "99926000")
            return data['data']['ltp']
        except: return None
    def mark_orb(self):
        now = datetime.now(IST)
        from_str = now.strftime("%Y-%m-%d 09:15")
        to_str = now.strftime("%Y-%m-%d 09:20")
        candles = self.get_candle_data("NSE", "99926000", "FIVE_MINUTE", from_str, to_str)
        if candles:
            self.orb_high = float(max([c[2] for c in candles]))
            self.orb_low = float(min([c[3] for c in candles]))
            send_telegram(f"📦 <b>ORB Marked (5-min 9:15)</b>\nHigh: {self.orb_high}\nLow: {self.orb_low}\nNow on 3-min TF")
            return True
        return False
    def find_delta_strike(self, spot, ce_pe):
        atm = round(spot / 50) * 50
        strikes = [atm - i*50 for i in range(0, 10)] if ce_pe == "CE" else [atm + i*50 for i in range(0, 10)]
        for strike in strikes:
            delta = calc_delta(spot, strike, option_type=ce_pe)
            if abs(delta) > 0.7: return strike, abs(delta)
        return atm, 0.7
    def run(self):
        if not self.login(): return
        while True:
            try:
                now = datetime.now(IST)
                if now.date() != self.last_date:
                    self.orb_high=None; self.orb_low=None; self.trade_count=0; self.traded_today=False; self.last_date=now.date()
                if now.hour < 9 or (now.hour==15 and now.minute>=16):
                    time.sleep(60); continue
                if now.hour==9 and now.minute<20: time.sleep(10); continue
                if self.orb_high is None and now.hour==9 and now.minute>=20:
                    self.mark_orb(); time.sleep(5); continue
                if self.orb_high is None or self.traded_today or self.trade_count>=MAX_TRADES:
                    time.sleep(60); continue
                from_str = now.strftime("%Y-%m-%d 09:15")
                to_str = now.strftime("%Y-%m-%d %H:%M")
                candles_3m = self.get_candle_data("NSE", "99926000", "THREE_MINUTE", from_str, to_str)
                if not candles_3m or len(candles_3m)<2: time.sleep(10); continue
                last = candles_3m[-1]
                close = float(last[4]); low = float(last[3]); high = float(last[2])
                spot = self.get_nifty_spot() or close
                logger.info(f"📈 SPOT {spot} ORB H:{self.orb_high} L:{self.orb_low} {now.strftime('%H:%M:%S')}")
                if close > self.orb_high:
                    strike, delta = self.find_delta_strike(spot, "CE")
                    risk = close-low; tp = close+(risk*TP_RR)
                    send_telegram(f"🚀 <b>CE BREAKOUT (3min)</b>\nSpot {spot} > {self.orb_high}\nCE {strike} D:{delta:.2f}\nSL:{low} TP:{tp}\nTrade {self.trade_count+1}/{MAX_TRADES}")
                    self.trade_count+=1
                    if self.trade_count>=MAX_TRADES: self.traded_today=True
                    time.sleep(60); continue
                elif close < self.orb_low:
                    strike, delta = self.find_delta_strike(spot, "PE")
                    risk = high-close; tp = close-(risk*TP_RR)
                    send_telegram(f"🔻 <b>PE BREAKDOWN (3min)</b>\nSpot {spot} < {self.orb_low}\nPE {strike} D:{delta:.2f}\nSL:{low} TP:{tp}\nTrade {self.trade_count+1}/{MAX_TRADES}")
                    self.trade_count+=1
                    if self.trade_count>=MAX_TRADES: self.traded_today=True
                    time.sleep(60); continue
                time.sleep(30)
            except Exception as e:
                logger.error(f"Loop {e}"); time.sleep(10)

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=telegram_listener, daemon=True).start()
    logger.info("✅ Web + Telegram Listener Started")
    bot_instance = NiftyBot()
    bot_instance.run()
