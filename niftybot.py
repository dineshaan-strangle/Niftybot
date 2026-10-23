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

# ========== RENDER PORT FIX - ITHU THAN MAIN FIX ==========
app = Flask(__name__)
@app.route('/')
def home():
    return f"Nifty Bot Running OK - ORB Logic Active - {datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%H:%M:%S')} IST"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# ========== IST FIX ==========
IST = timezone(timedelta(hours=5, minutes=30))

# ========== ENV ==========
API_KEY = os.getenv("ANGEL_API_KEY") or os.getenv("API_KEY")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID") or os.getenv("CLIENT_ID")
MPIN = os.getenv("ANGEL_MPIN") or os.getenv("MPIN")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET") or os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ========== CONFIG ==========
LOT_SIZE = 75
PRODUCT_TYPE = "CARRYFORWARD"
ANGEL_BASE_URL = "https://apiconnect.angelone.in"
MAX_TRADES = 2
TP_RR = 1.5
AUTO_EXIT_TIME = "15:15"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

# ========== TELEGRAM ==========
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")

# ========== DELTA CALC ==========
def calc_delta(spot, strike, time_to_expiry=3/365, vol=0.20, r=0.06, option_type="CE"):
    try:
        d1 = (math.log(spot/strike) + (r + 0.5*vol*vol)*time_to_expiry) / (vol*math.sqrt(time_to_expiry))
        def N(x):
            return 0.5 * (1 + math.erf(x / math.sqrt(2)))
        if option_type == "CE":
            return N(d1)
        else:
            return N(d1) - 1
    except:
        return 0.5

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
            if not API_KEY or not CLIENT_ID or not MPIN or not TOTP_SECRET:
                logger.error("❌ ENV MISSING")
                return False
            self.smartApi = SmartConnect(api_key=API_KEY)
            totp_code = pyotp.TOTP(TOTP_SECRET).now()
            session = self.smartApi.generateSession(CLIENT_ID, MPIN, totp_code)
            if session['status'] == False:
                send_telegram(f"❌ <b>Login Failed</b> {session['message']}")
                return False
            logger.info(f"✅ LOGIN SUCCESS {CLIENT_ID} {datetime.now(IST).strftime('%H:%M:%S')}")
            send_telegram(f"✅ <b>Login Success</b> {CLIENT_ID} {datetime.now(IST).strftime('%H:%M:%S')} IST")
            return True
        except Exception as e:
            logger.error(f"Login Exception: {e}")
            return False

    def get_candle_data(self, exchange, symboltoken, interval, fromdate, todate):
        try:
            params = {"exchange": exchange, "symboltoken": symboltoken, "interval": interval, "fromdate": fromdate, "todate": todate}
            data = self.smartApi.getCandleData(params)
            return data['data'] if data['data'] else []
        except Exception as e:
            logger.error(f"Candle Error {e}")
            return []

    def get_nifty_spot(self):
        try:
            data = self.smartApi.ltpData("NSE", "NIFTY", "99926000")
            return data['data']['ltp']
        except:
            return None

    def mark_orb(self):
        now = datetime.now(IST)
        from_str = now.strftime("%Y-%m-%d 09:15")
        to_str = now.strftime("%Y-%m-%d 09:20")
        candles = self.get_candle_data("NSE", "99926000", "FIVE_MINUTE", from_str, to_str)
        if candles:
            self.orb_high = float(max([c[2] for c in candles]))
            self.orb_low = float(min([c[3] for c in candles]))
            logger.info(f"📦 ORB MARKED H:{self.orb_high} L:{self.orb_low}")
            send_telegram(f"📦 <b>ORB Marked (5-min 9:15)</b>\nHigh: {self.orb_high}\nLow: {self.orb_low}")
            return True
        return False

    def find_delta_strike(self, spot, ce_pe):
        atm = round(spot / 50) * 50
        strikes = [atm - i*50 for i in range(0, 10)] if ce_pe == "CE" else [atm + i*50 for i in range(0, 10)]
        for strike in strikes:
            delta = calc_delta(spot, strike, option_type=ce_pe)
            if abs(delta) > 0.7:
                return strike, abs(delta)
        return atm, 0.7

    def place_order(self, tradingsymbol, symboltoken, buy_sell, qty):
        try:
            orderparams = {
                "variety": "NORMAL", "tradingsymbol": tradingsymbol, "symboltoken": symboltoken,
                "transactiontype": buy_sell, "exchange": "NFO", "ordertype": "MARKET",
                "producttype": PRODUCT_TYPE, "duration": "DAY", "quantity": qty
            }
            order_id = self.smartApi.placeOrder(orderparams)
            send_telegram(f"🚀 <b>Order</b> {buy_sell} {tradingsymbol} ID:{order_id}")
            return order_id
        except Exception as e:
            send_telegram(f"❌ Order Failed {e}")
            return None

    def run(self):
        logger.info("🚀 NIFTY BOT STARTING...")
        if not self.login():
            return

        while True:
            try:
                now = datetime.now(IST)
                
                if now.date() != self.last_date:
                    self.orb_high = None
                    self.orb_low = None
                    self.trade_count = 0
                    self.traded_today = False
                    self.last_date = now.date()

                if now.hour < 9 or (now.hour == 15 and now.minute >= 16):
                    if now.hour == 15 and now.minute >= 15 and not self.traded_today and self.trade_count>0:
                        send_telegram(f"⏰ <b>3:15 Auto Square Off</b> - Day Over")
                        self.traded_today = True
                    time.sleep(60)
                    continue

                if now.hour == 9 and now.minute < 20:
                    time.sleep(10)
                    continue

                if self.orb_high is None and now.hour == 9 and now.minute >= 20:
                    self.mark_orb()
                    time.sleep(5)
                    continue

                if self.orb_high is None:
                    time.sleep(10)
                    continue

                if self.traded_today or self.trade_count >= MAX_TRADES:
                    time.sleep(60)
                    continue

                from_str = now.strftime("%Y-%m-%d 09:15")
                to_str = now.strftime("%Y-%m-%d %H:%M")
                candles_3m = self.get_candle_data("NSE", "99926000", "THREE_MINUTE", from_str, to_str)
                
                if not candles_3m or len(candles_3m) < 2:
                    time.sleep(10)
                    continue

                last_candle = candles_3m[-1]
                close = float(last_candle[4])
                breakout_low = float(last_candle[3])
                breakdown_low = float(last_candle[3])
                breakout_high = float(last_candle[2])

                if close > self.orb_high:
                    spot = close
                    strike, delta = self.find_delta_strike(spot, "CE")
                    risk = close - breakout_low
                    tp = close + (risk * TP_RR)
                    sl = breakout_low
                    msg = f"🚀 <b>CE BREAKOUT (3min)</b>\nSpot: {spot} > ORB H: {self.orb_high}\nBuy CE: {strike} Delta: {delta:.2f}\nSL: {sl} (Low of breakout)\nTP: {tp} (1:1.5 RR)\nTrade: {self.trade_count+1}/{MAX_TRADES}"
                    logger.info(msg)
                    send_telegram(msg)
                    self.trade_count += 1
                    if self.trade_count >= MAX_TRADES:
                        self.traded_today = True
                    time.sleep(60)
                    continue

                elif close < self.orb_low:
                    spot = close
                    strike, delta = self.find_delta_strike(spot, "PE")
                    risk = breakout_high - close
                    tp = close - (risk * TP_RR)
                    sl = breakdown_low
                    msg = f"🔻 <b>PE BREAKDOWN (3min)</b>\nSpot: {spot} < ORB L: {self.orb_low}\nBuy PE: {strike} Delta: {delta:.2f}\nSL: {sl} (Low of breakdown - exact logic)\nTP: {tp} (1:1.5 RR)\nTrade: {self.trade_count+1}/{MAX_TRADES}"
                    logger.info(msg)
                    send_telegram(msg)
                    self.trade_count += 1
                    if self.trade_count >= MAX_TRADES:
                        self.traded_today = True
                    time.sleep(60)
                    continue

                time.sleep(30)

            except Exception as e:
                logger.error(f"Loop Error: {e}")
                time.sleep(10)

if __name__ == "__main__":
    # RENDER FIX
    t = threading.Thread(target=run_web)
    t.daemon = True
    t.start()
    logger.info(f"✅ Web server started on port {os.environ.get('PORT', 10000)} - Render port error fixed")
    bot = NiftyBot()
    bot.run()
