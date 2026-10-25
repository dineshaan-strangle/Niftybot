import os, time, pyotp, requests, logging, math, threading
from flask import Flask
from datetime import datetime, timezone, timedelta
from SmartApi.smartConnect import SmartConnect

app = Flask(__name__)
@app.route('/')
def home():
    if bot_instance:
        orb = f"H:{bot_instance.orb_high} L:{bot_instance.orb_low}" if bot_instance.orb_high else "Not Marked"
        fut = bot_instance.fut_symbol or "N/A"
        pos = bot_instance.active_pos['symbol'] if bot_instance.active_pos else "No Pos"
    else:
        orb="Init"; fut="Init"; pos="Init"
    return f"Bot {datetime.now(IST).strftime('%H:%M:%S')} | {get_mode_text()} | {fut} | {orb} | {pos}"

def run_web():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

IST = timezone(timedelta(hours=5, minutes=30))
API_KEY = os.getenv("ANGEL_API_KEY") or os.getenv("API_KEY")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID") or os.getenv("CLIENT_ID")
MPIN = os.getenv("ANGEL_MPIN") or os.getenv("MPIN")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET") or os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
PAPER_TRADING = str(os.getenv("PAPER_TRADING") or "True").lower() == "true"
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

def get_mode_text(): return "🧪 PAPER" if PAPER_TRADING else "🔴 LIVE"

def telegram_listener():
    offset=0
    while True:
        try:
            if not TELEGRAM_BOT_TOKEN: time.sleep(10); continue
            r=requests.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=30", timeout=35).json()
            if not r.get("result"): time.sleep(2); continue
            for upd in r["result"]:
                offset=upd["update_id"]+1
                txt=upd.get("message",{}).get("text","").strip()
                if not txt: continue
                low=txt.lower()
                now=datetime.now(IST).strftime('%H:%M:%S')
                if "/start" in low:
                    send_telegram(f"👋 <b>Bot Ready</b> {now} {get_mode_text()}")
                elif "/info" in low:
                    if bot_instance and bot_instance.orb_high is None: bot_instance.mark_orb_futures()
                    oh=bot_instance.orb_high if bot_instance else "Init"; ol=bot_instance.orb_low if bot_instance else "Init"
                    fut=bot_instance.fut_symbol if bot_instance else "N/A"; tr=bot_instance.trade_count if bot_instance else 0
                    pos=bot_instance.active_pos['symbol'] if bot_instance and bot_instance.active_pos else "No Pos"
                    send_telegram(f"🤖 <b>Info</b> {now}\n{get_mode_text()}\nFut:{fut}\nHigh:{oh} Low:{ol}\nTrades:{tr}/{MAX_TRADES}\nPos:{pos}")
                elif "/close" in low:
                    if bot_instance and bot_instance.active_pos: bot_instance.close_position("Manual /close")
                    else: send_telegram("📭 No position")
                elif "/stop" in low:
                    if bot_instance: bot_instance.traded_today=True; send_telegram(f"🛑 <b>STOPPED</b> No more trades today {now}")
                elif "/reset" in low:
                    if bot_instance: bot_instance.traded_today=False; bot_instance.trade_count=0; bot_instance.active_pos=None; send_telegram(f"🔄 <b>RESET</b> Can trade again {now}")
        except: time.sleep(5)

def calc_delta(spot, strike, time_to_expiry=3/365, vol=0.20, r=0.06, option_type="CE"):
    try:
        d1 = (math.log(spot/strike) + (r + 0.5*vol*vol)*time_to_expiry) / (vol*math.sqrt(time_to_expiry))
        def N(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))
        return N(d1) if option_type=="CE" else N(d1)-1
    except: return 0.5

def search_scrip_safe(api, exchange, keyword):
    try:
        data = api.searchScrip(exchange, keyword)
        if isinstance(data, dict) and data.get('data'): return data
        if isinstance(data, list): return {"data": data}
        return data
    except: return None

class NiftyBot:
    def __init__(self):
        self.smartApi=None; self.orb_high=None; self.orb_low=None; self.fut_symbol=None; self.fut_token=None
        self.trade_count=0; self.traded_today=False; self.last_date=datetime.now(IST).date(); self.active_pos=None

    def login(self):
        try:
            self.smartApi=SmartConnect(api_key=API_KEY); totp=pyotp.TOTP(TOTP_SECRET).now()
            session=self.smartApi.generateSession(CLIENT_ID, MPIN, totp)
            if not session['status']: send_telegram(f"❌ Login Fail {session['message']}"); return False
            send_telegram(f"✅ Login {CLIENT_ID} {get_mode_text()}"); return True
        except Exception as e: logger.error(f"Login {e}"); return False

    def get_candle_data(self, exchange, symboltoken, interval, fromdate, todate):
        try:
            data=self.smartApi.getCandleData({"exchange": exchange, "symboltoken": symboltoken, "interval": interval, "fromdate": fromdate, "todate": todate})
            return data['data'] if data and data.get('data') else []
        except: return []

    def get_ltp(self, exchange, tradingsymbol, token):
        try: return float(self.smartApi.ltpData(exchange, tradingsymbol, token)['data']['ltp'])
        except: return None

    def mark_orb_futures(self):
        try:
            fut_token=None; fut_symbol=None; exch="NFO"
            s=search_scrip_safe(self.smartApi, "NFO", "NIFTY FUT")
            if s and s.get('data'):
                cands=[i for i in s['data'] if 'NIFTY' in i.get('tradingsymbol','') and 'FUT' in i.get('tradingsymbol','') and 'BANK' not in i.get('tradingsymbol','')]
                if cands: cands.sort(key=lambda x: x.get('tradingsymbol','')); fut_token=cands[0]['symboltoken']; fut_symbol=cands[0]['tradingsymbol']
            if not fut_token: fut_token="99926000"; fut_symbol="NIFTY SPOT (Fallback)"; exch="NSE"
            now=datetime.now(IST)
            candles=self.get_candle_data(exch, fut_token, "FIVE_MINUTE", now.strftime("%Y-%m-%d 09:15"), now.strftime("%Y-%m-%d 09:20"))
            if candles: self.orb_high=float(candles[0][2]); self.orb_low=float(candles[0][3]); self.fut_symbol=fut_symbol; self.fut_token=fut_token; send_telegram(f"📦 <b>ORB Marked</b>\n{fut_symbol}\nHigh:{self.orb_high}\nLow:{self.orb_low}"); return True
            else: self.fut_symbol=fut_symbol; self.fut_token=fut_token; return False
        except: return False

    def find_delta_strike(self, spot, ce_pe):
        atm=round(spot/50)*50; strikes=[atm - i*50 for i in range(0,10)] if ce_pe=="CE" else [atm + i*50 for i in range(0,10)]
        for strike in strikes:
            d=calc_delta(spot, strike, option_type=ce_pe)
            if abs(d) > 0.7: return strike, abs(d)
        return atm, 0.7

    def place_buy(self, strike, ce_pe):
        if PAPER_TRADING:
            symbol=f"NIFTY {strike} {ce_pe} (PAPER)"; token="99926000"
            logger.info(f"PAPER BUY {symbol}"); return symbol, token
        try:
            keyword=f"NIFTY {strike}"
            search=search_scrip_safe(self.smartApi, "NFO", keyword)
            if not search or not search.get('data'):
                send_telegram(f"❌ Symbol not found NIFTY {strike} {ce_pe} - Search empty")
                return None, None
            
            valid=[]
            for item in search['data']:
                ts=item.get('tradingsymbol','')
                if str(strike) in ts and ce_pe in ts and 'NIFTY' in ts and 'BANK' not in ts:
                    if 'CE' in ts or 'PE' in ts:
                        valid.append(item)
            
            if not valid:
                send_telegram(f"❌ Symbol not found NIFTY {strike} {ce_pe} - No match")
                return None, None

            valid.sort(key=lambda x: x.get('tradingsymbol',''))
            best = valid[0]
            symbol=best['tradingsymbol']
            token=best['symboltoken']
            
            orderparams={"variety": "NORMAL", "tradingsymbol": symbol, "symboltoken": token, "transactiontype": "BUY", "exchange": "NFO", "ordertype": "MARKET", "producttype": PRODUCT_TYPE, "duration": "DAY", "price": "0", "quantity": str(LOT_SIZE)}
            self.smartApi.placeOrder(orderparams)
            logger.info(f"LIVE BUY {symbol}")
            return symbol, token
        except Exception as e:
            logger.error(f"place_buy {e}")
            send_telegram(f"❌ Order Error {e}")
            return None, None

    def close_position(self, reason):
        if not self.active_pos: return
        try:
            pos=self.active_pos
            if not PAPER_TRADING:
                orderparams={"variety": "NORMAL", "tradingsymbol": pos['symbol'], "symboltoken": pos['token'], "transactiontype": "SELL", "exchange": "NFO", "ordertype": "MARKET", "producttype": PRODUCT_TYPE, "duration": "DAY", "price": "0", "quantity": str(LOT_SIZE)}
                self.smartApi.placeOrder(orderparams)
            send_telegram(f"🔒 <b>CLOSED</b> {reason}\n{pos['symbol']}\nSL:{pos['sl']} TP:{pos['tp']}\n{get_mode_text()}"); self.active_pos=None
        except Exception as e:
            logger.error(f"close {e}")

    def run(self):
        if not self.login(): return
        time.sleep(3)
        if datetime.now(IST).hour>=9: self.mark_orb_futures()
        while True:
            try:
                now=datetime.now(IST)
                if now.date()!=self.last_date: self.orb_high=None; self.orb_low=None; self.trade_count=0; self.traded_today=False; self.active_pos=None; self.last_date=now.date()
                if now.hour<9 or (now.hour==15 and now.minute>=15) or now.hour>15:
                    if self.active_pos and now.hour==15 and now.minute>=15: self.close_position("3:15 Auto Square Off")
                    time.sleep(60); continue
                if now.hour==9 and now.minute<15: time.sleep(10); continue
                if self.orb_high is None and now.hour>=9: self.mark_orb_futures(); time.sleep(10); continue
                
                if self.active_pos:
                    from_str=now.strftime("%Y-%m-%d 09:15"); to_str=now.strftime("%Y-%m-%d %H:%M")
                    candles=self.get_candle_data("NSE", "99926000", "THREE_MINUTE", from_str, to_str)
                    if not candles or len(candles)<2: time.sleep(10); continue
                    close=float(candles[-1][4])
                    if self.active_pos['type']=="CE":
                        if close < self.active_pos['sl']: self.close_position(f"SL Hit {close} < {self.active_pos['sl']}")
                        elif close > self.active_pos['tp']: self.close_position(f"TP Hit {close} > {self.active_pos['tp']}")
                    else:
                        if close < self.active_pos['sl']: self.close_position(f"SL Hit PE {close} < {self.active_pos['sl']}")
                        elif close < self.active_pos['tp']: self.close_position(f"TP Hit PE {close} < {self.active_pos['tp']}")
                    time.sleep(30); continue

                if self.orb_high is None or self.traded_today or self.trade_count>=MAX_TRADES: time.sleep(30); continue
                from_str=now.strftime("%Y-%m-%d 09:15"); to_str=now.strftime("%Y-%m-%d %H:%M")
                candles=self.get_candle_data("NSE", "99926000", "THREE_MINUTE", from_str, to_str)
                if not candles or len(candles)<2: time.sleep(10); continue
                close=float(candles[-1][4]); low=float(candles[-1][3]); high=float(candles[-1][2])
                spot=self.get_ltp("NSE", "NIFTY", "99926000") or close

                if now.hour>=15 and now.minute>=15: time.sleep(60); continue
                
                if close > self.orb_high:
                    strike, delta=self.find_delta_strike(spot, "CE")
                    sl=low
                    tp=close + ((close-sl)*TP_RR) if (close-sl)>0 else close+20
                    symbol, token=self.place_buy(strike, "CE")
                    if symbol:
                        self.active_pos={"symbol": symbol, "token": token, "entry": close, "sl": sl, "tp": tp, "type": "CE"}
                        send_telegram(f"🚀 <b>CE BREAKOUT</b> {get_mode_text()}\nORB H:{self.orb_high} > {close}\n{symbol} D:{delta:.2f}\nSL:{sl} TP:{tp:.1f}\n{self.trade_count+1}/{MAX_TRADES}")
                        self.trade_count+=1
                        if self.trade_count>=MAX_TRADES: self.traded_today=True
                    time.sleep(60); continue
                    
                elif close < self.orb_low:
                    strike, delta=self.find_delta_strike(spot, "PE")
                    sl=low # ORIGINAL AS PER YOUR LOGIC - NOT CHANGED
                    tp=close - ((high-close)*TP_RR) if (high-close)>0 else close-20
                    symbol, token=self.place_buy(strike, "PE")
                    if symbol:
                        self.active_pos={"symbol": symbol, "token": token, "entry": close, "sl": sl, "tp": tp, "type": "PE"}
                        send_telegram(f"🔻 <b>PE BREAKDOWN</b> {get_mode_text()}\nORB L:{self.orb_low} < {close}\n{symbol} D:{delta:.2f}\nSL:{sl} TP:{tp:.1f}\n{self.trade_count+1}/{MAX_TRADES}")
                        self.trade_count+=1
                        if self.trade_count>=MAX_TRADES: self.traded_today=True
                    time.sleep(60); continue
                time.sleep(30)
            except Exception as e: logger.error(f"Loop {e}"); time.sleep(10)

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=telegram_listener, daemon=True).start()
    bot_instance=NiftyBot(); bot_instance.run()
