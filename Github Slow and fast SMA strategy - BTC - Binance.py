import os
import time
from datetime import datetime, timedelta
from binance.um_futures import UMFutures
import ta
import pandas as pd
from time import sleep
from binance.error import ClientError, ParameterRequiredError
import traceback
from dotenv import load_dotenv

# 1. Load the hidden .env file
load_dotenv()

api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')

client = UMFutures(key=api_key, secret=api_secret)

BOT_INSTANCE_ID = 'BINANCE_BOT_1'
BOT_NAME = os.getenv('BOT_NAME', f'Bot_{BOT_INSTANCE_ID}')

MAX_TRADES_PER_DAY = 3
TRADE_COUNTER_FILE = f"{BOT_NAME}_daily_trades.json"

# Trading parameters
tp_levels = {
    'tp1': {'percentage': 0.02, 'quantity_pct': 0.40},  # 2% → Close 40%
    'tp2': {'percentage': 0.02, 'quantity_pct': 0.40},  # 4% → Close 40%
    'tp3': {'percentage': 0.035, 'quantity_pct': 0.20},  # 6.1% → Close 20%
}

sl_normal = 0.01  # Normal stop loss percentage
balance_percentage = 0.13  # Base percentage of balance to use
leverage = 1
margin_type = 'CROSS'
qty_limit = 1  # Maximum number of simultaneous positions
candle_interval_integreted = 240
candle_interval = '4h'

# Volatility spike detection parameters
vol_threshold = 3  # Multiplier for detecting spikes
pause_duration = 24  # Bars to pause after spike (24 bars = 6 hours on 15m)
vol_lookback = 10  # Period for calculating volatility (48 bars = 12 hours)
use_returns_volatility = True

# Bot state tracking
symbol_states = {}
tracked_positions = {}
volatility_states = {}

# ET market close time (16:00 ET)
ET_MARKET_CLOSE_HOUR = 16
trailing_stops = {}
symbol_states = {}
bot_positions = {}

# Enhanced API retry function with better error handling for order operations
def api_call_with_retry(func, max_retries=3, delay=2, **kwargs):
    """
    Wrapper for API calls with retry logic
    Enhanced for better order cancellation handling
    """
    func_name = getattr(func, '__name__', str(func))

    for attempt in range(max_retries):
        try:
            print(f"🔄 API Call: {func_name} (attempt {attempt + 1}/{max_retries})")
            if kwargs:
                print(f"📊 Parameters: {kwargs}")

            result = func(**kwargs)

            # Log successful calls
            print(f"✅ {func_name} successful")
            if result and isinstance(result, (list, dict)):
                if isinstance(result, list):
                    print(f"📊 Returned {len(result)} items")
                else:
                    print(f"📊 Returned: {type(result).__name__}")

            return result

        except ClientError as error:
            error_code = getattr(error, 'error_code', 'Unknown')
            error_message = getattr(error, 'error_message', str(error))

            print(f"❌ Binance API Error in {func_name} (attempt {attempt + 1}/{max_retries}):")
            print(f"   Code: {error_code}")
            print(f"   Message: {error_message}")

            # Handle specific error codes that shouldn't be retried
            if error_code in [-2011, -1013, -4045]:  # Order not found, invalid quantity, etc.
                print(f"🚫 Non-retryable error code {error_code}, stopping retries")
                # For order cancellation, we might want to know if order doesn't exist
                if 'cancel' in func_name.lower() and error_code == -2011:
                    print(f"ℹ️ Order already cancelled or doesn't exist")
                    return {'msg': 'Order not found', 'cancelled': False}
                return None

            # Rate limiting - longer delay
            if error_code in [-1003, -1015]:  # Too many requests, too frequent
                print(f"⏱️ Rate limiting detected, using longer delay")
                if attempt < max_retries - 1:
                    sleep_time = delay * (attempt + 2) * 2  # Longer delay for rate limits
                    print(f"😴 Sleeping {sleep_time}s due to rate limiting...")
                    sleep(sleep_time)
                continue

            if attempt < max_retries - 1:
                sleep_time = delay * (attempt + 1)
                print(f"😴 Retrying in {sleep_time}s...")
                sleep(sleep_time)
            else:
                print(f"❌ Max retries reached for {func_name}")
                return None

        except ParameterRequiredError as e:
            print(f"❌ Parameter Error in {func_name}: {str(e)}")
            print(f"🔍 Check if you're using the correct API method for your use case")
            # Don't retry parameter errors - they won't succeed
            return None

        except ConnectionError as e:
            print(f"🌐 Connection Error in {func_name} (attempt {attempt + 1}/{max_retries}): {str(e)}")
            if attempt < max_retries - 1:
                sleep_time = delay * (attempt + 1)
                print(f"😴 Retrying connection in {sleep_time}s...")
                sleep(sleep_time)
            else:
                print(f"❌ Connection failed after {max_retries} attempts")
                return None

        except Exception as e:
            print(f"⚠️ Unexpected error in {func_name} (attempt {attempt + 1}/{max_retries}): {str(e)}")
            print(f"🔍 Error type: {type(e).__name__}")

            # Print traceback for debugging
            import traceback
            traceback.print_exc()

            if attempt < max_retries - 1:
                sleep_time = delay
                print(f"😴 Retrying in {sleep_time}s...")
                sleep(sleep_time)
            else:
                print(f"❌ Max retries reached for {func_name} due to unexpected error")
                return None

    return None


def get_balance_usdt():
    """Get USDT futures balance"""
    try:
        response = api_call_with_retry(client.balance, recvWindow=6000)
        if response:
            for elem in response:
                if elem['asset'] == 'BNFCR':
                    balance = float(elem['balance'])
                    base_volume = balance * balance_percentage
                    return balance, base_volume
    except Exception as e:
        print(f"Error getting balance: {e}")
    return None, None


def klines(symbol, interval='15m', limit=500):
    """Get candlestick data"""
    try:
        resp = pd.DataFrame(client.klines(symbol, interval, limit=limit))
        resp = resp.iloc[:, :6]
        resp.columns = ['Time', 'Open', 'High', 'Low', 'Close', 'Volume']
        resp = resp.set_index('Time')
        resp.index = pd.to_datetime(resp.index, unit='ms')
        resp = resp.astype(float)
        return resp
    except Exception as error:
        print(f"Error getting klines for {symbol}: {error}")
        return None


def get_price_precision(symbol):
    """Get price precision for symbol"""
    try:
        resp = client.exchange_info()['symbols']
        for elem in resp:
            if elem['symbol'] == symbol:
                return elem['pricePrecision']
    except Exception as e:
        print(f"Error getting price precision: {e}")
    return 4


def get_qty_precision(symbol):
    """Get quantity precision for symbol"""
    try:
        resp = client.exchange_info()['symbols']
        for elem in resp:
            if elem['symbol'] == symbol:
                return elem['quantityPrecision']
    except Exception as e:
        print(f"Error getting qty precision: {e}")
    return 3


def set_leverage(symbol, level):
    """Set leverage for symbol"""
    try:
        response = api_call_with_retry(client.change_leverage,
                                       symbol=symbol, leverage=level, recvWindow=6000)
        if response:
            print(f"Leverage set to {level}x for {symbol}")
    except Exception as error:
        print(f"Error setting leverage: {error}")


def set_mode(symbol, margin_type):
    """Set margin type for symbol"""
    try:
        response = api_call_with_retry(client.change_margin_type,
                                       symbol=symbol, marginType=margin_type, recvWindow=6000)
        if response:
            print(f"Margin type set to {margin_type} for {symbol}")
    except Exception as error:
        print(f"Error setting margin type: {error}")


def validate_and_format_order_params(symbol, price=None, quantity=None, stop_price=None):
    """Validate and format order parameters"""
    try:
        price_precision = get_price_precision(symbol)
        qty_precision = get_qty_precision(symbol)

        result = {}

        if price is not None:
            result['price'] = f"{price:.{price_precision}f}"
        if quantity is not None:
            result['quantity'] = f"{quantity:.{qty_precision}f}"
        if stop_price is not None:
            result['stop_price'] = f"{stop_price:.{price_precision}f}"

        return result
    except Exception as e:
        print(f"Error validating order params: {e}")
        return None


def update_trailing_stops():
    """
    Update all active trailing stops based on current market prices
    Compatible with hedge mode and bot instance tracking
    """
    try:
        if not trailing_stops:
            return

        print(f"\n🔄 Checking {len(trailing_stops)} trailing stops...")

        for position_key, stop_data in list(trailing_stops.items()):
            symbol = stop_data['symbol']
            side = stop_data['side']
            position_side = stop_data['position_side']
            entry_price = stop_data['entry_price']
            current_sl_price = stop_data['current_sl_price']
            current_sl_order_id = stop_data.get('current_sl_order_id')
            trailing_distance = stop_data.get('trailing_distance', sl_normal)

            # Get current price
            ticker_response = api_call_with_retry(client.ticker_price, symbol=symbol)
            if not ticker_response:
                print(f"⚠️ Could not get current price for {symbol}")
                continue

            current_price = float(ticker_response['price'])

            # Calculate new stop loss price based on trailing logic
            new_sl_price = None
            should_update = False

            if side == 'buy':  # LONG position
                # Track highest price since entry
                if 'highest_price' not in stop_data:
                    stop_data['highest_price'] = entry_price

                if current_price > stop_data['highest_price']:
                    stop_data['highest_price'] = current_price

                # Calculate new trailing stop (distance below highest price)
                potential_sl = stop_data['highest_price'] * (1 - trailing_distance)

                # Only update if new SL is higher than current SL (trailing up)
                if potential_sl > current_sl_price:
                    new_sl_price = potential_sl
                    should_update = True

            else:  # SHORT position (side == 'sell')
                # Track lowest price since entry
                if 'lowest_price' not in stop_data:
                    stop_data['lowest_price'] = entry_price

                if current_price < stop_data['lowest_price']:
                    stop_data['lowest_price'] = current_price

                # Calculate new trailing stop (distance above lowest price)
                potential_sl = stop_data['lowest_price'] * (1 + trailing_distance)

                # Only update if new SL is lower than current SL (trailing down)
                if potential_sl < current_sl_price:
                    new_sl_price = potential_sl
                    should_update = True

            if should_update and new_sl_price:
                print(f"🎯 Updating trailing stop for {symbol} ({position_side})")
                print(f"   Current price: {current_price:.6f}")
                print(f"   Old SL: {current_sl_price:.6f}")
                print(f"   New SL: {new_sl_price:.6f}")

                # Cancel existing stop loss order
                if current_sl_order_id:
                    cancel_params = {
                        'symbol': symbol,
                        'orderId': current_sl_order_id,
                        'recvWindow': 6000
                    }
                    cancel_response = api_call_with_retry(client.cancel_order, **cancel_params)
                    if cancel_response:
                        print(f"✅ Cancelled old SL order {current_sl_order_id}")
                    else:
                        print(f"⚠️ Could not cancel old SL order {current_sl_order_id}")

                    # Small delay to ensure cancellation processes
                    time.sleep(1)

                # Place new trailing stop order
                sl_order_side = 'SELL' if side == 'buy' else 'BUY'

                # Validate new stop price and quantity
                validation = validate_and_format_order_params(
                    symbol,
                    stop_price=new_sl_price,
                    quantity=stop_data['quantity']
                )

                if validation:
                    new_sl_params = {
                        'symbol': symbol,
                        'side': sl_order_side,
                        'positionSide': position_side,
                        'type': 'STOP_MARKET',
                        'quantity': validation['quantity'],
                        'stopPrice': validation['stop_price'],
                        'timeInForce': 'GTC',
                        'recvWindow': 10000,
                        'newClientOrderId': f"{BOT_INSTANCE_ID}_TSL_{position_side}_{int(time.time())}"
                    }

                    new_sl_response = api_call_with_retry(client.new_order, **new_sl_params)

                    if new_sl_response:
                        # Update trailing stop data
                        stop_data['current_sl_price'] = new_sl_price
                        stop_data['current_sl_order_id'] = new_sl_response['orderId']
                        print(f"✅ New trailing stop placed: {new_sl_price:.6f}")
                    else:
                        print(f"❌ Failed to place new trailing stop")
                else:
                    print(f"❌ Failed to validate new stop loss parameters")

    except Exception as e:
        print(f"❌ Error in update_trailing_stops: {str(e)}")
        import traceback
        traceback.print_exc()


def cancel_orders_for_position(symbol, position_side=None):
    """
    Cancel all bot's orders for a specific symbol and position side
    Useful for cleanup after position closes
    """
    try:
        print(f"🚫 Canceling orders for {symbol} {position_side or 'ALL'}")

        # Get all open orders for the symbol
        orders_response = api_call_with_retry(client.get_open_orders, symbol=symbol, recvWindow=10000)

        if not orders_response:
            print(f"ℹ️ No open orders found for {symbol}")
            return

        cancelled_count = 0
        bot_orders_found = 0

        for order in orders_response:
            # Check if order belongs to this bot instance
            client_order_id = order.get('clientOrderId', '')
            order_position_side = order.get('positionSide', 'BOTH')

            # Skip orders that don't belong to this bot
            if BOT_INSTANCE_ID not in client_order_id:
                continue

            bot_orders_found += 1

            # If position_side specified, only cancel orders for that position side
            if position_side and position_side != 'BOTH' and order_position_side != position_side:
                continue

            order_id = order['orderId']
            order_type = order.get('type', 'UNKNOWN')

            print(f"🎯 Canceling {order_type} order {order_id} ({order_position_side})")

            cancel_params = {
                'symbol': symbol,
                'orderId': order_id,
                'recvWindow': 6000
            }

            cancel_response = api_call_with_retry(client.cancel_order, **cancel_params)

            if cancel_response:
                cancelled_count += 1
                print(f"✅ Cancelled order {order_id}")
            else:
                print(f"❌ Failed to cancel order {order_id}")

            # Small delay between cancellations
            time.sleep(0.5)

        print(f"📊 Order cleanup summary for {symbol}:")
        print(f"   Bot orders found: {bot_orders_found}")
        print(f"   Orders cancelled: {cancelled_count}")

    except Exception as e:
        print(f"❌ Error canceling orders for {symbol}: {str(e)}")
        import traceback
        traceback.print_exc()


def cleanup_position_state(symbol, position_side=None):
    """
    Clean up bot's internal state after position closes
    Removes tracking data and trailing stop information
    """
    try:
        print(f"🧹 Cleaning up position state for {symbol} {position_side or 'ALL'}")

        # Clean up tracked_positions
        keys_to_remove = []
        for key in tracked_positions.keys():
            if symbol in key:
                if position_side is None or position_side in key:
                    keys_to_remove.append(key)

        for key in keys_to_remove:
            del tracked_positions[key]
            print(f"🗑️ Removed tracked position: {key}")

        # Clean up trailing_stops
        keys_to_remove = []
        for key in trailing_stops.keys():
            if symbol in key:
                if position_side is None or position_side in key:
                    keys_to_remove.append(key)

        for key in keys_to_remove:
            del trailing_stops[key]
            print(f"🗑️ Removed trailing stop: {key}")

        # Clean up symbol_states for this bot
        bot_key = f"{BOT_INSTANCE_ID}_{symbol}"
        if bot_key in symbol_states:
            if position_side:
                # If specific position side, only clear that side's data
                state = symbol_states[bot_key]
                if isinstance(state, dict) and position_side in state:
                    del state[position_side]
                    print(f"🗑️ Removed symbol state for {position_side}")

                    # If no position sides left, remove entire symbol state
                    if not any(side in state for side in ['LONG', 'SHORT']):
                        del symbol_states[bot_key]
                        print(f"🗑️ Removed entire symbol state: {bot_key}")
            else:
                # Remove entire symbol state
                del symbol_states[bot_key]
                print(f"🗑️ Removed symbol state: {bot_key}")

        print(f"✅ Position state cleanup completed for {symbol}")

    except Exception as e:
        print(f"❌ Error in cleanup_position_state: {str(e)}")
        import traceback
        traceback.print_exc()

def handle_trailing_stops(symbol, current_price, position_amt):
    """
    Handle trailing stops for bot positions
    """
    try:
        # Find the correct position side based on position amount
        position_side = 'LONG' if position_amt > 0 else 'SHORT'
        bot_position_key = f"{symbol}_{BOT_INSTANCE_ID}_{position_side}"

        if bot_position_key not in trailing_stops:
            return  # No trailing stop for this position

        ts_data = trailing_stops[bot_position_key]

        # Skip if no current stop loss order
        if not ts_data.get('current_sl_order_id'):
            return

        # Get current market price
        ticker = api_call_with_retry(client.ticker_price, symbol=symbol)
        if not ticker:
            return

        current_price = float(ticker['price'])
        entry_price = ts_data['entry_price']
        side = ts_data['side']
        trailing_distance = ts_data.get('trailing_distance', sl_normal)  # Fallback to normal SL

        should_update = False
        new_sl_price = None

        print(f"🔍 Checking trailing stop for {symbol} {position_side}:")
        print(f"   Current price: {current_price:.6f}")
        print(f"   Entry price: {entry_price:.6f}")
        print(f"   Current SL: {ts_data['current_sl_price']:.6f}")

        if side == 'buy':  # LONG position
            # Update highest price seen
            if ts_data['highest_price'] is None or current_price > ts_data['highest_price']:
                ts_data['highest_price'] = current_price

            # Calculate new stop loss (trail below highest price)
            new_sl_price = ts_data['highest_price'] * (1 - trailing_distance)

            # Only update if new SL is higher than current SL (tighter stop)
            if new_sl_price > ts_data['current_sl_price']:
                should_update = True
                print(f"   New high: {ts_data['highest_price']:.6f} -> SL: {new_sl_price:.6f}")

        else:  # SHORT position
            # Update lowest price seen
            if ts_data['lowest_price'] is None or current_price < ts_data['lowest_price']:
                ts_data['lowest_price'] = current_price

            # Calculate new stop loss (trail above lowest price)
            new_sl_price = ts_data['lowest_price'] * (1 + trailing_distance)

            # Only update if new SL is lower than current SL (tighter stop)
            if new_sl_price < ts_data['current_sl_price']:
                should_update = True
                print(f"   New low: {ts_data['lowest_price']:.6f} -> SL: {new_sl_price:.6f}")

        if should_update and new_sl_price:
            # Cancel current stop loss order
            try:
                cancel_response = api_call_with_retry(
                    client.cancel_order,
                    symbol=symbol,
                    orderId=ts_data['current_sl_order_id'],
                    recvWindow=10000
                )
                if cancel_response:
                    print(f"✅ Cancelled old SL order: {ts_data['current_sl_order_id']}")
                else:
                    print(f"⚠️ Could not cancel old SL order: {ts_data['current_sl_order_id']}")
            except Exception as e:
                print(f"⚠️ Error cancelling old SL: {e}")

            # Validate new stop loss price
            sl_price_validation = validate_and_format_order_params(
                symbol,
                stop_price=new_sl_price,
                quantity=abs(position_amt)
            )

            if sl_price_validation:
                # Determine order parameters
                sl_order_side = 'SELL' if side == 'buy' else 'BUY'

                # Place new trailing stop loss order
                sl_params = {
                    'symbol': symbol,
                    'side': sl_order_side,
                    'positionSide': position_side,
                    'type': 'STOP_MARKET',
                    'quantity': sl_price_validation['quantity'],
                    'stopPrice': sl_price_validation['stop_price'],
                    'timeInForce': 'GTC',
                    'recvWindow': 10000,
                    'newClientOrderId': f"{BOT_INSTANCE_ID}_TSL_{position_side}_{int(time.time())}"
                }

                sl_response = api_call_with_retry(client.new_order, **sl_params)

                if sl_response:
                    # Update trailing stop data
                    ts_data['current_sl_price'] = new_sl_price
                    ts_data['current_sl_order_id'] = sl_response['orderId']

                    direction = '-' if side == 'buy' else '+'
                    improvement = abs((new_sl_price - ts_data['current_sl_price']) / entry_price * 100)

                    print(f"🎯 Trailing stop updated for {symbol} {position_side}:")
                    print(f"   New SL: {new_sl_price:.6f} ({direction}{trailing_distance * 100:.2f}%)")
                    print(f"   Order ID: {sl_response['orderId']}")

                    # Save updated trailing stop data
                    trailing_stops[bot_position_key] = ts_data

                else:
                    print(f"❌ Failed to place new trailing stop loss for {symbol} {position_side}")
            else:
                print(f"❌ Invalid stop loss parameters for {symbol}")

    except Exception as e:
        print(f"❌ Error in handle_trailing_stops for {symbol}: {str(e)}")
        import traceback
        traceback.print_exc()

def cancel_bot_orders(symbol):
    """Cancel only orders placed by this bot instance"""
    try:
        # Get all open orders for the symbol
        response = api_call_with_retry(client.get_orders, symbol=symbol, recvWindow=10000)
        if response is None:
            return

        cancelled_count = 0
        for order in response:
            # Check if this order was placed by this bot (check clientOrderId or custom order ID)
            order_id = order.get('orderId')
            client_order_id = order.get('clientOrderId', '')

            # If clientOrderId contains our bot ID, cancel it
            if BOT_INSTANCE_ID in client_order_id or not client_order_id:
                try:
                    cancel_response = api_call_with_retry(
                        client.cancel_order,
                        symbol=symbol,
                        orderId=order_id,
                        recvWindow=10000
                    )
                    if cancel_response:
                        cancelled_count += 1
                        print(f"{BOT_NAME}: Cancelled order {order_id} for {symbol}")
                except Exception as e:
                    print(f"{BOT_NAME}: Error cancelling order {order_id}: {str(e)}")

        if cancelled_count > 0:
            print(f"{BOT_NAME}: Cancelled {cancelled_count} orders for {symbol}")
        else:
            print(f"{BOT_NAME}: No orders to cancel for {symbol}")

    except Exception as error:
        print(f"{BOT_NAME}: Error cancelling orders: {str(error)}")
        traceback.print_exc()

def update_trailing_stop_monitoring():
    """
    Main function to check and update all trailing stops
    Call this in your main loop
    """
    try:
        if not trailing_stops:
            return

        print(f"🔄 Checking {len(trailing_stops)} trailing stops...")

        # Get all current positions
        positions = api_call_with_retry(client.get_position_risk, recvWindow=10000)
        if not positions:
            return

        # Check each tracked trailing stop
        for bot_position_key, ts_data in list(trailing_stops.items()):
            symbol = ts_data['symbol']
            position_side = ts_data['position_side']

            # Find corresponding position
            current_position = None
            for pos in positions:
                if (pos['symbol'] == symbol and
                        pos['positionSide'] == position_side):
                    position_amt = float(pos['positionAmt'])
                    if abs(position_amt) > 0:  # Position still exists
                        current_position = pos
                        break

            if current_position:
                # Position still exists, update trailing stop
                position_amt = float(current_position['positionAmt'])
                current_price = float(current_position['markPrice'])

                handle_trailing_stops(symbol, current_price, position_amt)
            else:
                # Position closed, remove from trailing stops
                print(f"📝 Position closed, removing trailing stop: {bot_position_key}")
                del trailing_stops[bot_position_key]

    except Exception as e:
        print(f"❌ Error in update_trailing_stop_monitoring: {e}")
        import traceback
        traceback.print_exc()

def enhanced_check_position_status(symbols):
    """
    Enhanced position status check with proper trailing stop handling
    """
    try:
        positions = api_call_with_retry(client.get_position_risk, recvWindow=10000)
        if not positions:
            return

        current_time = datetime.now()

        # Check if we're approaching 16:00 ET close time
        et_close_time = current_time.replace(hour=16, minute=0, second=0, microsecond=0)
        if current_time.hour >= 15 and current_time.minute >= 45:  # 15:45 ET warning
            print("🚨 Approaching 16:00 ET close time - preparing to close all positions")

        # Update trailing stops for all active positions
        update_trailing_stop_monitoring()

        for symbol in symbols:
            # Check bot-specific positions
            for position in positions:
                if position['symbol'] == symbol:
                    position_amt = float(position['positionAmt'])
                    if position_amt != 0:
                        # This is an active position
                        position_side = 'LONG' if position_amt > 0 else 'SHORT'
                        bot_position_key = f"{symbol}_{BOT_INSTANCE_ID}_{position_side}"

                        # Check if this position belongs to our bot
                        if bot_position_key in trailing_stops or any(
                                BOT_INSTANCE_ID in key for key in bot_positions if symbol in key):
                            entry_price = float(position['entryPrice'])
                            mark_price = float(position['markPrice'])
                            unrealized_pnl = float(position['unRealizedProfit'])

                            print(f"📊 Monitoring {symbol} {position_side}: {position_amt:.6f} @ {entry_price:.6f}")
                            print(f"   Mark: {mark_price:.6f} | PnL: {unrealized_pnl:.2f}")

                            # Force close at 16:00 ET
                            #if current_time.hour >= 16:
                             #   print(f"🕐 16:00 ET reached - force closing {symbol} position")
                              #  close_position(symbol, position_side)
    except Exception as e:
        print(f"❌ Error in enhanced_check_position_status: {e}")
        import traceback
        traceback.print_exc()

def monitor_and_cleanup_closed_positions():
    """
    Monitor positions and clean up orders/state when positions are closed
    Should be called periodically in main trading loop
    """
    try:
        # Get current positions from exchange
        positions_response = api_call_with_retry(client.get_position_risk, recvWindow=10000)
        if not positions_response:
            return

        # Create set of active positions (symbol_positionSide)
        active_positions = set()
        for position in positions_response:
            symbol = position['symbol']
            position_side = position.get('positionSide', 'BOTH')
            position_amt = float(position.get('positionAmt', 0))

            if position_amt != 0:
                active_positions.add(f"{symbol}_{position_side}")

        # Check our tracked positions against active positions
        tracked_keys = list(trailing_stops.keys())

        for position_key in tracked_keys:
            # Extract symbol and position side from key
            # Format: "ETHUSDT_ETHVWAPBOT-5_LONG"
            parts = position_key.split('_')
            if len(parts) >= 3:
                symbol = parts[0]
                position_side = parts[-1]  # Last part should be LONG/SHORT

                position_check_key = f"{symbol}_{position_side}"

                # If position is no longer active, clean up
                if position_check_key not in active_positions:
                    print(f"🔔 Position closed detected: {symbol} ({position_side})")

                    # Cancel remaining orders for this position
                    cancel_orders_for_position(symbol, position_side)

                    # Clean up internal state
                    cleanup_position_state(symbol, position_side)

                    # Small delay to prevent overwhelming the API
                    time.sleep(1)

    except Exception as e:
        print(f"❌ Error in monitor_and_cleanup_closed_positions: {str(e)}")
        import traceback
        traceback.print_exc()


def track_position(symbol, quantity, entry_price, side, position_side):
    """
    Enhanced position tracking that includes position_side for hedge mode
    Updates the existing track_position function
    """
    position_key = f"{symbol}_{BOT_INSTANCE_ID}_{position_side}"
    tracked_positions[position_key] = {
        'bot_id': BOT_INSTANCE_ID,
        'symbol': symbol,
        'quantity': quantity,
        'entry_price': entry_price,
        'side': side,
        'position_side': position_side,
        'timestamp': datetime.now()
    }
    print(f"📝 Position tracked: {position_key}")

def get_bot_positions():
    """Get only positions placed by this bot instance"""
    try:
        resp = api_call_with_retry(client.get_position_risk, recvWindow=10000)
        if resp is None:
            return []

        bot_pos = []
        for elem in resp:
            if float(elem['positionAmt']) != 0:
                # Check if this position was placed by this bot
                # We can identify this by checking if there are any orders with our bot ID
                symbol = elem['symbol']
                orders = api_call_with_retry(client.get_orders, symbol=symbol, recvWindow=10000)
                if orders:
                    for order in orders:
                        client_order_id = order.get('clientOrderId', '')
                        if BOT_INSTANCE_ID in client_order_id:
                            bot_pos.append(symbol)
                            break
        return bot_pos
    except Exception as error:
        print(f"{BOT_NAME}: Error getting bot positions: {str(error)}")
        traceback.print_exc()
        return []


def get_bot_open_orders(symbol=None):
    """
    Get only orders that belong to this bot instance
    Useful for monitoring and debugging
    """
    try:
        if symbol:
            # Get orders for specific symbol
            orders_response = api_call_with_retry(client.get_open_orders, symbol=symbol, recvWindow=10000)
        else:
            # Get ALL open orders across all symbols
            # Note: Binance API requires different method for getting all orders
            try:
                # Try to get all open orders (this may require different API endpoint)
                orders_response = api_call_with_retry(client.get_orders, recvWindow=10000)

                # Filter to only open orders if we got historical orders
                if orders_response:
                    orders_response = [order for order in orders_response if
                                       order.get('status') in ['NEW', 'PARTIALLY_FILLED']]
            except:
                # Fallback: if we can't get all orders, just return empty list
                print("ℹ️ Cannot fetch all open orders without symbol, returning empty list")
                return []

        if not orders_response:
            return []

        bot_orders = []
        for order in orders_response:
            client_order_id = order.get('clientOrderId', '')
            if BOT_INSTANCE_ID in client_order_id:
                bot_orders.append(order)

        return bot_orders

    except Exception as e:
        print(f"❌ Error getting bot orders: {str(e)}")
        return []


def display_bot_status():
    """
    Display current bot status including positions and orders
    Useful for monitoring and debugging
    """
    try:
        print(f"\n📊 {BOT_NAME} Status Report")
        print("=" * 60)

        # Show tracked positions
        print(f"🎯 Tracked Positions ({len(tracked_positions)}):")
        for key, pos in tracked_positions.items():
            print(f"   {key}: {pos['quantity']} @ {pos['entry_price']:.6f} ({pos['side']})")

        # Show trailing stops
        print(f"\n🔄 Active Trailing Stops ({len(trailing_stops)}):")
        for key, stop in trailing_stops.items():
            symbol = stop['symbol']
            current_sl = stop['current_sl_price']
            print(f"   {key}: SL @ {current_sl:.6f}")

        # Show open orders - get orders for the symbol being traded
        # Since we're trading a single symbol in the main loop, use that
        print(f"\n📋 Open Orders:")

        # Try to get orders for tracked symbols
        all_bot_orders = []

        # Get orders from tracked positions
        tracked_symbols = set()
        for key in tracked_positions.keys():
            # Extract symbol from key (format: "SYMBOL_BOTID_POSITIONSIDE")
            symbol_part = key.split('_')[0]
            tracked_symbols.add(symbol_part)

        # Get orders from trailing stops
        for key in trailing_stops.keys():
            symbol_part = key.split('_')[0]
            tracked_symbols.add(symbol_part)

        # If no tracked symbols, use the main symbol from the trading loop
        if not tracked_symbols:
            # This will be set in the main loop - for now just show message
            print("   No active positions or orders")
        else:
            for sym in tracked_symbols:
                symbol_orders = get_bot_open_orders(sym)
                all_bot_orders.extend(symbol_orders)

            if all_bot_orders:
                for order in all_bot_orders[:5]:  # Show first 5 orders
                    symbol = order['symbol']
                    order_type = order['type']
                    side = order['side']
                    position_side = order.get('positionSide', 'BOTH')
                    print(f"   {symbol} {order_type} {side} ({position_side})")

                if len(all_bot_orders) > 5:
                    print(f"   ... and {len(all_bot_orders) - 5} more orders")
            else:
                print("   No open orders")

        print("=" * 60)

    except Exception as e:
        print(f"❌ Error displaying bot status: {str(e)}")
        import traceback
        traceback.print_exc()


def open_order(symbol, side, balance):
    """
    Open order without TP/SL - simplified for MA crossover strategy
    """
    try:
        # Set trading mode to hedge
        set_mode(symbol, 'HEDGE')
        set_leverage(symbol, leverage)

        # Calculate position size
        volume = balance * balance_percentage * leverage

        # Get current price
        ticker_response = api_call_with_retry(client.ticker_price, symbol=symbol)
        if not ticker_response:
            print(f"Could not get price for {symbol}")
            return None

        price = float(ticker_response['price'])
        qty = volume / price

        # Validate quantity
        qty_validation = validate_and_format_order_params(symbol, quantity=qty)
        if not qty_validation:
            print(f"Failed to validate quantity for {symbol}")
            return None

        qty = float(qty_validation['quantity'])

        print(f"\n🚀 Opening {side.upper()} position for {symbol}")
        print(f"   Position size: {qty}")
        print(f"   Current price: {price:.6f}")

        # HEDGE MODE: Set position side and order side
        position_side = 'LONG' if side == 'buy' else 'SHORT'
        order_side = 'BUY' if side == 'buy' else 'SELL'

        # Place market entry order
        order_params = {
            'symbol': symbol,
            'side': order_side,
            'positionSide': position_side,
            'type': 'MARKET',
            'quantity': qty_validation['quantity'],
            'recvWindow': 10000,
            'newClientOrderId': f"{BOT_INSTANCE_ID}_ENTRY_{position_side}_{int(time.time())}"
        }

        print(f"📝 Placing market entry order ({position_side})...")
        entry_response = api_call_with_retry(client.new_order, **order_params)

        if not entry_response:
            print("❌ Failed to place entry order")
            return None

        print("✅ Entry order placed successfully")

        # Get actual entry price
        sleep(2)
        positions = api_call_with_retry(client.get_position_risk, recvWindow=10000)

        actual_entry_price = None
        if positions:
            for position in positions:
                if (position['symbol'] == symbol and
                        position['positionSide'] == position_side):
                    pos_amt = float(position['positionAmt'])
                    if abs(pos_amt) > 0:
                        actual_entry_price = float(position['entryPrice'])
                        break

        if not actual_entry_price:
            actual_entry_price = price

        print(f"✅ Entry confirmed at: {actual_entry_price:.6f} ({position_side})")

        # Track position
        track_position(symbol, qty, actual_entry_price, side, position_side)

        # Update symbol state
        if symbol not in symbol_states:
            symbol_states[symbol] = {}

        symbol_states[symbol][position_side] = {
            'entry_time': datetime.now(),
            'entry_price': actual_entry_price,
            'position_side': position_side,
            'side': side,
            'quantity': qty,
            'last_signal_time': symbol_states.get(symbol, {}).get(position_side, {}).get('last_signal_time')
        }

        print(f"\n📊 {symbol} Position Summary ({position_side}):")
        print(f"   Direction: {side.upper()}")
        print(f"   Size: {qty}")
        print(f"   Entry: {actual_entry_price:.6f}")
        print(f"   Strategy: MA Crossover + ADX")

        return entry_response

    except Exception as e:
        print(f"❌ Error in open_order: {str(e)}")
        traceback.print_exc()
        return None


def close_opposite_position(symbol, new_signal_side):
    """
    Close opposite position when strategy signals a reversal

    Args:
        symbol: Trading symbol
        new_signal_side: 'buy' or 'sell' - the new signal direction

    Returns:
        bool: True if opposite position was closed, False otherwise
    """
    try:
        # Determine which position side to close
        position_side_to_close = 'SHORT' if new_signal_side == 'buy' else 'LONG'

        print(f"\n🔄 Checking for opposite position to close...")
        print(f"   New signal: {new_signal_side.upper()}")
        print(f"   Looking to close: {position_side_to_close}")

        # Get current positions
        positions = api_call_with_retry(client.get_position_risk, recvWindow=10000)
        if not positions:
            print("   No positions found")
            return False

        # Find opposite position
        for position in positions:
            if position['symbol'] == symbol and position['positionSide'] == position_side_to_close:
                position_amt = float(position['positionAmt'])

                if abs(position_amt) > 0:
                    print(f"   ✅ Found {position_side_to_close} position: {position_amt}")

                    # Cancel all orders for this position
                    cancel_orders_for_position(symbol, position_side_to_close)

                    # Close the position with market order
                    close_side = 'BUY' if position_side_to_close == 'SHORT' else 'SELL'

                    qty_validation = validate_and_format_order_params(
                        symbol,
                        quantity=abs(position_amt)
                    )

                    if qty_validation:
                        close_params = {
                            'symbol': symbol,
                            'side': close_side,
                            'positionSide': position_side_to_close,
                            'type': 'MARKET',
                            'quantity': qty_validation['quantity'],
                            'recvWindow': 10000,
                            'newClientOrderId': f"{BOT_INSTANCE_ID}_CLOSE_{position_side_to_close}_{int(time.time())}"
                        }

                        close_response = api_call_with_retry(client.new_order, **close_params)

                        if close_response:
                            print(f"   ✅ Closed {position_side_to_close} position")

                            # Clean up state
                            cleanup_position_state(symbol, position_side_to_close)

                            return True
                        else:
                            print(f"   ❌ Failed to close {position_side_to_close} position")
                            return False

        print(f"   No opposite position found")
        return False

    except Exception as e:
        print(f"❌ Error closing opposite position: {str(e)}")
        traceback.print_exc()
        return False

def should_pause_trading(symbol):
    """Check if trading should be paused due to recent volatility spike"""
    try:
        # Get bot-specific symbol state
        current_state = get_bot_symbol_state(symbol)

        # Check for recent volatility spike
        is_current_spike, spike_msg = check_volatility_spike(symbol)
        current_time = datetime.now()

        # If we detect a spike now, record it
        if is_current_spike:
            current_state['last_vol_spike_time'] = current_time
            current_state['vol_pause_until'] = current_time + timedelta(
                minutes=pause_duration * candle_interval_integreted)
            set_bot_symbol_state(symbol, current_state)
            print(f"🚨 Volatility spike detected for {symbol}! Pausing trading until {current_state['vol_pause_until']}")
            return True, f"New spike detected: {spike_msg}"

        # Check if we're still in a pause period from a previous spike
        if 'vol_pause_until' in current_state and current_state['vol_pause_until']:
            if current_time < current_state['vol_pause_until']:
                time_left = current_state['vol_pause_until'] - current_time
                return True, f"Still in volatility pause (ends in {time_left})"
            else:
                # Pause period has ended, clear it
                current_state['vol_pause_until'] = None
                set_bot_symbol_state(symbol, current_state)

        return False, "No volatility pause active"

    except Exception as e:
        print(f"Error checking pause status: {e}")
        return False, f"Error: {str(e)}"


def initialize_symbol_state(symbol):
    """Initialize state for a symbol"""
    return {
        'last_signal_time': None,
        'last_vol_spike_time': None,
        'vol_pause_until': None,
        'entry_time': None,
        'entry_price': None,
        'last_check_time': datetime.now(),
        'position_side': None,
        'last_vwap_cross': None,  # Track VWAP crosses
        'last_dayopen_cross': None  # Track day open crosses
    }


def load_daily_trade_counter():
    """Load daily trade counter from file"""
    try:
        if os.path.exists(TRADE_COUNTER_FILE):
            with open(TRADE_COUNTER_FILE, 'r') as f:
                data = json.load(f)
                return data
        else:
            return {}
    except Exception as e:
        print(f"⚠️ Error loading trade counter: {str(e)}")
        return {}


def save_daily_trade_counter(data):
    """Save daily trade counter to file"""
    try:
        with open(TRADE_COUNTER_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️ Error saving trade counter: {str(e)}")


def get_today_date():
    """Get today's date as string"""
    return datetime.now().strftime("%Y-%m-%d")


def get_daily_trade_count(symbol):
    """Get today's trade count for a specific symbol"""
    trade_data = load_daily_trade_counter()
    today = get_today_date()
    bot_key = f"{BOT_INSTANCE_ID}_{symbol}"

    if today in trade_data and bot_key in trade_data[today]:
        return trade_data[today][bot_key]
    return 0


def increment_daily_trade_count(symbol):
    """Increment today's trade count for a specific symbol"""
    trade_data = load_daily_trade_counter()
    today = get_today_date()
    bot_key = f"{BOT_INSTANCE_ID}_{symbol}"

    if today not in trade_data:
        trade_data[today] = {}

    if bot_key not in trade_data[today]:
        trade_data[today][bot_key] = 0

    trade_data[today][bot_key] += 1

    # Clean up old data (keep only last 7 days)
    cleanup_old_trade_data(trade_data)

    save_daily_trade_counter(trade_data)
    return trade_data[today][bot_key]


def cleanup_old_trade_data(trade_data):
    """Remove trade data older than 7 days"""
    try:
        today = datetime.now()
        cutoff_date = today - timedelta(days=7)

        dates_to_remove = []
        for date_str in trade_data.keys():
            try:
                date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                if date_obj < cutoff_date:
                    dates_to_remove.append(date_str)
            except ValueError:
                continue

        for date_str in dates_to_remove:
            del trade_data[date_str]

    except Exception as e:
        print(f"⚠️ Error cleaning up old trade data: {str(e)}")


def can_trade_today(symbol):
    """Check if we can still trade today (haven't reached daily limit)"""
    current_count = get_daily_trade_count(symbol)
    can_trade = current_count < MAX_TRADES_PER_DAY

    if not can_trade:
        print(f"🚫 {BOT_NAME}: Daily trade limit reached for {symbol} ({current_count}/{MAX_TRADES_PER_DAY})")

    return can_trade


def get_time_until_next_day():
    """Get seconds until next day (for logging purposes)"""
    now = datetime.now()
    tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return int((tomorrow - now).total_seconds())


def display_daily_trade_status(symbol):
    """Display current daily trade status"""
    current_count = get_daily_trade_count(symbol)
    remaining = MAX_TRADES_PER_DAY - current_count
    today = get_today_date()

    print(f"📊 {BOT_NAME} Daily Trade Status for {symbol} ({today}):")
    print(f"   Trades executed today: {current_count}/{MAX_TRADES_PER_DAY}")
    print(f"   Remaining trades: {remaining}")

    if remaining == 0:
        time_until_reset = get_time_until_next_day()
        hours = time_until_reset // 3600
        minutes = (time_until_reset % 3600) // 60
        print(f"   Trade limit reset in: {hours}h {minutes}m")


def get_bot_symbol_state(symbol):
    """Get bot-specific symbol state"""
    bot_key = f"{BOT_INSTANCE_ID}_{symbol}"
    if bot_key not in symbol_states:
        symbol_states[bot_key] = initialize_symbol_state(symbol)
    return symbol_states[bot_key]


def set_bot_symbol_state(symbol, state):
    """Set bot-specific symbol state"""
    bot_key = f"{BOT_INSTANCE_ID}_{symbol}"
    symbol_states[bot_key] = state


def track_bot_position(symbol, bot_id, quantity, entry_price, position_side):
    """Track bot-specific positions"""
    position_key = f"{symbol}_{bot_id}"
    bot_positions[position_key] = {
        'symbol': symbol,
        'bot_id': bot_id,
        'quantity': quantity,
        'entry_price': entry_price,
        'position_side': position_side,
        'timestamp': datetime.now()
    }


def track_bot_order(symbol, bot_id, order_id, order_type):
    """Track bot-specific orders"""
    order_key = f"{symbol}_{bot_id}_{order_id}"
    bot_orders[order_key] = {
        'symbol': symbol,
        'bot_id': bot_id,
        'order_id': order_id,
        'order_type': order_type,
        'timestamp': datetime.now()
    }


def track_bot_sl_order(bot_id, symbol, order_id):
    """Track stop loss orders for trailing stop functionality"""
    sl_key = f"{symbol}_{bot_id}_SL"
    bot_orders[sl_key] = {
        'symbol': symbol,
        'bot_id': bot_id,
        'order_id': order_id,
        'order_type': 'STOP_LOSS',
        'timestamp': datetime.now()
    }


def check_bot_orders():
    """Check only bot-specific orders"""
    try:
        response = client.get_orders(recvWindow=6000)
        bot_symbols = []

        for elem in response:
            # Check if order belongs to this bot instance
            client_order_id = elem.get('clientOrderId', '')
            if BOT_INSTANCE_ID in client_order_id:
                bot_symbols.append(elem['symbol'])

        return list(set(bot_symbols))  # Remove duplicates

    except ClientError as error:
        print(f"Error checking bot orders: {error}")
        return []


def calculate_moving_averages(kl, fast_period=9, slow_period=18, trend_period=200):
    """
    Calculate fast, slow, and trend moving averages

    Args:
        kl: DataFrame with OHLC data
        fast_period: Fast MA period (default 9)
        slow_period: Slow MA period (default 18)
        trend_period: Trend MA period (default 100)

    Returns:
        tuple: (fast_ma, slow_ma, trend_ma)
    """
    try:
        fast_ma = kl['Close'].rolling(window=fast_period).mean()
        slow_ma = kl['Close'].rolling(window=slow_period).mean()
        trend_ma = kl['Close'].rolling(window=trend_period).mean()

        return fast_ma, slow_ma, trend_ma
    except Exception as e:
        print(f"Error calculating moving averages: {e}")
        return None, None, None


def calculate_adx(kl, period=14):
    """
    Calculate ADX (Average Directional Index)

    Args:
        kl: DataFrame with High, Low, Close columns
        period: ADX calculation period (default 14)

    Returns:
        Series: ADX values
    """
    try:
        adx_indicator = ta.trend.ADXIndicator(
            high=kl['High'],
            low=kl['Low'],
            close=kl['Close'],
            window=period
        )
        adx = adx_indicator.adx()
        return adx
    except Exception as e:
        print(f"Error calculating ADX: {e}")
        return None


def check_ma_crossover(fast_ma, slow_ma, lookback=2):
    """
    Check for moving average crossover

    Args:
        fast_ma: Fast moving average series
        slow_ma: Slow moving average series
        lookback: Number of candles to check for crossover (default 2)

    Returns:
        str: 'bullish', 'bearish', or 'none'
    """
    try:
        # Get recent values
        fast_current = fast_ma.iloc[-1]
        fast_previous = fast_ma.iloc[-2]
        slow_current = slow_ma.iloc[-1]
        slow_previous = slow_ma.iloc[-2]

        # Bullish crossover: fast crosses above slow
        if fast_previous <= slow_previous and fast_current > slow_current:
            return 'bullish'

        # Bearish crossover: fast crosses below slow
        if fast_previous >= slow_previous and fast_current < slow_current:
            return 'bearish'

        return 'none'
    except Exception as e:
        print(f"Error checking MA crossover: {e}")
        return 'none'


def combined_strategy_signal(symbol, use_volatility_filter=False, use_bull_bias=False):
    """
    Combined MA Crossover + ADX Strategy

    BUY Signal Requirements:
    - Fast MA (9) crosses above Slow MA (18)
    - Price is above Trend MA (100)
    - ADX > 25 (strong trend)

    SELL Signal Requirements:
    - Fast MA (9) crosses below Slow MA (18)
    - Price is below Trend MA (100)
    - ADX > 25 (strong trend)

    Args:
        symbol: Trading symbol
        use_volatility_filter: Enable ATR volatility filter (optional)
        use_bull_bias: Not used in this strategy

    Returns:
        str: 'up' for buy, 'down' for sell, or 0 for no signal
    """
    try:
        print(f"\n{'=' * 60}")
        print(f"📊 Analyzing {symbol} - MA Crossover + ADX Strategy")
        print(f"{'=' * 60}")

        # Get candlestick data
        kl = klines(symbol, candle_interval, limit=150)
        if kl is None or len(kl) < 100:
            print("❌ Insufficient data for analysis")
            return 0

        # Optional: Check volatility filter
        if use_volatility_filter:
            is_acceptable = check_atr_volatility_filter(kl, symbol)
            if not is_acceptable:
                print("🚫 Volatility too high - skipping trade")
                return 0

        # Calculate indicators
        fast_ma, slow_ma, trend_ma = calculate_moving_averages(kl, 9, 18, 200)
        adx = calculate_adx(kl, 14)

        if fast_ma is None or slow_ma is None or trend_ma is None or adx is None:
            print("❌ Failed to calculate indicators")
            return 0

        # Get current values
        current_price = kl['Close'].iloc[-1]
        last_price = kl['Close'].iloc[-2]
        fast_ma_current = fast_ma.iloc[-1]
        slow_ma_current = slow_ma.iloc[-1]
        trend_ma_current = trend_ma.iloc[-1]
        adx_current = adx.iloc[-1]

        # Check for MA crossover
        crossover = check_ma_crossover(fast_ma, slow_ma)

        # Display current market conditions
        print(f"\n📈 Market Conditions:")
        print(f"   Price one candle ago: {last_price:.6f}")
        print(f"   Current Price: {current_price:.6f}")
        print(f"   Fast MA (9): {fast_ma_current:.6f}")
        print(f"   Slow MA (18): {slow_ma_current:.6f}")
        print(f"   Trend MA (100): {trend_ma_current:.6f}")
        print(f"   ADX (14): {adx_current:.2f}")
        print(f"   Crossover: {crossover.upper()}")

        # BUY Signal Logic
        if crossover == 'bullish':
            print(f"\n🔍 Bullish crossover detected!")

            # Check if price is above trend MA
            price_above_trend = current_price > trend_ma_current
            print(f"   Price above 100 MA: {'✅' if price_above_trend else '❌'}")

            # Check if ADX is strong
            adx_strong = adx_current > 25
            print(f"   ADX > 25: {'✅' if adx_strong else '❌'} ({adx_current:.2f})")

            if price_above_trend and adx_strong:
                print(f"\n✅ BUY SIGNAL CONFIRMED for {symbol}")
                print(f"   All conditions met:")
                print(f"   - Fast MA crossed above Slow MA ✅")
                print(f"   - Price above Trend MA ✅")
                print(f"   - Strong trend (ADX > 25) ✅")
                return 'up'
            else:
                print(f"\n⚠️ Bullish crossover but conditions not met")
                return 0

        # SELL Signal Logic
        elif crossover == 'bearish':
            print(f"\n🔍 Bearish crossover detected!")

            # Check if price is below trend MA
            price_below_trend = current_price < trend_ma_current
            print(f"   Price below 100 MA: {'✅' if price_below_trend else '❌'}")

            # Check if ADX is strong
            adx_strong = adx_current > 25
            print(f"   ADX > 25: {'✅' if adx_strong else '❌'} ({adx_current:.2f})")

            if price_below_trend and adx_strong:
                print(f"\n✅ SELL SIGNAL CONFIRMED for {symbol}")
                print(f"   All conditions met:")
                print(f"   - Fast MA crossed below Slow MA ✅")
                print(f"   - Price below Trend MA ✅")
                print(f"   - Strong trend (ADX > 25) ✅")
                return 'down'
            else:
                print(f"\n⚠️ Bearish crossover but conditions not met")
                return 0

        else:
            print(f"\n⏸️ No crossover detected - waiting for signal")
            return 0

    except Exception as e:
        print(f"❌ Error in combined_strategy_signal: {str(e)}")
        import traceback
        traceback.print_exc()
        return 0

def get_candle_close_time(current_time, candles_ago, candle_interval):
    """
    Calculate the actual close time for a candle N periods ago

    Args:
        current_time: Current datetime
        candles_ago: How many candles back (0 = current candle)
        candle_interval: Interval in minutes (e.g., 15 for 15m candles)

    Returns:
        datetime: The actual close time of the specified candle
    """
    # Calculate the most recent candle close time
    # For 15m candles: :00, :15, :30, :45
    minutes_since_hour = current_time.minute

    # Find the most recent candle close
    if candle_interval == 15:
        # For 15m candles
        possible_closes = [0, 15, 30, 45]
        # Find the most recent close time
        recent_close_minute = None
        for close_minute in reversed(possible_closes):
            if minutes_since_hour >= close_minute:
                recent_close_minute = close_minute
                break

        if recent_close_minute is None:
            # We're before the first close of the hour, so use the last close of previous hour
            recent_close_minute = 45
            most_recent_close = current_time.replace(minute=recent_close_minute, second=0, microsecond=0) - timedelta(
                hours=1)
        else:
            most_recent_close = current_time.replace(minute=recent_close_minute, second=0, microsecond=0)

    elif candle_interval == 5:
        # For 5m candles: :00, :05, :10, :15, etc.
        recent_close_minute = (minutes_since_hour // 5) * 5
        most_recent_close = current_time.replace(minute=recent_close_minute, second=0, microsecond=0)

    elif candle_interval == 1:
        # For 1m candles
        most_recent_close = current_time.replace(second=0, microsecond=0)

    else:
        # For other intervals, use generic calculation
        minutes_into_interval = minutes_since_hour % candle_interval
        recent_close_minute = minutes_since_hour - minutes_into_interval
        most_recent_close = current_time.replace(minute=recent_close_minute, second=0, microsecond=0)

    # Calculate the target candle close time
    target_candle_close = most_recent_close - timedelta(minutes=candles_ago * candle_interval)

    return target_candle_close


def get_candle_close_time(current_time, candles_ago, candle_interval):
    """
    Calculate the actual close time for a candle N periods ago

    Args:
        current_time: Current datetime
        candles_ago: How many candles back (0 = current candle)
        candle_interval: Interval in minutes (e.g., 15 for 15m candles)

    Returns:
        datetime: The actual close time of the specified candle
    """
    # Calculate the most recent candle close time
    # For 15m candles: :00, :15, :30, :45
    minutes_since_hour = current_time.minute

    # Find the most recent candle close
    if candle_interval == 15:
        # For 15m candles
        possible_closes = [0, 15, 30, 45]
        # Find the most recent close time
        recent_close_minute = None
        for close_minute in reversed(possible_closes):
            if minutes_since_hour >= close_minute:
                recent_close_minute = close_minute
                break

        if recent_close_minute is None:
            # We're before the first close of the hour, so use the last close of previous hour
            recent_close_minute = 45
            most_recent_close = current_time.replace(minute=recent_close_minute, second=0, microsecond=0) - timedelta(
                hours=1)
        else:
            most_recent_close = current_time.replace(minute=recent_close_minute, second=0, microsecond=0)

    elif candle_interval == 5:
        # For 5m candles: :00, :05, :10, :15, etc.
        recent_close_minute = (minutes_since_hour // 5) * 5
        most_recent_close = current_time.replace(minute=recent_close_minute, second=0, microsecond=0)

    elif candle_interval == 1:
        # For 1m candles
        most_recent_close = current_time.replace(second=0, microsecond=0)

    else:
        # For other intervals, use generic calculation
        minutes_into_interval = minutes_since_hour % candle_interval
        recent_close_minute = minutes_since_hour - minutes_into_interval
        most_recent_close = current_time.replace(minute=recent_close_minute, second=0, microsecond=0)

    # Calculate the target candle close time
    target_candle_close = most_recent_close - timedelta(minutes=candles_ago * candle_interval)

    return target_candle_close





def get_true_day_open(kl, market_timezone='UTC', fallback_hours=2, day_open_hour=12, day_open_minute=0):
    """
    Get the true day open price from the klines DataFrame.
    Handles midnight edge cases and timezone issues with configurable day open time.

    Args:
        kl: DataFrame with OHLCV data and datetime index/column
        market_timezone: Timezone of the market data (default: 'UTC')
        fallback_hours: Hours to look back if today's data isn't available (default: 2)
        day_open_hour: Hour of the day that marks the "day open" (default: 0 for midnight)
        day_open_minute: Minute of the hour for day open (default: 0)

    Returns:
        float: Today's opening price (or most recent available)

    Examples:
        # Traditional midnight open
        get_true_day_open(kl)

        # 12 PM (noon) as day open
        get_true_day_open(kl, day_open_hour=12)

        # 9:30 AM as day open (typical stock market)
        get_true_day_open(kl, day_open_hour=9, day_open_minute=30)
    """
    try:
        from datetime import datetime, date, timedelta
        import pandas as pd
        import pytz

        # Get current time in market timezone
        if market_timezone == 'UTC':
            market_tz = pytz.UTC
        else:
            market_tz = pytz.timezone(market_timezone)

        current_time = datetime.now(market_tz)
        today = current_time.date()

        # Calculate the day open time for today
        day_open_time = current_time.replace(
            hour=day_open_hour,
            minute=day_open_minute,
            second=0,
            microsecond=0
        )

        # If current time is before day open, we're still in "yesterday's" trading day
        if current_time < day_open_time:
            trading_day_start = day_open_time - timedelta(days=1)
            print(f"⏰ Before day open time ({day_open_hour:02d}:{day_open_minute:02d}), using previous day's open")
        else:
            trading_day_start = day_open_time

        # Early morning threshold (1 hour after day open)
        early_threshold = day_open_time + timedelta(hours=1)

        # If using datetime index
        if isinstance(kl.index, pd.DatetimeIndex):
            # Ensure index is timezone-aware
            if kl.index.tz is None:
                kl.index = kl.index.tz_localize(market_timezone)
            elif kl.index.tz != market_tz:
                kl.index = kl.index.tz_convert(market_timezone)

            # Get data at or after the trading day start
            day_data = kl[kl.index >= trading_day_start]

            if not day_data.empty:
                open_price = day_data['Open'].iloc[0]
                open_time = day_data.index[0].strftime('%Y-%m-%d %H:%M:%S')
                print(f"📈 Day open price at {open_time}: {open_price:.4f}")
                return open_price

            # If we're shortly after day open and no data yet, look back
            if current_time < early_threshold:
                print(f"⏰ Shortly after day open ({current_time.strftime('%H:%M')}), looking for most recent data...")

                cutoff_time = current_time - timedelta(hours=fallback_hours)
                recent_data = kl[kl.index >= cutoff_time]

                if not recent_data.empty:
                    most_recent_open = recent_data['Open'].iloc[0]
                    print(f"📈 Using most recent open price: {most_recent_open:.4f}")
                    return most_recent_open

        # If datetime is in a column (try common column names)
        datetime_columns = ['timestamp', 'datetime', 'time', 'date']
        for col in datetime_columns:
            if col in kl.columns:
                # Convert to datetime if not already
                if not pd.api.types.is_datetime64_any_dtype(kl[col]):
                    kl[col] = pd.to_datetime(kl[col])

                # Make timezone-aware if needed
                if kl[col].dt.tz is None:
                    kl[col] = kl[col].dt.tz_localize(market_timezone)
                elif kl[col].dt.tz != market_tz:
                    kl[col] = kl[col].dt.tz_convert(market_timezone)

                # Get data at or after the trading day start
                day_data = kl[kl[col] >= trading_day_start]

                if not day_data.empty:
                    open_price = day_data['Open'].iloc[0]
                    open_time = day_data[col].iloc[0].strftime('%Y-%m-%d %H:%M:%S')
                    print(f"📈 Day open price at {open_time}: {open_price:.4f}")
                    return open_price

                # Shortly after day open fallback
                if current_time < early_threshold:
                    print(
                        f"⏰ Shortly after day open ({current_time.strftime('%H:%M')}), looking for most recent data...")

                    cutoff_time = current_time - timedelta(hours=fallback_hours)
                    recent_data = kl[kl[col] >= cutoff_time]

                    if not recent_data.empty:
                        most_recent_open = recent_data['Open'].iloc[0]
                        print(f"📈 Using most recent open price: {most_recent_open:.4f}")
                        return most_recent_open

        # Final fallback: use the most recent available data
        print(f"⚠️  Warning: Could not find day open data. Using most recent available open: {kl['Open'].iloc[-1]:.4f}")
        return kl['Open'].iloc[-1]

    except Exception as e:
        print(f"⚠️  Error getting true day open: {str(e)}. Using most recent available open.")
        return kl['Open'].iloc[-1]


# Alternative: Simple time-aware version if you don't want timezone complexity
def get_true_day_open_simple(kl, wait_until_hour=1):
    """
    Simple version that waits until a specific hour before using today's date.

    Args:
        kl: DataFrame with OHLCV data
        wait_until_hour: Hour to wait until before using today's date (default: 1 AM)

    Returns:
        float: Opening price
    """
    try:
        from datetime import datetime, date, timedelta
        import pandas as pd

        current_time = datetime.now()

        # If it's before the wait hour, use yesterday's date
        if current_time.hour < wait_until_hour:
            target_date = (current_time - timedelta(days=1)).date()
            print(f"⏰ Before {wait_until_hour} AM, using yesterday's date: {target_date}")
        else:
            target_date = current_time.date()

        # If using datetime index
        if isinstance(kl.index, pd.DatetimeIndex):
            target_data = kl[kl.index.date == target_date]
            if not target_data.empty:
                return target_data['Open'].iloc[0]

        # If datetime is in a column
        datetime_columns = ['timestamp', 'datetime', 'time', 'date']
        for col in datetime_columns:
            if col in kl.columns:
                if not pd.api.types.is_datetime64_any_dtype(kl[col]):
                    kl[col] = pd.to_datetime(kl[col])

                target_data = kl[kl[col].dt.date == target_date]
                if not target_data.empty:
                    return target_data['Open'].iloc[0]

        # Fallback to most recent
        print(f"⚠️  Using most recent available open: {kl['Open'].iloc[-1]:.4f}")
        return kl['Open'].iloc[-1]

    except Exception as e:
        print(f"⚠️  Error: {str(e)}. Using most recent available open.")
        return kl['Open'].iloc[-1]


def calculate_atr(df, period=14):
    """
    Calculate Average True Range (ATR)

    Args:
        df: DataFrame with High, Low, Close columns
        period: ATR calculation period (default 14)

    Returns:
        Series with ATR values
    """
    high = df['High']
    low = df['Low']
    close = df['Close']

    # Calculate True Range components
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))

    # True Range is the maximum of the three components
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # ATR is the rolling average of True Range
    atr = true_range.rolling(window=period).mean()

    return atr


def check_atr_volatility_filter(kl, symbol, atr_period=14, lookback_period=100, volatility_threshold=1.5):
    """
    ATR-based volatility filter to avoid trading in extremely volatile conditions

    Args:
        kl: DataFrame with OHLC data
        symbol: Trading symbol (for logging)
        atr_period: Period for ATR calculation (default 14)
        lookback_period: Period for median ATR calculation (default 100)
        volatility_threshold: Multiplier for median ATR (default 1.5)

    Returns:
        bool: True if volatility is acceptable for trading, False otherwise
    """
    try:
        # Ensure we have enough data
        if len(kl) < max(atr_period, lookback_period):
            print(
                f"⚠️  Not enough data for ATR volatility filter ({len(kl)} bars available, need {max(atr_period, lookback_period)})")
            return True  # Allow trading if we don't have enough data

        # Calculate ATR
        atr = calculate_atr(kl, period=atr_period)

        # Get current ATR (most recent non-NaN value)
        current_atr = atr.dropna().iloc[-1]

        # Calculate median ATR over the lookback period
        recent_atr = atr.dropna().tail(lookback_period)
        median_atr = recent_atr.median()

        # Check if current volatility is too high
        volatility_ratio = current_atr / median_atr if median_atr > 0 else 0
        is_acceptable = volatility_ratio < volatility_threshold

        # Detailed logging
        print(f"📊 ATR Volatility Analysis for {symbol}:")
        print(f"   Current ATR: {current_atr:.6f}")
        print(f"   Median ATR ({lookback_period} bars): {median_atr:.6f}")
        print(f"   Volatility Ratio: {volatility_ratio:.2f}x")
        print(f"   Threshold: {volatility_threshold}x")
        print(f"   Status: {'✅ ACCEPTABLE' if is_acceptable else '🚫 TOO VOLATILE'}")

        return is_acceptable

    except Exception as e:
        print(f"Error in ATR volatility filter: {str(e)}")
        return True  # Allow trading on error to avoid blocking the bot


# Example usage with different configurations:
def run_strategy_with_volatility_filter(symbol):
    """Run strategy with ATR volatility filter enabled"""
    return combined_strategy_signal(symbol, use_volatility_filter=True)


def run_strategy_without_volatility_filter(symbol):
    """Run strategy without ATR volatility filter"""
    return combined_strategy_signal(symbol, use_volatility_filter=False)


def get_sleep_time(candle_interval_integreted = candle_interval_integreted):
    """Calculate time to sleep until next candle close"""
    try:
        current_time = datetime.now()

        # Calculate next candle close time based on interval
        if candle_interval == '15m':
            # Round to next 15-minute interval
            minutes = current_time.minute
            next_close_minute = ((minutes // 15) + 1) * 15

            if next_close_minute >= 60:
                next_close = current_time.replace(hour=current_time.hour + 1, minute=0, second=0, microsecond=0)
            else:
                next_close = current_time.replace(minute=next_close_minute, second=0, microsecond=0)

        elif candle_interval == '5m':
            minutes = current_time.minute
            next_close_minute = ((minutes // 5) + 1) * 5

            if next_close_minute >= 60:
                next_close = current_time.replace(hour=current_time.hour + 1, minute=0, second=0, microsecond=0)
            else:
                next_close = current_time.replace(minute=next_close_minute, second=0, microsecond=0)

        elif candle_interval == '1m':
            next_close = current_time.replace(second=0, microsecond=0) + timedelta(minutes=1)

        else:
            # Default to 15 minutes if interval not recognized
            next_close = current_time + timedelta(minutes=15)

        sleep_seconds = (next_close - current_time).total_seconds()

        # Ensure minimum sleep time
        if sleep_seconds < 10:
            sleep_seconds += 60 * candle_interval_integreted

        return int(sleep_seconds)

    except Exception as e:
        print(f"Error calculating sleep time: {e}")
        return 900  # Default 15 minutes


symbol = 'BTCUSDT'  # Change to your desired symbol
loop_iteration = 0  # Counter for periodic status display

while True:
    try:
        loop_iteration += 1

        balance, base_volume = get_balance_usdt()
        sleep(1)

        if balance is None or base_volume is None:
            print('Cannot connect to API. Check IP, restrictions or wait some time')
            continue

        print(f"{BOT_NAME}: My balance is: ", balance, " USDT")
        print(f"{BOT_NAME}: Base volume with standard percentage: ", base_volume, " USDT")

        pos = get_bot_positions()
        print(f'{BOT_NAME}: You have {len(pos)} opened positions:\n{pos}')
        enhanced_check_position_status([symbol])

        # 🔄 Update trailing stops for all tracked positions
        update_trailing_stops()

        # 🧹 Monitor and cleanup closed positions (every iteration)
        monitor_and_cleanup_closed_positions()

        ord = check_bot_orders()  # Use bot-specific order checking

        for elem in ord:
            if elem not in pos:
                cancel_bot_orders(elem)  # Use bot-specific cancellation

        # 📊 Display daily trade status
        display_daily_trade_status(symbol)

        # In the main while True loop, replace the signal handling section with:

        if len(pos) < qty_limit:
            signal = combined_strategy_signal(symbol, use_volatility_filter=False, use_bull_bias=False)

            if signal == 'up' and symbol not in ord:
                # Check daily trade limit
                if can_trade_today(symbol):
                    print(f'{BOT_NAME}: Found BUY signal for {symbol}')

                    # Close opposite position if exists
                    close_opposite_position(symbol, 'buy')
                    sleep(2)

                    set_mode(symbol, margin_type)
                    sleep(1)
                    set_leverage(symbol, leverage)
                    sleep(1)
                    print(f'{BOT_NAME}: Placing buy order for {symbol}')

                    order_result = open_order(symbol, 'buy', balance)

                    if order_result:
                        new_count = increment_daily_trade_count(symbol)
                        print(f"✅ {BOT_NAME}: BUY order executed! Daily trades: {new_count}/{MAX_TRADES_PER_DAY}")

                    pos = get_bot_positions()
                    sleep(1)
                    ord = check_bot_orders()
                    sleep(10)
                else:
                    print(f"🚫 {BOT_NAME}: Skipping BUY signal - daily trade limit reached")

            elif signal == 'down' and symbol not in ord:
                # Check daily trade limit
                if can_trade_today(symbol):
                    print(f'{BOT_NAME}: Found SELL signal for {symbol}')

                    # Close opposite position if exists
                    close_opposite_position(symbol, 'sell')
                    sleep(2)

                    set_mode(symbol, margin_type)
                    sleep(1)
                    set_leverage(symbol, leverage)
                    sleep(1)
                    print(f'{BOT_NAME}: Placing sell order for {symbol}')

                    order_result = open_order(symbol, 'sell', balance)

                    if order_result:
                        new_count = increment_daily_trade_count(symbol)
                        print(f"✅ {BOT_NAME}: SELL order executed! Daily trades: {new_count}/{MAX_TRADES_PER_DAY}")

                    pos = get_bot_positions()
                    sleep(1)
                    ord = check_bot_orders()
                    sleep(10)
                else:
                    print(f"🚫 {BOT_NAME}: Skipping SELL signal - daily trade limit reached")

        # 📊 Display bot status every 10 iterations (optional)
        if loop_iteration % 10 == 0:
            display_bot_status()

        sleep_seconds = get_sleep_time()
        current_time = datetime.now()
        next_candle = current_time + timedelta(seconds=sleep_seconds)

        print(f'Current time: {current_time.strftime("%Y-%m-%d %H:%M:%S")}')
        print(f'Waiting until next candle close at: {next_candle.strftime("%Y-%m-%d %H:%M:%S")}')
        print(f'Sleeping for {sleep_seconds} seconds')

        sleep(sleep_seconds)

    except KeyboardInterrupt:
        print(f"🛑 {BOT_NAME} stopped by user")
        # Optional: Cancel all bot orders and clean up on exit
        try:
            cancel_orders_for_position(symbol)
            cleanup_position_state(symbol)
        except:
            pass
        break
    except Exception as e:
        print(f"❌ Error in main trading loop: {str(e)}")
        import traceback

        traceback.print_exc()
        sleep(10)  # Wait before retrying
