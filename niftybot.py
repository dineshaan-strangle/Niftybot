import os
import time
import pyotp
import requests
import logging
from datetime import datetime, timezone, timedelta
from SmartApi.smartConnect import SmartConnect

# ========== IST FIX - IDHU MUKKIYAM ==========
IST = timezone(timedelta(hours=5, minutes=30))

# ========== ENV - RENDER LA SET PANNIRUKEENGA (Rendu name um support aagum) ==========
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
ANGEL_TOTP_URL = "https://smartapi.angelone.in/enable-totp"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

# ========== TELEGRAM FUNCTION ==========
def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            print(f"📱 TG Sent: {message[:50]}")
        else:
            print(f"TG Failed: {r.text}")
    except Exception as e:
        print(f"Telegram Error: {e}")

# ========== NIFTY BOT CLASS ==========
class NiftyBot:
    def __init__(self):
        self.smartApi = None
        print(f"🔗 Angel One URL Fixed: {ANGEL_BASE_URL}")
        print(f"🔗 TOTP URL: {ANGEL_TOTP_URL}")

    def login(self):
        try:
            # ENV Check - Entha key missing nu sollum
            if not API_KEY or not CLIENT_ID or not MPIN or not TOTP_SECRET:
                logger.error(f"❌ ENV MISSING: API_KEY={bool(API_KEY)}, CLIENT_ID={bool(CLIENT_ID)}, MPIN={bool(MPIN)}, TOTP={bool(TOTP_SECRET)}")
                send_telegram(f"❌ <b>ENV Missing</b>\nAPI:{bool(API_KEY)} Client:{bool(CLIENT_ID)} MPIN:{bool(MPIN)} TOTP:{bool(TOTP_SECRET)}")
                return False

            self.smartApi = SmartConnect(api_key=API_KEY)
            totp_code = pyotp.TOTP(TOTP_SECRET).now()
            logger.info(f"Login Attempt - TOTP: {totp_code} - IST: {datetime.now(IST).strftime('%H:%M:%S')}")

            session = self.smartApi.generateSession(CLIENT_ID, MPIN, totp_code)

            if session['status'] == False:
                err = session['message']
                logger.error(f"❌ LOGIN FAILED: {err}")
                send_telegram(f"❌ <b>Login Failed</b>\n{err}\nCheck: {ANGEL_TOTP_URL}")
                return False

            jwt = session['data']['jwtToken']
            feed = session['data']['feedToken']
            logger.info(f"✅ LOGIN SUCCESS - {CLIENT_ID}")
            logger.info(f"✅ JWT & Feed OK - Base: {ANGEL_BASE_URL}")

            send_telegram(f"✅ <b>Nifty Bot Login Success</b>\nClient: {CLIENT_ID}\nTime: {datetime.now(IST).strftime('%H:%M:%S')} IST\nURL: {ANGEL_BASE_URL}")

            return True
        except Exception as e:
            logger.error(f"Login Exception: {e}")
            send_telegram(f"❌ Login Exception: {e}")
            return False

    def get_nifty_spot(self):
        try:
            data = self.smartApi.ltpData("NSE", "NIFTY", "99926000")
            ltp = data['data']['ltp']
            return ltp
        except Exception as e:
            print(f"LTP Error: {e}")
            return None

    def get_positions(self):
        try:
            pos = self.smartApi.position()
            return pos['data']
        except:
            return []

    def place_order(self, tradingsymbol, symboltoken, buy_sell, qty):
        try:
            orderparams = {
                "variety": "NORMAL",
                "tradingsymbol": tradingsymbol,
                "symboltoken": symboltoken,
                "transactiontype": buy_sell,
                "exchange": "NFO",
                "ordertype": "MARKET",
                "producttype": PRODUCT_TYPE,
                "duration": "DAY",
                "quantity": qty
            }
            order_id = self.smartApi.placeOrder(orderparams)
            msg = f"🚀 <b>Order Placed</b>\n{buy_sell} {tradingsymbol}\nQty: {qty}\nID: {order_id}"
            logger.info(msg)
            send_telegram(msg)
            return order_id
        except Exception as e:
            logger.error(f"Order Failed: {e}")
            send_telegram(f"❌ Order Failed: {e}")
            return None

    def run(self):
        logger.info("🚀 NIFTY BOT STARTING...")
        send_telegram(f"🚀 <b>Bot Starting... IST: {datetime.now(IST).strftime('%H:%M:%S')}</b>\nFixed URL: {ANGEL_BASE_URL}")

        if not self.login():
            return

        while True:
            try:
                now = datetime.now(IST)  # <--- IST FIX - IDHU THAN MAIN

                # Market Time Check IST
                if now.hour < 9 or (now.hour == 15 and now.minute >= 31):
                    logger.info(f"Market Closed {now.strftime('%H:%M')} IST - Sleeping")
                    time.sleep(60)
                    continue

                if now.hour == 9 and now.minute < 20:
                    logger.info(f"Waiting 9:20 IST... {now.strftime('%H:%M:%S')}")
                    time.sleep(10)
                    continue

                spot = self.get_nifty_spot()
                if not spot:
                    time.sleep(5)
                    continue

                logger.info(f"📈 NIFTY SPOT: {spot} | {now.strftime('%H:%M:%S')} IST")
                
                # ===== YOUR STRATEGY HERE =====
                # Example: If you want to buy CE when spot > 25500
                # if spot > 25500:
                #     self.place_order("NIFTY...CE", "token", "BUY", LOT_SIZE)
                # ==============================

                time.sleep(30)

            except Exception as e:
                logger.error(f"Loop Error: {e}")
                time.sleep(10)

if __name__ == "__main__":
    bot = NiftyBot()
    bot.run()
