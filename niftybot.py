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

app = Flask(__name__)
@app.route('/')
def home():
    return f"Nifty Bot Live - {datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%H:%M:%S')} IST | Mode: {'PAPER' if PAPER_TRADING else 'LIVE'}"

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

# Support both names
paper_env = os.getenv("PAPER_TRADING") or os.getenv("PAPER_TRADE") or "True"
PAPER_TRADING = str(paper_env).lower() == "true"

LOT_SIZE = 75
PRODUCT_TYPE = "CARRYFORWARD"
MAX_TRADES = 2
TP_RR = 1.5

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()
bot_instance = None

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=10)
    except: pass

def get_mode_text():
    return "🧪 PAPER TRADING - No Real Order" if PAPER_TRADING else "🔴 LIVE TRADING - Real Order"

def telegram_listener():
    offset = 0
    while True:
        try:
            if not TELEGRAM_BOT_TOKEN:
                time.sleep(10); continue
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=30"
            r = requests.get(url, timeout=35).json()
            if not r.get("result"):
                time.sleep(2); continue
            for upd in r["result"]:
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                text = msg.get("text", "").strip()
                if not text: continue
                now = datetime.now(IST).strftime('%H:%M:%S')
                if "/start" in text:
                    send_telegram(f"👋 <b>Nifty Bot Ready!</b>\n\nTime: {now} IST\nClient: {CLIENT_ID}\nMode: {get_mode_text()}\n\n/info - status")
                elif "/info" in text:
                    orb_h = bot_instance.orb_high if bot_instance and bot_instance.orb_high else "Not Marked (9:15)"
                    orb_l = bot_instance.orb_low if bot_instance and bot_instance.orb_low else "Not Marked"
                    trades = bot_instance.trade_count if bot_instance else 0
                    reply = f"🤖 <b>Nifty Bot Info</b>\n⏰ {now} IST\n\n{get_mode_text()}\n\n📦 ORB High: {orb_h}\n📦 ORB Low: {orb_l}\n📊 Trades: {trades}/{MAX_TRADES}\n👤 Client: {CLIENT_ID}\n🟢 Status: Running"
                    send_telegram(reply)
        except Exception as e:
            print(f"Listener error {e}"); time.sleep(5)

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
                send_telegram(f"❌ Login Failed {session['message']}"); return False
            logger.info(f"✅ LOGIN SUCCESS {CLIENT_ID} | {get_mode_text()}")
            send_telegram(f"✅ <b>Login Success</b> {CLIENT_ID} {datetime.now(IST).strftime('%H:%M:%S')}\n{get_mode_text()}")
            return True
        except Exception as e:
            logger.error(f"Login {e}"); return False
    def get_candle_data(self, exchange, symboltoken, interval, fromdate, todate):
        try:
            params = {"exchange": exchange, "symboltoken": symboltoken, "interval": interval, "fromdate": fromdate, "todate": todate}
            data = self.smartApi.getCandleData(params)
            return data['data'] if data and data.get('data') else []
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
            self.orb_high = float(candles[0][2])
            self.orb_low = float(candles[0][3])
            send_telegram(f"📦 <b>ORB Marked</b>\nHigh: {self.orb_high}\nLow: {self.orb_low}\n{get_mode_text()}")
            return True
        return False
    def find_delta_strike(self, spot, ce_pe):
        atm = round(spot / 50) * 50
        strikes = [atm - i*50 for i in range(0, 10)] if ce_pe == "CE" else [atm + i*50 for i in range(0, 10)]
        for strike in strikes:
            delta = calc_delta(spot, strike, option_type=ce_pe)
            if abs(delta) >= 0.65: return strike, abs(delta)
        return atm, 0.65
    
    def place_order(self, strike, ce_pe, spot, sl, tp):
        if PAPER_TRADING:
            return f"PAPER-{strike}-{ce_pe}"
        try:
            # Find token - search
            search = self.smartApi.searchScrip({"exchange": "NFO", "searchscrip": f"NIFTY {strike} {ce_pe}"})
            symbol = None; token = None
            if search and search.get('data'):
                for item in search['data']:
                    ts = item.get('tradingsymbol','')
                    if str(strike) in ts and ce_pe in ts and 'NIFTY' in ts:
                        # Prefer weekly expiry
                        symbol = ts; token = item.get('symboltoken'); break
                if not symbol:
                    symbol = search['data'][0]['tradingsymbol']
                    token = search['data'][0]['symboltoken']
            if not symbol:
                send_telegram(f"❌ Symbol not found {strike} {ce_pe}"); return None
            
            orderparams = {
                "variety": "NORMAL",
                "tradingsymbol": symbol,
                "symboltoken": token,
                "transactiontype": "BUY",
                "exchange": "NFO",
                "ordertype": "MARKET",
                "producttype": PRODUCT_TYPE,
                "duration": "DAY",
                "price": "0",
                "quantity": str(LOT_SIZE)
            }
            orderId = self.smartApi.placeOrder(orderparams)
            send_telegram(f"✅ <b>LIVE ORDER SUCCESS</b>\n{symbol}\nOrderID: {orderId}\nSL: {sl} TP: {tp}")
            return orderId
        except Exception as e:
            logger.error(f"Order fail {e}")
            send_telegram(f"❌ <b>Order Failed</b>\n{strike} {ce_pe}\n{e}")
            return None

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
                logger.info(f"📈 {spot} ORB {self.orb_high}/{self.orb_low} Mode:{'PAPER' if PAPER_TRADING else 'LIVE'}")

                if close > self.orb_high:
                    strike, delta = self.find_delta_strike(spot, "CE")
                    risk = close-low
                    if risk<=0: risk=15
                    sl = low
                    tp = close + (risk*TP_RR)
                    oid = self.place_order(strike, "CE", spot, sl, tp)
                    if oid:
                        send_telegram(f"🚀 <b>CE BREAKOUT</b> {get_mode_text()}\nSpot {spot} > {self.orb_high}\nStrike {strike} CE D:{delta:.2f}\nEntry:{close} SL:{sl} TP:{tp:.1f}\nTrade {self.trade_count+1}/{MAX_TRADES}\nOrder: {oid}")
                        self.trade_count+=1
                        if self.trade_count>=MAX_TRADES: self.traded_today=True
                    time.sleep(120); continue

                elif close < self.orb_low:
                    strike, delta = self.find_delta_strike(spot, "PE")
                    risk = high-close
                    if risk<=0: risk=15
                    sl = high  # BUG FIXED - was low earlier
                    tp = close - (risk*TP_RR)
                    oid = self.place_order(strike, "PE", spot, sl, tp)
                    if oid:
                        send_telegram(f"🔻 <b>PE BREAKDOWN</b> {get_mode_text()}\nSpot {spot} < {self.orb_low}\nStrike {strike} PE D:{delta:.2f}\nEntry:{close} SL:{sl} TP:{tp:.1f}\nTrade {self.trade_count+1}/{MAX_TRADES}\nOrder: {oid}")
                        self.trade_count+=1
                        if self.trade_count>=MAX_TRADES: self.traded_today=True
                    time.sleep(120); continue

                time.sleep(30)
            except Exception as e:
                logger.error(f"Loop {e}"); time.sleep(10)

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=telegram_listener, daemon=True).start()
    logger.info(f"✅ Started | Mode: {get_mode_text()}")
    bot_instance = NiftyBot()
    bot_instance.run()
