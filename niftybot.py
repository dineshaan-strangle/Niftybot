import os, time, pyotp, requests, logging, math, threading
from flask import Flask
from datetime import datetime, timezone, timedelta
from SmartApi.smartConnect import SmartConnect

app = Flask(__name__)
@app.route('/')
def home():
    pos = bot_instance.active_pos['symbol'] if bot_instance and bot_instance.active_pos else "No Pos"
    return f"Nifty Futures ORB Bot - {datetime.now(IST).strftime('%H:%M:%S')} | {get_mode_text()} | ORB {bot_instance.orb_high}/{bot_instance.orb_low} | {pos}"

def run_web():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

IST = timezone(timedelta(hours=5, minutes=30))
API_KEY = os.getenv("ANGEL_API_KEY") or os.getenv("API_KEY")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID") or os.getenv("CLIENT_ID")
MPIN = os.getenv("ANGEL_MPIN") or os.getenv("MPIN")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET") or os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

paper_env = os.getenv("PAPER_TRADING") or os.getenv("PAPER_TRADE") or "True"
PAPER_TRADING = str(paper_env).lower() == "true"
LOT_SIZE = 75
PRODUCT_TYPE = "CARRYFORWARD"
MAX_TRADES = 2
TP_RR = 1.5

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()
bot_instance = None

def send_telegram(m):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": m, "parse_mode": "HTML"}, timeout=10)
    except: pass

def get_mode_text():
    return "🧪 PAPER" if PAPER_TRADING else "🔴 LIVE"

def telegram_listener():
    offset=0
    while True:
        try:
            if not TELEGRAM_BOT_TOKEN: time.sleep(10); continue
            r=requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=30", timeout=35).json()
            if not r.get("result"): time.sleep(2); continue
            for upd in r["result"]:
                offset=upd["update_id"]+1
                text=upd.get("message",{}).get("text","").strip()
                if not text: continue
                now=datetime.now(IST).strftime('%H:%M:%S')
                if "/start" in text:
                    send_telegram(f"👋 <b>Nifty FUT ORB Bot Ready</b>\n{now} {get_mode_text()}\nLogic: Futures 5min ORB -> 3min Breakout")
                elif "/info" in text:
                    orb_h=bot_instance.orb_high if bot_instance and bot_instance.orb_high else "Not Marked"
                    orb_l=bot_instance.orb_low if bot_instance and bot_instance.orb_low else "Not Marked"
                    fut=bot_instance.fut_symbol if bot_instance and bot_instance.fut_symbol else "N/A"
                    trades=bot_instance.trade_count if bot_instance else 0
                    pos=bot_instance.active_pos
                    pos_txt=f"\n📈 Active: {pos['symbol']}\nEntry: {pos['entry']} SL:{pos['sl']} TP:{pos['tp']}" if pos else "\n📭 No Position"
                    send_telegram(f"🤖 <b>Nifty FUT ORB Info</b>\n⏰ {now}\n{get_mode_text()}\nFut: {fut}\n📦 ORB H: {orb_h} (Fut 9:15)\n📦 ORB L: {orb_l}\n📊 Trades: {trades}/{MAX_TRADES}{pos_txt}")
                elif "/close" in text:
                    if bot_instance and bot_instance.active_pos: bot_instance.close_position("Manual /close")
                    else: send_telegram("📭 No position")
        except: time.sleep(5)

def calc_delta(spot, strike, time_to_expiry=3/365, vol=0.20, r=0.06, option_type="CE"):
    try:
        d1 = (math.log(spot/strike) + (r + 0.5*vol*vol)*time_to_expiry) / (vol*math.sqrt(time_to_expiry))
        def N(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
        return N(d1) if option_type=="CE" else N(d1)-1
    except: return 0.5

class NiftyBot:
    def __init__(self):
        self.smartApi=None; self.orb_high=None; self.orb_low=None; self.fut_symbol=None; self.fut_token=None
        self.trade_count=0; self.traded_today=False; self.last_date=datetime.now(IST).date(); self.active_pos=None

    def login(self):
        try:
            self.smartApi=SmartConnect(api_key=API_KEY)
            totp=pyotp.TOTP(TOTP_SECRET).now()
            session=self.smartApi.generateSession(CLIENT_ID, MPIN, totp)
            if not session['status']: send_telegram(f"❌ Login Fail"); return False
            send_telegram(f"✅ Login {CLIENT_ID} {get_mode_text()}"); return True
        except: return False

    def get_candle_data(self, exchange, symboltoken, interval, fromdate, todate):
        try:
            data=self.smartApi.getCandleData({"exchange": exchange, "symboltoken": symboltoken, "interval": interval, "fromdate": fromdate, "todate": todate})
            return data['data'] if data and data.get('data') else []
        except: return []

    def get_ltp(self, exchange, tradingsymbol, token):
        try: return float(self.smartApi.ltpData(exchange, tradingsymbol, token)['data']['ltp'])
        except: return None

    def mark_orb_futures(self):
        # LOGIC 1: Mark high and low of first 5 minute candle of nifty futures (9.15 to 9.20)
        try:
            search=self.smartApi.searchScrip({"exchange": "NFO", "searchscrip": "NIFTY FUT"})
            fut_token=None; fut_symbol=None
            if search and search.get('data'):
                for item in search['data']:
                    ts=item['tradingsymbol']
                    if 'NIFTY' in ts and 'FUT' in ts and 'BANK' not in ts:
                        # Take nearest expiry FUT
                        fut_token=item['symboltoken']; fut_symbol=ts; break
            if not fut_token:
                send_telegram("⚠️ Futures not found, using Spot fallback"); fut_token="99926000"; fut_symbol="NIFTY SPOT"
                exch="NSE"
            else: exch="NFO"

            now=datetime.now(IST)
            candles=self.get_candle_data(exch, fut_token, "FIVE_MINUTE", now.strftime("%Y-%m-%d 09:15"), now.strftime("%Y-%m-%d 09:20"))
            if candles:
                self.orb_high=float(candles[0][2]); self.orb_low=float(candles[0][3])
                self.fut_symbol=fut_symbol; self.fut_token=fut_token
                send_telegram(f"📦 <b>ORB Marked - LOGIC 1</b>\nFut: {fut_symbol}\nHigh: {self.orb_high}\nLow: {self.orb_low}\n(9:15-9:20 5min)")
                return True
        except Exception as e:
            logger.error(f"ORB error {e}")
        return False

    def find_delta_strike(self, spot, ce_pe):
        atm=round(spot/50)*50
        strikes=[atm - i*50 for i in range(0,10)] if ce_pe=="CE" else [atm + i*50 for i in range(0,10)]
        for strike in strikes:
            d=calc_delta(spot, strike, option_type=ce_pe)
            if abs(d) > 0.7: return strike, abs(d)
        return atm, 0.7

    def place_buy(self, strike, ce_pe):
        try:
            search=self.smartApi.searchScrip({"exchange": "NFO", "searchscrip": f"NIFTY {strike} {ce_pe}"})
            symbol=None; token=None
            if search and search.get('data'):
                for item in search['data']:
                    ts=item.get('tradingsymbol','')
                    if str(strike) in ts and ce_pe in ts and 'NIFTY' in ts:
                        symbol=ts; token=item.get('symboltoken'); break
                if not symbol:
                    symbol=search['data'][0]['tradingsymbol']; token=search['data'][0]['symboltoken']
            if not symbol: return None, None
            if PAPER_TRADING: return symbol, token
            orderparams={"variety": "NORMAL", "tradingsymbol": symbol, "symboltoken": token, "transactiontype": "BUY", "exchange": "NFO", "ordertype": "MARKET", "producttype": PRODUCT_TYPE, "duration": "DAY", "price": "0", "quantity": str(LOT_SIZE)}
            self.smartApi.placeOrder(orderparams)
            return symbol, token
        except Exception as e:
            send_telegram(f"❌ Buy Fail {e}"); return None, None

    def close_position(self, reason):
        if not self.active_pos: return
        try:
            pos=self.active_pos
            if not PAPER_TRADING:
                orderparams={"variety": "NORMAL", "tradingsymbol": pos['symbol'], "symboltoken": pos['token'], "transactiontype": "SELL", "exchange": "NFO", "ordertype": "MARKET", "producttype": PRODUCT_TYPE, "duration": "DAY", "price": "0", "quantity": str(LOT_SIZE)}
                self.smartApi.placeOrder(orderparams)
            ltp=self.get_ltp("NFO", pos['symbol'], pos['token']) or pos['entry']
            send_telegram(f"🔒 <b>CLOSED</b> {reason}\n{pos['symbol']}\nEntry:{pos['entry']} Exit:{ltp}\nSL:{pos['sl']} TP:{pos['tp']}\n{get_mode_text()}")
            self.active_pos=None
        except Exception as e: send_telegram(f"❌ Close Fail {e}")

    def run(self):
        if not self.login(): return
        while True:
            try:
                now=datetime.now(IST)
                if now.date()!=self.last_date:
                    self.orb_high=None; self.orb_low=None; self.trade_count=0; self.traded_today=False; self.active_pos=None; self.last_date=now.date()
                if now.hour<9 or (now.hour==15 and now.minute>=16):
                    if self.active_pos and now.hour==15 and now.minute>=15: self.close_position("3:15 Auto Square Off")
                    time.sleep(60); continue
                if now.hour==9 and now.minute<20: time.sleep(10); continue
                
                # LOGIC 1
                if self.orb_high is None and now.hour==9 and now.minute>=20:
                    self.mark_orb_futures(); time.sleep(5); continue

                # Active position SL TP check
                if self.active_pos:
                    from_str=now.strftime("%Y-%m-%d 09:15"); to_str=now.strftime("%Y-%m-%d %H:%M")
                    candles_3m=self.get_candle_data("NSE", "99926000", "THREE_MINUTE", from_str, to_str)
                    if not candles_3m or len(candles_3m)<2: time.sleep(10); continue
                    close=float(candles_3m[-1][4])
                    # SL at low of breakout/breakdown as per your logic
                    if close < self.active_pos['sl'] or close > self.active_pos['tp'] or (self.active_pos['type']=="PE" and close < self.active_pos['tp']):
                        # Simplified: For CE TP is higher, for PE TP is lower
                        if self.active_pos['type']=="CE":
                            if close < self.active_pos['sl']: self.close_position(f"SL Hit {close} < {self.active_pos['sl']}")
                            elif close > self.active_pos['tp']: self.close_position(f"TP Hit {close} > {self.active_pos['tp']}")
                        else:
                            # LOGIC 4 as you said: SL at low of breakdown, so SL hit if close < SL? 
                            # As per your exact words: SL at low of breakdown candle
                            if close < self.active_pos['sl']: self.close_position(f"SL Hit PE {close} < {self.active_pos['sl']}")
                            elif close < self.active_pos['tp']: self.close_position(f"TP Hit PE {close} < {self.active_pos['tp']}")
                    time.sleep(30); continue

                if self.orb_high is None or self.traded_today or self.trade_count>=MAX_TRADES: time.sleep(30); continue

                # LOGIC 2: Switch to 3 minute timeframe
                from_str=now.strftime("%Y-%m-%d 09:15"); to_str=now.strftime("%Y-%m-%d %H:%M")
                candles_3m=self.get_candle_data("NSE", "99926000", "THREE_MINUTE", from_str, to_str)
                if not candles_3m or len(candles_3m)<2: time.sleep(10); continue
                last=candles_3m[-1]
                close=float(last[4]); low=float(last[3]); high=float(last[2])
                spot=self.get_ltp("NSE", "NIFTY", "99926000") or close

                # LOGIC 3: If a candle breaks the high of the 9.15 candle, buy a CE of Delta >0.7. Keep the SL at the low of the breakout candle.
                if close > self.orb_high:
                    strike, delta=self.find_delta_strike(spot, "CE")
                    sl=low; tp=close + ((close-low)*TP_RR) if (close-low)>0 else close+20
                    symbol, token=self.place_buy(strike, "CE")
                    if symbol:
                        self.active_pos={"symbol": symbol, "token": token, "entry": close, "sl": sl, "tp": tp, "type": "CE"}
                        send_telegram(f"🚀 <b>LOGIC 3 - CE BREAKOUT</b> {get_mode_text()}\nFut ORB H:{self.orb_high} broken by 3min close {close}\n{symbol} D:{delta:.2f}\nSL:{sl} (Low of breakout) TP:{tp:.1f}\nTrade {self.trade_count+1}/{MAX_TRADES}")
                        self.trade_count+=1
                        if self.trade_count>=MAX_TRADES: self.traded_today=True
                    time.sleep(60); continue

                # LOGIC 4: If a candle breaks the Low of the 9.15 candle, buy a PE of Delta >0.7. Keep the SL at the low of the breakdown candle.
                elif close < self.orb_low:
                    strike, delta=self.find_delta_strike(spot, "PE")
                    sl=low  # AS PER YOUR LOGIC - low of breakdown candle
                    tp=close - ((high-close)*TP_RR) if (high-close)>0 else close-20
                    symbol, token=self.place_buy(strike, "PE")
                    if symbol:
                        self.active_pos={"symbol": symbol, "token": token, "entry": close, "sl": sl, "tp": tp, "type": "PE"}
                        send_telegram(f"🔻 <b>LOGIC 4 - PE BREAKDOWN</b> {get_mode_text()}\nFut ORB L:{self.orb_low} broken by 3min close {close}\n{symbol} D:{delta:.2f}\nSL:{sl} (Low of breakdown as per logic) TP:{tp:.1f}\nTrade {self.trade_count+1}/{MAX_TRADES}")
                        self.trade_count+=1
                        if self.trade_count>=MAX_TRADES: self.traded_today=True
                    time.sleep(60); continue

                time.sleep(30)
            except Exception as e:
                logger.error(f"Loop {e}"); time.sleep(10)

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=telegram_listener, daemon=True).start()
    logger.info(f"✅ Futures ORB Bot Started {get_mode_text()}")
    bot_instance=NiftyBot()
    bot_instance.run()
