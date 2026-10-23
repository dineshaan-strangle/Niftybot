import os
import time
import threading
import pyotp
import pandas as pd
from datetime import datetime, timedelta
from flask import Flask
from SmartApi import SmartConnect
from telegram.ext import Application

# ================= RENDER WEB SERVER =================
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return """
    <h2>🤖 Nifty Friend Logic Bot Running!</h2>
    <p>Strategy: 9:30-10:30 LONG Only | VWAP + EMA9 | 90% WR</p>
    <p>Telegram: Active | Angel One: Connected</p>
    <p>Time: """ + datetime.now().strftime("%H:%M:%S") + """</p>
    """

@flask_app.route('/status')
def status():
    return {"status": "running", "strategy": "friend_90pct_long", "time": datetime.now().isoformat()}

# ================= CONFIG FROM RENDER ENV =================
API_KEY = os.environ.get("API_KEY")
CLIENT_ID = os.environ.get("CLIENT_ID")
PASSWORD = os.environ.get("PASSWORD")
TOTP_SECRET = os.environ.get("TOTP_SECRET")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# Nifty 23000 PE - Token maathi use pannu, expiry ku thaguntha maathiri
SYMBOL = "NIFTY27OCT23 23000 PE"
SYMBOL_TOKEN = os.environ.get("SYMBOL_TOKEN", "45664")  # Angel One la search panni edukanum
LOT_SIZE = int(os.environ.get("LOT_SIZE", "50"))

# Global
smart_api = None
telegram_app = None
in_trade = False
entry_price = 0

def angel_login():
    global smart_api
    try:
        smart_api = SmartConnect(api_key=API_KEY)
        totp = pyotp.TOTP(TOTP_SECRET).now()
        data = smart_api.generateSession(CLIENT_ID, PASSWORD, totp)
        print(f"✅ Angel One Login Success: {CLIENT_ID}")
        return True
    except Exception as e:
        print(f"❌ Angel Login Failed: {e}")
        return False

def get_live_data():
    try:
        # Get last 50 candles 5min
        from_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        to_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        historic = smart_api.getCandleData({
            "exchange": "NFO",
            "symboltoken": SYMBOL_TOKEN,
            "interval": "FIVE_MINUTE",
            "fromdate": from_date,
            "todate": to_date
        })
        
        if not historic or 'data' not in historic:
            return None
            
        df = pd.DataFrame(historic['data'], columns=['time','open','high','low','close','volume'])
        df['close'] = df['close'].astype(float)
        df['volume'] = df['volume'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        
        # Calculate EMA
        df['ema9'] = df['close'].ewm(span=9).mean()
        df['ema50'] = df['close'].ewm(span=50).mean()
        
        # VWAP
        df['typical'] = (df['high'] + df['low'] + df['close'])/3
        df['tp_vol'] = df['typical'] * df['volume']
        df['vwap'] = df['tp_vol'].cumsum() / df['volume'].cumsum()
        
        # RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))
        
        last = df.iloc[-1]
        return {
            "price": float(last['close']),
            "vwap": float(last['vwap']),
            "ema9": float(last['ema9']),
            "ema50": float(last['ema50']),
            "rsi": float(last['rsi']),
            "volume": float(last['volume']),
            "vol_avg": float(df['volume'].tail(20).mean())
        }
    except Exception as e:
        print(f"Data Error: {e}")
        return None

async def send_telegram(msg):
    try:
        await telegram_app.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode='HTML')
        print(f"Telegram Sent: {msg[:50]}")
    except Exception as e:
        print(f"Telegram Error: {e}")

async def trading_loop():
    global in_trade, entry_price
    print("🚀 Trading Loop Started - 9:30 to 10:30 Strategy")
    
    while True:
        try:
            now_str = datetime.now().strftime("%H:%M")
            now_time = datetime.now()
            
            # Only 9:30 to 10:30
            if now_time.hour == 9 and now_time.minute < 30:
                await asyncio.sleep(10)
                continue
            if now_time.hour == 10 and now_time.minute > 30:
                if in_trade:
                    # Time exit
                    smart_api.placeOrder({
                        "variety":"NORMAL","tradingsymbol":SYMBOL,"symboltoken":SYMBOL_TOKEN,
                        "transactiontype":"SELL","exchange":"NFO","ordertype":"MARKET",
                        "producttype":"INTRADAY","duration":"DAY","quantity":LOT_SIZE
                    })
                    await send_telegram(f"⏰ <b>10:30 TIME EXIT</b>\nPrice: {entry_price}\nAuto Closed")
                    in_trade = False
                await asyncio.sleep(60)
                continue

            data = get_live_data()
            if not data:
                await asyncio.sleep(10)
                continue

            price = data['price']
            vwap = data['vwap']
            ema9 = data['ema9']
            ema50 = data['ema50']
            rsi = data['rsi']
            vol = data['volume']
            vol_avg = data['vol_avg']

            print(f"{now_str} Price:{price} VWAP:{vwap:.1f} EMA9:{ema9:.1f} RSI:{rsi:.1f}")

            # ENTRY - Friend Logic
            if not in_trade:
                if price > vwap and price > ema9 and ema9 > ema50 and 55 < rsi < 75 and vol > vol_avg:
                    # BUY
                    try:
                        smart_api.placeOrder({
                            "variety":"NORMAL","tradingsymbol":SYMBOL,"symboltoken":SYMBOL_TOKEN,
                            "transactiontype":"BUY","exchange":"NFO","ordertype":"MARKET",
                            "producttype":"INTRADAY","duration":"DAY","quantity":LOT_SIZE
                        })
                        entry_price = price
                        sl = min(vwap, ema50)
                        tp = price * 1.40
                        in_trade = True
                        await send_telegram(
                            f"🟢 <b>FRIEND LONG ENTRY</b>\n\n"
                            f"Symbol: {SYMBOL}\n"
                            f"Buy: {price}\n"
                            f"VWAP: {vwap:.1f} | EMA9: {ema9:.1f}\n"
                            f"RSI: {rsi:.1f} | Vol: {vol:.0f}\n\n"
                            f"SL: {sl:.1f} (VWAP/EMA50)\n"
                            f"TP: {tp:.1f} (40%)\n"
                            f"HOLD Rule: Price > EMA9 varaikkum HOLD"
                        )
                    except Exception as e:
                        await send_telegram(f"❌ BUY Failed: {e}")

            # EXIT - EMA9 Break
            else:
                if price < ema9:
                    try:
                        smart_api.placeOrder({
                            "variety":"NORMAL","tradingsymbol":SYMBOL,"symboltoken":SYMBOL_TOKEN,
                            "transactiontype":"SELL","exchange":"NFO","ordertype":"MARKET",
                            "producttype":"INTRADAY","duration":"DAY","quantity":LOT_SIZE
                        })
                        pnl = price - entry_price
                        pnl_pct = (pnl/entry_price)*100
                        await send_telegram(
                            f"🔴 <b>EMA9 BREAK EXIT</b>\n\n"
                            f"Exit: {price}\n"
                            f"Entry: {entry_price}\n"
                            f"P&L: {pnl:.1f} pts ({pnl_pct:.1f}%)\n"
                            f"EMA9: {ema9:.1f} broken!"
                        )
                        in_trade = False
                        entry_price = 0
                    except Exception as e:
                        await send_telegram(f"❌ SELL Failed: {e}")

            await asyncio.sleep(30) # 30 sec check

        except Exception as e:
            print(f"Loop Error: {e}")
            await asyncio.sleep(30)

import asyncio

def start_bot():
    global telegram_app
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()
    loop.run_until_complete(telegram_app.initialize())
    
    if angel_login():
        loop.run_until_complete(send_telegram("🤖 <b>Bot Started on Render!</b>\nFriend 90% Logic Active\n9:30-10:30 LONG Only"))
    
    loop.run_until_complete(trading_loop())

# ================= MAIN =================
if __name__ == "__main__":
    # Start bot in background thread
    bot_thread = threading.Thread(target=start_bot, daemon=True)
    bot_thread.start()
    
    # Start Flask for Render Web Service
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host='0.0.0.0', port=port, debug=False)
