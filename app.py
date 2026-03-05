# app.py
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import pandas as pd
import datetime as dt
import talib
import os
import sys
import json
import threading
import time
from kiteconnect import KiteConnect
from kiteconnect import KiteTicker
from colorama import Fore, Back, Style, init
from dotenv import load_dotenv
from functools import wraps
from datetime import datetime
import secrets
import math

# Load environment variables
load_dotenv()

# Initialize Colorama
init(autoreset=True)

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', secrets.token_hex(32))

# --- Configuration from environment variables ---
API_KEY = os.getenv('KITE_API_KEY', 'your_api_key_here')
API_SECRET = os.getenv('KITE_API_SECRET', 'your_api_secret_here')
REDIRECT_URL = os.getenv('REDIRECT_URL', 'http://127.0.0.1:5000/authenticate')

# Trading Configuration
INDEX = 'BANKNIFTY'
INST_TOKEN = 260105  # BankNifty spot token
QTY = 30
GAP = 100
OTM_LEVEL = 20
EXCH = 'NFO'
STRAT_START_TIME = dt.time(9, 30)
STRAT_END_TIME = dt.time(15, 15)
MARKET_CLOSE_TIME = dt.time(15, 30)
MAX_TRADES_DAILY = 5
CROSS_THRESHOLD = 10.0
LOT_SIZE = 30
MIN_ORDER_GAP = 300
WEBSOCKET_RECONNECT_INTERVAL = 5  # Seconds
MAX_WEBSOCKET_RETRIES = 5

# SL/TP Configuration
SL_PERCENT = 0.10  # 10% stop loss
TP_PERCENT = 0.35  # 35% take profit
TSL_TRIGGER = 0.10  # Trail after 10% profit
TSL_TRAIL = 0.08   # Trail by 8%

TOKEN_FILE = "access_token.txt"

# Global variables for trading state
trading_state = {
    'kite': None,
    'kws': None,
    'kws_connected': False,
    'kws_retries': 0,
    'ltp_data': {INST_TOKEN: 0},
    'subscribed_tokens': set([INST_TOKEN]),
    'status': {},
    'is_order_pending': False,
    'trade_count': 0,
    'last_order_time': None,
    'pending_order_id': None,
    'pending_order_type': None,
    'bot_start_time': None,
    'inst_df': None,
    'current_expiry': None,
    'is_running': False,
    'thread': None,
    'logs': [],
    'last_heartbeat': time.time(),
    'websocket_lock': threading.Lock()
}

# --- Token Validation Function ---
def is_token_valid():
    """Check if stored token is valid"""
    try:
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, 'r') as f:
                token_data = json.load(f)
            
            created_at = datetime.strptime(token_data['created_at'], "%Y-%m-%d %H:%M:%S")
            token_age = datetime.now() - created_at
            
            # Token valid for 1 day
            if token_age.days < 1 and token_data['api_key'] == API_KEY:
                return token_data['access_token']
    except Exception as e:
        print(f"Token validation error: {e}")
    return None

# --- Authentication Required Decorator ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'access_token' not in session:
            return redirect(url_for('index', error='Please login first'))
        return f(*args, **kwargs)
    return decorated_function

# --- Before Request Handler ---
@app.before_request
def check_token_before_request():
    """Check token validity before each request"""
    # Skip static files and authentication routes
    if request.path.startswith('/static') or request.path in ['/authenticate', '/login', '/api/update_credentials', '/api/check_auth']:
        return
    
    # If user is authenticated but token might be expired, check
    if 'access_token' in session:
        stored_token = is_token_valid()
        if not stored_token:
            # Token expired, clear session
            session.clear()
            return redirect(url_for('index', error='Session expired. Please login again.'))

# --- Routes ---
@app.route('/')
def index():
    """Simplified index route"""
    # Check if token exists
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, 'r') as f:
                token_data = json.load(f)
            session['access_token'] = token_data['access_token']
            return redirect(url_for('dashboard'))
        except:
            pass
    
    # Otherwise show login
    return render_template('index.html',
                         api_key_set=bool(API_KEY and API_KEY != 'your_api_key_here'),
                         is_authenticated=False,
                         redirect_url=REDIRECT_URL)

@app.route('/login')
def login():
    """Initiate Kite login"""
    # Check if already have valid token
    stored_token = is_token_valid()
    if stored_token:
        session['access_token'] = stored_token
        session['api_key'] = API_KEY
        return redirect(url_for('dashboard'))
    
    if API_KEY == 'your_api_key_here':
        return redirect(url_for('index', error='Please configure your API credentials first'))
    
    kite = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()
    return render_template('index.html', 
                         action='login',
                         login_url=login_url,
                         api_key_set=True,
                         is_authenticated=False,
                         redirect_url=REDIRECT_URL)

@app.route('/authenticate')
def authenticate():
    """Handle OAuth callback"""
    request_token = request.args.get('request_token')
    
    if not request_token:
        return redirect(url_for('index', error='No request token provided'))
    
    try:
        kite = KiteConnect(api_key=API_KEY)
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        access_token = data["access_token"]
        
        # Store in session
        session['access_token'] = access_token
        session['api_key'] = API_KEY
        
        # Save token to file
        save_token_to_file(access_token)
        
        # Redirect directly to dashboard
        return redirect(url_for('dashboard'))
    except Exception as e:
        return redirect(url_for('index', error=f'Authentication failed: {str(e)}'))

@app.route('/dashboard')
@login_required
def dashboard():
    """Main trading dashboard"""
    return render_template('index.html',
                         action='dashboard',
                         is_authenticated=True,
                         api_key_set=True,
                         config={
                             'index': INDEX,
                             'qty': QTY,
                             'gap': GAP,
                             'max_trades': MAX_TRADES_DAILY,
                             'sl_percent': SL_PERCENT * 100,
                             'tp_percent': TP_PERCENT * 100,
                             'tsl_trigger': TSL_TRIGGER * 100,
                             'tsl_trail': TSL_TRAIL * 100,
                             'lot_size': LOT_SIZE,
                             'cross_threshold': CROSS_THRESHOLD
                         })

@app.route('/api/check_auth')
def check_auth():
    """Check if user is authenticated"""
    if 'access_token' in session:
        # Verify token is still valid
        stored_token = is_token_valid()
        if stored_token:
            return jsonify({'authenticated': True})
    
    return jsonify({'authenticated': False})

@app.route('/api/start_bot', methods=['POST'])
@login_required
def start_bot():
    """Start the trading bot"""
    if trading_state['is_running']:
        return jsonify({'status': 'error', 'message': 'Bot is already running'})
    
    # Initialize Kite with session token
    access_token = session.get('access_token')
    if not access_token:
        return jsonify({'status': 'error', 'message': 'No access token'})
    
    try:
        # Reset some states
        trading_state['kws_retries'] = 0
        trading_state['subscribed_tokens'] = set([INST_TOKEN])
        
        # Start bot in background thread
        trading_state['thread'] = threading.Thread(target=run_trading_bot, args=(access_token,))
        trading_state['thread'].daemon = True
        trading_state['thread'].start()
        trading_state['is_running'] = True
        add_log('Bot started successfully', 'success')
        
        return jsonify({'status': 'success', 'message': 'Bot started successfully'})
    except Exception as e:
        add_log(f'Error starting bot: {str(e)}', 'danger')
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/stop_bot', methods=['POST'])
@login_required
def stop_bot():
    """Stop the trading bot"""
    trading_state['is_running'] = False
    if trading_state['kws']:
        try:
            trading_state['kws'].close()
        except:
            pass
    
    add_log('Bot stopped', 'warning')
    return jsonify({'status': 'success', 'message': 'Bot stopped'})

@app.route('/api/bot_status')
@login_required
def bot_status():
    """Get current bot status"""
    # Calculate technical indicators if bot is running
    latest_ta = {'ema_fast': 0, 'ema_slow': 0, 'rsi': 50, 'atr': 0}
    ema_gap = 0

    # Signal strength components
    signal_components = {
        'ema_score': 0,
        'rsi_score': 0,
        'combined_score': 0,
        'direction': 'NEUTRAL'
    }

    if trading_state['is_running'] and trading_state['kite'] and trading_state['kws_connected']:
        try:
            hist = trading_state['kite'].historical_data(
                INST_TOKEN,
                dt.datetime.now() - dt.timedelta(days=3),
                dt.datetime.now(),
                "3minute"
            )
            if hist and len(hist) > 20:
                df = pd.DataFrame(hist)
                df['ema_fast'] = talib.EMA(df['close'], 9)
                df['ema_slow'] = talib.EMA(df['close'], 21)
                df['atr'] = talib.ATR(df['high'], df['low'], df['close'], 14)
                df['rsi'] = talib.RSI(df['close'], 14)
                latest = df.iloc[-1]
                latest_ta = {
                    'ema_fast': latest['ema_fast'] if not math.isnan(latest['ema_fast']) else 0,
                    'ema_slow': latest['ema_slow'] if not math.isnan(latest['ema_slow']) else 0,
                    'rsi': latest['rsi'] if not math.isnan(latest['rsi']) else 50,
                    'atr': latest['atr'] if not math.isnan(latest['atr']) else 0
                }
                ema_gap = latest_ta['ema_fast'] - latest_ta['ema_slow']

                # Calculate combined signal strength
                # EMA Score (0-50% based on gap strength)
                abs_gap = abs(ema_gap)
                if abs_gap >= CROSS_THRESHOLD:
                    ema_score = min(50, 50 * (abs_gap / (CROSS_THRESHOLD * 2)))  # Max 50% at 2x threshold
                else:
                    ema_score = 25 * (abs_gap / CROSS_THRESHOLD)  # Up to 25% below threshold

                # RSI Score (0-50% based on how far in the right zone)
                rsi = latest_ta['rsi']
                if ema_gap > 0:  # Looking for CE (RSI > 50)
                    if rsi > 50:
                        rsi_score = min(50, 25 + (rsi - 50))  # 25-50% based on RSI strength
                    else:
                        rsi_score = max(0, 25 * (rsi / 50))  # 0-25% when below 50
                elif ema_gap < 0:  # Looking for PE (RSI < 50)
                    if rsi < 50:
                        rsi_score = min(50, 25 + (50 - rsi))  # 25-50% based on RSI weakness
                    else:
                        rsi_score = max(0, 25 * ((100 - rsi) / 50))  # 0-25% when above 50
                else:
                    rsi_score = 25  # Neutral

                combined_score = ema_score + rsi_score

                # Determine direction
                if combined_score >= 70 and ema_gap > 0 and rsi > 50:
                    direction = 'STRONG_CE'
                elif combined_score >= 70 and ema_gap < 0 and rsi < 50:
                    direction = 'STRONG_PE'
                elif combined_score >= 50:
                    direction = 'CE' if ema_gap > 0 else 'PE' if ema_gap < 0 else 'NEUTRAL'
                else:
                    direction = 'NEUTRAL'

                signal_components = {
                    'ema_score': round(ema_score, 1),
                    'rsi_score': round(rsi_score, 1),
                    'combined_score': round(combined_score, 1),
                    'direction': direction,
                    'ema_gap': round(ema_gap, 2),
                    'rsi': round(rsi, 1)
                }

        except Exception as e:
            add_log(f'Error calculating indicators: {e}', 'warning')

    # Rest of your existing code for position P&L, etc...
    # Calculate position P&L if exists
    position_pnl = 0
    position_pnl_percent = 0
    opt_ltp = 0

    if trading_state['status'] and trading_state['status'].get('buy_price', 0) > 0:
        opt_token = trading_state['status'].get('opt_token', 0)
        if opt_token in trading_state['ltp_data']:
            opt_ltp = trading_state['ltp_data'][opt_token]
            if opt_ltp > 0:
                position_pnl = (opt_ltp - trading_state['status']['buy_price']) * QTY
                position_pnl_percent = ((opt_ltp - trading_state['status']['buy_price']) / trading_state['status']['buy_price']) * 100

    status_data = {
        'is_running': trading_state['is_running'],
        'kws_connected': trading_state['kws_connected'],
        'trade_count': trading_state['trade_count'],
        'max_trades': MAX_TRADES_DAILY,
        'index_ltp': trading_state['ltp_data'].get(INST_TOKEN, 0),
        'position': trading_state['status'],
        'opt_ltp': opt_ltp,
        'position_pnl': position_pnl,
        'position_pnl_percent': position_pnl_percent,
        'is_order_pending': trading_state['is_order_pending'],
        'pending_order_id': trading_state['pending_order_id'],
        'pending_order_type': trading_state['pending_order_type'],
        'bot_start_time': trading_state['bot_start_time'].strftime('%H:%M:%S') if trading_state['bot_start_time'] else None,
        'logs': trading_state['logs'][-20:],  # Last 20 logs
        'technical': latest_ta,
        'ema_gap': ema_gap,
        'signal_components': signal_components,  # Add signal components
        'cross_threshold': CROSS_THRESHOLD,
        'sl_percent': SL_PERCENT * 100,
        'tp_percent': TP_PERCENT * 100,
        'tsl_trigger': TSL_TRIGGER * 100,
        'tsl_trail': TSL_TRAIL * 100
    }
    return jsonify(status_data)

@app.route('/api/place_exit_order', methods=['POST'])
@login_required
def place_exit_order():
    """Manually place exit order"""
    if not trading_state['status']:
        return jsonify({'status': 'error', 'message': 'No active position'})
    
    try:
        order_id = place_market_order(trading_state['status']['sym'], 'SELL')
        if order_id:
            add_log(f'Manual exit order placed: {order_id}', 'warning')
            return jsonify({'status': 'success', 'message': f'Exit order placed: {order_id}'})
        else:
            return jsonify({'status': 'error', 'message': 'Failed to place order'})
    except Exception as e:
        add_log(f'Error placing exit order: {str(e)}', 'danger')
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/api/update_credentials', methods=['POST'])
def update_credentials():
    """Update API credentials"""
    global API_KEY, API_SECRET
    
    api_key = request.json.get('api_key')
    api_secret = request.json.get('api_secret')
    
    if not api_key or not api_secret:
        return jsonify({'status': 'error', 'message': 'API key and secret required'})
    
    try:
        # Update environment variables
        API_KEY = api_key
        API_SECRET = api_secret
        
        # Save to .env file
        with open('.env', 'w') as f:
            f.write(f'KITE_API_KEY={api_key}\n')
            f.write(f'KITE_API_SECRET={api_secret}\n')
            f.write(f'REDIRECT_URL={REDIRECT_URL}\n')
            f.write(f'FLASK_SECRET_KEY={app.secret_key}\n')
        
        return jsonify({'status': 'success', 'message': 'Credentials updated successfully'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/logout')
def logout():
    """Logout and clear session"""
    session.clear()
    if trading_state['is_running']:
        trading_state['is_running'] = False
    
    # Optionally delete token file
    if os.path.exists(TOKEN_FILE):
        try:
            os.remove(TOKEN_FILE)
        except:
            pass
    
    return redirect(url_for('index', success='Logged out successfully'))

# --- Helper Functions ---
def add_log(message, type='info'):
    """Add log message"""
    timestamp = datetime.now().strftime('%H:%M:%S')
    log_entry = {
        'time': timestamp,
        'message': message,
        'type': type
    }
    trading_state['logs'].append(log_entry)
    # Keep only last 200 logs
    if len(trading_state['logs']) > 200:
        trading_state['logs'] = trading_state['logs'][-200:]
    
    # Also print to console for debugging
    color = {
        'success': Fore.GREEN,
        'danger': Fore.RED,
        'warning': Fore.YELLOW,
        'info': Fore.CYAN
    }.get(type, Fore.WHITE)
    print(f"{color}[{timestamp}] {message}{Style.RESET_ALL}")

def save_token_to_file(access_token):
    """Save access token to file"""
    try:
        token_data = {
            'access_token': access_token,
            'created_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'api_key': API_KEY
        }
        with open(TOKEN_FILE, 'w') as f:
            json.dump(token_data, f, indent=2)
        
        # Also set in session
        session['access_token'] = access_token
        session['api_key'] = API_KEY
        
        add_log('Access token saved successfully', 'success')
        return True
    except Exception as e:
        add_log(f'Failed to save token: {e}', 'danger')
        return False

def on_ticks(ws, ticks):
    """WebSocket tick handler with proper initialization"""
    try:
        with trading_state['websocket_lock']:
            for tick in ticks:
                token = tick['instrument_token']
                ltp = tick['last_price']
                
                # Update LTP data
                trading_state['ltp_data'][token] = ltp
                
                # Handle first tick or reset
                if token == INST_TOKEN:
                    if not trading_state.get('last_index_price'):
                        # First tick, just set the price without logging
                        trading_state['last_index_price'] = ltp
                        add_log(f'Initial index price: {ltp}', 'info')
                    else:
                        # Normal price update with validation
                        last_price = trading_state['last_index_price']
                        if last_price > 0:
                            change = ((ltp - last_price) / last_price) * 100
                            
                            # Sanity check: BankNifty typically moves <5% in normal conditions
                            if abs(change) < 5:
                                if abs(change) > 1:  # Log only significant moves
                                    add_log(f'Index moved {change:+.2f}% to {ltp}', 'info')
                                trading_state['last_index_price'] = ltp
                            else:
                                add_log(f'⚠️ Ignoring {change:+.2f}% move (possible data glitch)', 'warning')
            
            trading_state['last_heartbeat'] = time.time()
            
    except Exception as e:
        add_log(f'Error in on_ticks: {e}', 'danger')

def on_connect(ws, response):
    """WebSocket connection handler"""
    with trading_state['websocket_lock']:
        trading_state['kws_connected'] = True
        trading_state['kws_retries'] = 0
        
        # Subscribe to all required tokens
        tokens_to_subscribe = list(trading_state['subscribed_tokens'])
        if tokens_to_subscribe:
            ws.subscribe(tokens_to_subscribe)
            ws.set_mode(ws.MODE_LTP, tokens_to_subscribe)
            add_log(f'WebSocket connected. Subscribed to {len(tokens_to_subscribe)} tokens', 'success')

def on_close(ws, code, reason):
    """WebSocket close handler"""
    with trading_state['websocket_lock']:
        trading_state['kws_connected'] = False
        add_log(f'WebSocket closed: {reason}', 'warning')
        
        # Attempt to reconnect if bot is still running
        if trading_state['is_running']:
            if trading_state['kws_retries'] < MAX_WEBSOCKET_RETRIES:
                trading_state['kws_retries'] += 1
                add_log(f'Attempting to reconnect WebSocket ({trading_state["kws_retries"]}/{MAX_WEBSOCKET_RETRIES})...', 'warning')
                time.sleep(WEBSOCKET_RECONNECT_INTERVAL)
                try:
                    ws.connect()
                except:
                    pass
            else:
                add_log('Max WebSocket retries reached. Please restart bot.', 'danger')

def on_error(ws, code, reason):
    """WebSocket error handler"""
    add_log(f'WebSocket error: {reason}', 'danger')
    trading_state['kws_connected'] = False

def get_daily_config(kite):
    """Get daily instrument configuration"""
    try:
        all_inst = pd.DataFrame(kite.instruments(EXCH))
        nfo_inst = all_inst[(all_inst['name'] == INDEX) & (all_inst['segment'] == 'NFO-OPT')]
        nfo_inst['expiry'] = pd.to_datetime(nfo_inst['expiry'])
        nearest_expiry = nfo_inst['expiry'].min()
        df = nfo_inst[nfo_inst['expiry'] == nearest_expiry].copy()
        add_log(f'Loaded {len(df)} instruments for expiry {nearest_expiry.date()}', 'success')
        return df, nearest_expiry
    except Exception as e:
        add_log(f'Error getting daily config: {e}', 'danger')
        return None, None

def can_place_order():
    """Check if we can place a new order"""
    if trading_state['is_order_pending'] or trading_state['pending_order_id']:
        return False, "Order already pending"
    
    if trading_state['last_order_time']:
        time_diff = (dt.datetime.now() - trading_state['last_order_time']).total_seconds()
        if time_diff < MIN_ORDER_GAP:
            return False, f"Wait {MIN_ORDER_GAP - time_diff:.1f}s"
    
    return True, "OK"

def place_market_order(symbol, transaction_type, max_retries=2):
    """Place a market order with up to 2 retry attempts"""
    for attempt in range(1, max_retries + 1):
        try:
            # Check if we can place order
            can_place, reason = can_place_order()
            if not can_place:
                add_log(f'Order blocked on attempt {attempt}: {reason}', 'warning')
                if attempt < max_retries:
                    time.sleep(2)  # Wait 2 seconds before retry
                    continue
                return None

            # Place the order
            order_id = trading_state['kite'].place_order(
                variety=trading_state['kite'].VARIETY_REGULAR,
                exchange=EXCH,
                tradingsymbol=symbol,
                transaction_type=transaction_type,
                quantity=QTY,
                product=trading_state['kite'].PRODUCT_NRML,
                order_type=trading_state['kite'].ORDER_TYPE_MARKET
            )

            # Update order tracking
            trading_state['last_order_time'] = dt.datetime.now()
            trading_state['pending_order_id'] = order_id
            trading_state['pending_order_type'] = transaction_type
            trading_state['is_order_pending'] = True

            add_log(f'✅ {transaction_type} order placed on attempt {attempt}: {order_id}', 'success')
            return order_id

        except Exception as e:
            error_msg = str(e)
            add_log(f'❌ Order attempt {attempt} failed: {error_msg}', 'warning')

            if attempt < max_retries:
                add_log(f'Retrying order in 2 seconds... (attempt {attempt + 1}/{max_retries})', 'info')
                time.sleep(2)

                # Reset pending state for retry
                trading_state['is_order_pending'] = False
                trading_state['pending_order_id'] = None
                trading_state['pending_order_type'] = None
            else:
                add_log(f'❌ All {max_retries} order attempts failed for {symbol}', 'danger')
                trading_state['is_order_pending'] = False
                trading_state['pending_order_id'] = None
                trading_state['pending_order_type'] = None

    return None

def check_order_status():
    """Check pending order status"""
    if not trading_state['pending_order_id']:
        return
    
    try:
        orders = trading_state['kite'].order_history(trading_state['pending_order_id'])
        if orders:
            latest_order = orders[-1]
            status_val = latest_order['status']
            
            if status_val == 'COMPLETE':
                if latest_order['transaction_type'] == 'BUY':
                    # Buy order completed
                    fill_price = latest_order['average_price']
                    opt_token = latest_order['instrument_token']
                    
                    # Calculate SL and TP
                    option_sl = round(fill_price * (1 - SL_PERCENT), 2)
                    option_tp = round(fill_price * (1 + TP_PERCENT), 2)
                    
                    # Subscribe to option token
                    with trading_state['websocket_lock']:
                        trading_state['subscribed_tokens'].add(opt_token)
                        if trading_state['kws'] and trading_state['kws_connected']:
                            trading_state['kws'].subscribe([opt_token])
                            trading_state['kws'].set_mode(trading_state['kws'].MODE_LTP, [opt_token])
                    
                    # Update status
                    trading_state['status'] = {
                        'sym': latest_order['tradingsymbol'],
                        'buy_price': fill_price,
                        'opt_token': opt_token,
                        'option_sl': option_sl,
                        'option_tp': option_tp,
                        'peak_price': fill_price,
                        'tsl_activated': False,
                        'entry_time': datetime.now().strftime('%H:%M:%S'),
                        'entry_index': trading_state['ltp_data'].get(INST_TOKEN, 0)
                    }
                    trading_state['trade_count'] += 1
                    
                    add_log(f'✅ Position opened at {fill_price} | SL: {option_sl} | TP: {option_tp}', 'success')
                    
                elif latest_order['transaction_type'] == 'SELL':
                    # Sell order completed - clear position
                    if trading_state['status']:
                        buy_price = trading_state['status']['buy_price']
                        sell_price = latest_order['average_price']
                        pnl = (sell_price - buy_price) * QTY
                        pnl_percent = ((sell_price - buy_price) / buy_price) * 100
                        add_log(f'✅ Position closed | P&L: {pnl:+.2f} ({pnl_percent:+.1f}%)', 'success')
                    
                    # Unsubscribe from option token
                    if trading_state['status'] and 'opt_token' in trading_state['status']:
                        old_token = trading_state['status']['opt_token']
                        with trading_state['websocket_lock']:
                            if old_token in trading_state['subscribed_tokens']:
                                trading_state['subscribed_tokens'].remove(old_token)
                    
                    trading_state['status'] = {}
                
                # Clear pending order
                trading_state['pending_order_id'] = None
                trading_state['pending_order_type'] = None
                trading_state['is_order_pending'] = False
                
            elif status_val in ['REJECTED', 'CANCELLED']:
                add_log(f'Order {status_val}: {trading_state["pending_order_id"]}', 'danger')
                trading_state['pending_order_id'] = None
                trading_state['pending_order_type'] = None
                trading_state['is_order_pending'] = False
                
    except Exception as e:
        add_log(f'Error checking order: {e}', 'danger')

def check_exit_conditions():
    """Check if exit conditions are met"""
    if not trading_state['status'] or trading_state['status'].get('buy_price', 0) == 0:
        return False, "No position"
    
    opt_token = trading_state['status'].get('opt_token', 0)
    if opt_token not in trading_state['ltp_data']:
        return False, "Waiting for option LTP"
    
    opt_ltp = trading_state['ltp_data'][opt_token]
    if opt_ltp <= 0:
        return False, "Invalid option LTP"
    
    buy_price = trading_state['status']['buy_price']
    current_sl = trading_state['status']['option_sl']
    current_tp = trading_state['status']['option_tp']
    
    # Calculate current profit percentage
    profit_percent = ((opt_ltp - buy_price) / buy_price) * 100
    
    # Update peak price for trailing stop (only if price increased)
    if opt_ltp > trading_state['status'].get('peak_price', buy_price):
        old_peak = trading_state['status']['peak_price']
        trading_state['status']['peak_price'] = opt_ltp
        
        # Log peak update for significant moves
        if opt_ltp - old_peak > buy_price * 0.02:  # 2% move
            add_log(f'New peak: {opt_ltp:.2f} ({profit_percent:+.1f}%)', 'info')
        
        # Activate trailing stop if profit exceeds trigger and not already activated
        if not trading_state['status'].get('tsl_activated') and profit_percent >= TSL_TRIGGER * 100:
            trading_state['status']['tsl_activated'] = True
            trail_sl = opt_ltp * (1 - TSL_TRAIL)
            add_log(f'⚡ Trailing stop activated at {profit_percent:.1f}% | Trail SL: {trail_sl:.2f}', 'warning')
    
    # Check take profit (highest priority)
    if opt_ltp >= current_tp:
        return True, f"🎯 Take profit hit at {profit_percent:+.1f}%"
    
    # Check stop loss
    if trading_state['status'].get('tsl_activated'):
        # Trailing stop loss
        trail_sl = trading_state['status']['peak_price'] * (1 - TSL_TRAIL)
        if opt_ltp <= trail_sl:
            return True, f"🔄 Trailing stop hit at {profit_percent:+.1f}% (from peak {trading_state['status']['peak_price']:.2f})"
    else:
        # Regular stop loss
        if opt_ltp <= current_sl:
            return True, f"🛑 Stop loss hit at {profit_percent:+.1f}%"
    
    return False, "Hold"

def is_trading_hours():
    """Check trading hours"""
    now_time = dt.datetime.now().time()
    
    # Check if weekend
    if dt.datetime.now().weekday() >= 5:
        return False, "Weekend"
    
    # Don't trade in last 5 minutes (square-off time)
    if now_time >= dt.time(15, 25):
        return False, "Square-off time"
    
    # Check if within strategy hours
    if STRAT_START_TIME <= now_time <= STRAT_END_TIME:
        return True, "Trading hours"
    
    return False, "Outside trading hours"

def run_trading_bot(access_token):
    """Main trading bot loop"""
    try:
        # Initialize Kite
        trading_state['kite'] = KiteConnect(api_key=API_KEY)
        trading_state['kite'].set_access_token(access_token)
        
        # Get daily config
        trading_state['inst_df'], trading_state['current_expiry'] = get_daily_config(trading_state['kite'])
        if trading_state['inst_df'] is None:
            add_log('Failed to get instrument data', 'danger')
            return
        
        trading_state['bot_start_time'] = dt.datetime.now()
        
        # Initialize WebSocket with proper handlers
        trading_state['kws'] = KiteTicker(API_KEY, access_token)
        trading_state['kws'].on_ticks = on_ticks
        trading_state['kws'].on_connect = on_connect
        trading_state['kws'].on_close = on_close
        trading_state['kws'].on_error = on_error
        
        # Connect WebSocket
        trading_state['kws'].connect(threaded=True)
        
        add_log(f'Bot initialized. Expiry: {trading_state["current_expiry"].date()}', 'success')
        
        # Wait for WebSocket connection
        wait_start = time.time()
        while not trading_state['kws_connected'] and time.time() - wait_start < 10:
            time.sleep(0.5)
        
        if not trading_state['kws_connected']:
            add_log('WebSocket connection timeout', 'danger')
            return
        
        # Main trading loop
        while trading_state['is_running']:
            try:
                # Check WebSocket health
                if time.time() - trading_state.get('last_heartbeat', 0) > 30:
                    add_log('WebSocket heartbeat timeout', 'warning')
                    # Force reconnect
                    if trading_state['kws']:
                        try:
                            trading_state['kws'].close()
                        except:
                            pass
                        time.sleep(2)
                        trading_state['kws'].connect(threaded=True)
                
                # Get index LTP
                index_ltp = trading_state['ltp_data'].get(INST_TOKEN, 0)
                if index_ltp == 0:
                    time.sleep(1)
                    continue
                
                # Check pending order status
                if trading_state['pending_order_id']:
                    check_order_status()
                    time.sleep(1)
                    continue
                
                now_time = dt.datetime.now().time()
                trading_hours, hours_msg = is_trading_hours()
                
                # Square-off at market close
                if now_time >= dt.time(15, 25) and trading_state['status']:
                    add_log('Market square-off time - closing position', 'warning')
                    place_market_order(trading_state['status']['sym'], 'SELL')
                    time.sleep(2)
                    continue
                
                # Skip if outside trading hours or max trades reached
                if not trading_hours or trading_state['trade_count'] >= MAX_TRADES_DAILY:
                    time.sleep(5)
                    continue
                
                # Get technical indicators
                try:
                    hist = trading_state['kite'].historical_data(
                        INST_TOKEN,
                        dt.datetime.now() - dt.timedelta(days=3),
                        dt.datetime.now(),
                        "3minute"
                    )
                    
                    if len(hist) < 20:
                        time.sleep(5)
                        continue
                        
                    df = pd.DataFrame(hist)
                    df['ema_fast'] = talib.EMA(df['close'], 9)
                    df['ema_slow'] = talib.EMA(df['close'], 21)
                    df['rsi'] = talib.RSI(df['close'], 7)
                    
                    latest = df.iloc[-1]
                    
                    # Check for NaN values
                    if pd.isna(latest['ema_fast']) or pd.isna(latest['ema_slow']) or pd.isna(latest['rsi']):
                        time.sleep(5)
                        continue
                    
                    ema_gap = latest['ema_fast'] - latest['ema_slow']
                    
                except Exception as e:
                    add_log(f'Error fetching indicators: {e}', 'warning')
                    time.sleep(5)
                    continue
                
                # ========== EXIT LOGIC ==========
                if trading_state['status']:
                    should_exit, exit_reason = check_exit_conditions()
                    if should_exit:
                        add_log(f'Exit signal: {exit_reason}', 'warning')
                        place_market_order(trading_state['status']['sym'], 'SELL')
                        time.sleep(2)
                        continue
                
                # ========== ENTRY LOGIC ==========
                if not trading_state['status']:
                    entry_condition = False
                    side = None
                    
                    # Check entry conditions
                    if ema_gap > CROSS_THRESHOLD and latest['rsi'] > 50:
                        entry_condition = True
                        side = 'CE'
                        add_log(f'CE signal: Gap={ema_gap:.2f}, RSI={latest["rsi"]:.1f}', 'info')
                    elif ema_gap < -CROSS_THRESHOLD and latest['rsi'] < 50:
                        entry_condition = True
                        side = 'PE'
                        add_log(f'PE signal: Gap={ema_gap:.2f}, RSI={latest["rsi"]:.1f}', 'info')
                    
                        atm = round(index_ltp / GAP) * GAP
                        # Calculate OTM strike based on configured level
                        if side == 'CE':
                            # For CE, OTM means higher strike than ATM
                            otm_strike = atm + (OTM_LEVEL * GAP)
                        else:  # PE
                            # For PE, OTM means lower strike than ATM
                            otm_strike = atm - (OTM_LEVEL * GAP)

                        # Find matching instrument
                        match = trading_state['inst_df'][
                            (trading_state['inst_df']['strike'] == otm_strike) &
                            (trading_state['inst_df']['instrument_type'] == side)
                        ]
                        
                        if not match.empty:
                            sym = match.iloc[0]['tradingsymbol']
                            add_log(f'🎯 Entry signal detected: {sym} at {index_ltp:.2f}', 'success')
                            place_market_order(sym, 'BUY')
                            time.sleep(2)
                            continue
                        else:
                            add_log(f'No instrument found for strike {atm} {side}', 'warning')
                
                time.sleep(1.5)  # Prevent CPU overuse
                
            except Exception as e:
                add_log(f'Error in bot loop: {e}', 'danger')
                time.sleep(5)
                
    except Exception as e:
        add_log(f'Fatal error in trading bot: {e}', 'danger')
    finally:
        if trading_state['kws']:
            try:
                trading_state['kws'].close()
            except:
                pass
        trading_state['is_running'] = False
        trading_state['kws_connected'] = False
        add_log('Bot stopped', 'warning')

# Run the app
if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5000, debug=True)
