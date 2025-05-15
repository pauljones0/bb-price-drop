import requests
import json
import time
import schedule
import os
import sys
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
# import discord # Removed for webhook integration

# --- Global Variables ---
CONFIG = {}
LAST_NOTIFIED_PRICES = {} # SKU: last_notified_price
PRICE_HISTORY_FILE = "data/price_history.json" # Default, will be overridden by config
MAX_HISTORY_DAYS = 30 # Default, will be overridden by config
MAX_SKU_ENTRIES = 1000 # Default, will be overridden by config

# --- Logging Setup ---
def setup_logging():
    global CONFIG
    log_config = CONFIG.get('logging', {})
    log_level_str = log_config.get('log_level', 'INFO').upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    log_file_path = log_config.get('log_file_path', 'data/app.log')
    
    # Ensure data directory exists for log file
    os.makedirs(os.path.dirname(log_file_path), exist_ok=True)

    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout), # Log to console
            logging.FileHandler(log_file_path) # Log to file
        ]
    )
    # TODO: Add log rotation if needed, using RotatingFileHandler

# --- Configuration Loading ---
def load_config():
    global CONFIG, PRICE_HISTORY_FILE, MAX_HISTORY_DAYS, MAX_SKU_ENTRIES
    try:
        with open('config.json', 'r') as f:
            CONFIG = json.load(f)
        
        # Setup data persistence parameters
        persistence_config = CONFIG.get('data_persistence', {})
        PRICE_HISTORY_FILE = persistence_config.get('data_file_path', PRICE_HISTORY_FILE)
        MAX_HISTORY_DAYS = persistence_config.get('max_history_days', MAX_HISTORY_DAYS)
        MAX_SKU_ENTRIES = persistence_config.get('max_sku_entries', MAX_SKU_ENTRIES)

        # Ensure data directory exists for price history
        os.makedirs(os.path.dirname(PRICE_HISTORY_FILE), exist_ok=True)

        # Validate Discord webhook URL
        if not CONFIG.get('discord_webhook_url'):
            logging.error("CRITICAL: 'discord_webhook_url' not found in config.json.")
            return False

        return True
    except FileNotFoundError:
        logging.error("CRITICAL: config.json not found. Please create it from config.sample.json.")
        return False
    except json.JSONDecodeError:
        logging.error("CRITICAL: Error decoding config.json. Please check its format.")
        return False

# --- Data Persistence ---
def load_price_history():
    global LAST_NOTIFIED_PRICES
    if not CONFIG.get('data_persistence', {}).get('enabled', False):
        logging.info("Data persistence is disabled in config.")
        return {}
    try:
        if os.path.exists(PRICE_HISTORY_FILE):
            with open(PRICE_HISTORY_FILE, 'r') as f:
                history = json.load(f)
                # Convert string prices back to Decimal for LAST_NOTIFIED_PRICES
                LAST_NOTIFIED_PRICES = {sku: Decimal(price_str) for sku, price_str in history.get('last_notified_prices', {}).items()}
                return history.get('sku_price_points', {}) # This will store historical price points for each SKU
        logging.info(f"No existing price history file found at {PRICE_HISTORY_FILE}. Starting fresh.")
    except (json.JSONDecodeError, IOError) as e:
        logging.error(f"Error loading price history from {PRICE_HISTORY_FILE}: {e}. Starting fresh.")
    return {} # Return empty dict if file not found or error

def save_price_history(sku_price_points):
    if not CONFIG.get('data_persistence', {}).get('enabled', False):
        return
    try:
        # Convert Decimal prices in LAST_NOTIFIED_PRICES to string for JSON serialization
        serializable_last_notified = {sku: str(price) for sku, price in LAST_NOTIFIED_PRICES.items()}
        with open(PRICE_HISTORY_FILE, 'w') as f:
            json.dump({'sku_price_points': sku_price_points, 'last_notified_prices': serializable_last_notified}, f, indent=2)
        logging.info(f"Price history saved to {PRICE_HISTORY_FILE}")
    except IOError as e:
        logging.error(f"Error saving price history to {PRICE_HISTORY_FILE}: {e}")

def manage_price_history_size(sku_price_points):
    """
    Manages the size of the price history data.
    - Prunes old price points for each SKU.
    - Limits the total number of SKUs tracked if it exceeds MAX_SKU_ENTRIES (less critical for now).
    """
    if not CONFIG.get('data_persistence', {}).get('enabled', False):
        return sku_price_points

    cutoff_date = datetime.now() - timedelta(days=MAX_HISTORY_DAYS)
    updated_sku_price_points = {}
    pruned_count = 0

    for sku, entries in sku_price_points.items():
        # Filter entries: keep only those within the MAX_HISTORY_DAYS
        # Assuming entries are like: {'timestamp': 'YYYY-MM-DDTHH:MM:SS', 'price': '123.45'}
        valid_entries = []
        for entry in entries:
            try:
                entry_date = datetime.fromisoformat(entry['timestamp'])
                if entry_date >= cutoff_date:
                    valid_entries.append(entry)
            except (KeyError, ValueError):
                logging.warning(f"Malformed entry for SKU {sku} in price history: {entry}. Skipping.")
                valid_entries.append(entry) # Keep malformed entries for now to avoid data loss, or decide to discard

        if valid_entries:
            updated_sku_price_points[sku] = valid_entries
        pruned_count += (len(entries) - len(valid_entries))

    if pruned_count > 0:
        logging.info(f"Pruned {pruned_count} old price entries from history.")
    
    # Optional: Limit total number of SKUs (e.g., by LRU or other metric if needed)
    if len(updated_sku_price_points) > MAX_SKU_ENTRIES:
        logging.warning(f"Number of SKUs ({len(updated_sku_price_points)}) exceeds MAX_SKU_ENTRIES ({MAX_SKU_ENTRIES}). Consider implementing SKU pruning.")
        # For now, we'll just log. A more complex strategy might be needed if this becomes an issue.

    return updated_sku_price_points

def add_price_point(sku_price_points, sku, price):
    """Adds a new price point for a SKU with a timestamp."""
    if not CONFIG.get('data_persistence', {}).get('enabled', False):
        return
    
    if sku not in sku_price_points:
        sku_price_points[sku] = []
    
    # Avoid adding duplicate price if it's the same as the last recorded one for this run (not historical)
    # This simple check might need refinement based on how often prices are polled vs. actual changes
    if sku_price_points[sku] and safe_decimal(sku_price_points[sku][-1]['price']) == safe_decimal(price):
        # logging.debug(f"Price for SKU {sku} ({price}) is same as last recorded. Not adding duplicate point for this check.")
        return

    sku_price_points[sku].append({
        'timestamp': datetime.now().isoformat(),
        'price': str(safe_decimal(price)) # Store as string
    })


# --- Helper function for safe Decimal conversion ---
def safe_decimal(value):
    if value is None or value == "":
        return Decimal('0.00')
    try:
        cleaned_value = "".join(c for i, c in enumerate(str(value)) if c.isdigit() or (c == '.' and '.' not in str(value)[:i]) or (c == '-' and i == 0))
        return Decimal(cleaned_value)
    except (InvalidOperation, TypeError):
        logging.warning(f"Could not convert price '{value}' to Decimal. Treating as 0.")
        return Decimal('0.00')

# --- Discord Webhook Integration ---
def send_discord_webhook_message(payload):
    """
    Sends a message to the configured Discord webhook.
    Handles potential errors including rate limits.
    """
    webhook_url = CONFIG.get('discord_webhook_url')
    if not webhook_url:
        logging.error("Discord webhook URL is not configured. Cannot send message.")
        return False

    headers = {'Content-Type': 'application/json'}
    max_retries = CONFIG.get('discord', {}).get('webhook_max_retries', 3)
    retry_delay_base = CONFIG.get('discord', {}).get('webhook_retry_delay_base_seconds', 5)

    for attempt in range(max_retries):
        try:
            response = requests.post(webhook_url, data=json.dumps(payload), headers=headers, timeout=10)
            
            if response.status_code == 204 or response.status_code == 200: # 204 No Content is success for webhooks
                logging.info(f"Successfully sent message to Discord webhook. Status: {response.status_code}")
                return True
            elif response.status_code == 429: # Rate limited
                retry_after = response.headers.get('Retry-After') # Seconds, or a datetime string
                wait_time = retry_delay_base * (2 ** attempt) # Exponential backoff
                if retry_after:
                    try:
                        wait_time = int(float(retry_after)) + 1 # Add a small buffer
                        logging.warning(f"Discord rate limit hit (429). Retrying after {wait_time} seconds (from header). Attempt {attempt + 1}/{max_retries}.")
                    except ValueError: # If Retry-After is a timestamp
                        # For simplicity, we'll stick to exponential backoff if parsing fails
                        logging.warning(f"Discord rate limit hit (429). Could not parse Retry-After header '{retry_after}'. Retrying in {wait_time}s (exponential backoff). Attempt {attempt + 1}/{max_retries}.")
                else:
                    logging.warning(f"Discord rate limit hit (429). No Retry-After header. Retrying in {wait_time} seconds (exponential backoff). Attempt {attempt + 1}/{max_retries}.")
                
                if attempt + 1 < max_retries:
                    time.sleep(wait_time)
                else:
                    logging.error(f"Discord rate limit: Max retries ({max_retries}) reached for payload: {json.dumps(payload)[:200]}...")
                    return False
            else:
                logging.error(f"Error sending to Discord webhook: {response.status_code} - {response.text}. Payload: {json.dumps(payload)[:200]}...")
                return False # Don't retry on other client/server errors immediately

        except requests.exceptions.Timeout:
            logging.error(f"Timeout sending to Discord webhook. Attempt {attempt + 1}/{max_retries}. Payload: {json.dumps(payload)[:200]}...")
            if attempt + 1 < max_retries:
                time.sleep(retry_delay_base * (2 ** attempt))
            else:
                return False
        except requests.exceptions.RequestException as e:
            logging.error(f"Network error sending to Discord webhook: {e}. Attempt {attempt + 1}/{max_retries}. Payload: {json.dumps(payload)[:200]}...")
            if attempt + 1 < max_retries:
                time.sleep(retry_delay_base * (2 ** attempt)) # Basic backoff for network issues too
            else:
                return False
        except Exception as e: # Catch any other unexpected errors during the request
            logging.error(f"Unexpected error during Discord webhook POST: {e}. Payload: {json.dumps(payload)[:200]}...")
            return False # Do not retry on unknown errors

    logging.error(f"Failed to send message to Discord webhook after {max_retries} attempts. Payload: {json.dumps(payload)[:200]}...")
    return False

def send_discord_notification(item_details):
    """
    Constructs and sends a Discord notification using the webhook.
    """
    discord_config = CONFIG.get('discord', {}) # For potential future webhook-specific configs like username/avatar
    webhook_username = discord_config.get('webhook_username', 'StockTrack Price Monitor')
    webhook_avatar_url = discord_config.get('webhook_avatar_url', None) # Optional

    embed = {
        "title": f"🚨 Price Drop Alert: {item_details.get('Name', 'N/A')}",
        "url": item_details.get('bestbuy_link', CONFIG.get('monitoring', {}).get('target_website_urls', {}).get('bestbuy_base_url')),
        "color": 15158332, # Red color
        "fields": [
            {"name": "SKU", "value": item_details.get('Sku', 'N/A'), "inline": True},
            {"name": "Current Price", "value": f"${item_details.get('NewPrice', '0.00')}", "inline": True},
            {"name": "All-Time Low Status", "value": "Yes" if item_details.get('is_all_time_low') else "No", "inline": True}, # Clarified label
            {"name": "Lowest Historical", "value": f"${item_details.get('lowest_historical_price', 'N/A')}", "inline": True},
            {"name": "Highest Historical", "value": f"${item_details.get('highest_historical_price', 'N/A')}", "inline": True},
            {"name": "Average Historical", "value": f"${item_details.get('average_historical_price', 'N/A')}", "inline": True},
            {"name": "Diff (Highest - Current)", "value": f"${item_details.get('highest_to_current_diff', 'N/A')}", "inline": True},
            {"name": "Diff (2nd Lowest - Current)", "value": f"{item_details.get('second_lowest_to_current_diff', 'N/A')}", "inline": True},
            {"name": "Discount vs Avg.", "value": f"{item_details.get('discount_vs_average_percent', 'N/A')}%", "inline": True},
            {"name": "Alert Reason", "value": item_details.get('notification_trigger_reason', 'Refer to logs for details'), "inline": False}
        ],
        "footer": {"text": f"StockTrack Price Monitor | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"}
    }

    if 'Image' in item_details and item_details['Image']:
        embed["thumbnail"] = {"url": item_details['Image']}

    payload = {
        "embeds": [embed]
    }
    if webhook_username:
        payload['username'] = webhook_username
    if webhook_avatar_url:
        payload['avatar_url'] = webhook_avatar_url
    
    # Optional: Add content field for a simple text message alongside the embed
    # payload['content'] = f"Price drop for {item_details.get('Name', 'N/A')}!"

    logging.info(f"Preparing to send Discord webhook for SKU {item_details.get('Sku')}")
    if send_discord_webhook_message(payload):
        logging.info(f"Successfully queued/sent Discord webhook for SKU {item_details.get('Sku')}")
    else:
        logging.error(f"Failed to send Discord webhook for SKU {item_details.get('Sku')}")

# --- Core Logic (adapted from original script) ---
def get_total_count(monitoring_config):
    drops_url = monitoring_config.get('target_website_urls', {}).get('drops_url')
    headers = {"Host": "stocktrack.ca", "User-Agent": monitoring_config.get('user_agent')}
    if not drops_url:
        logging.error("Drops URL not configured.")
        return None
    try:
        response = requests.get(f"{drops_url}?t=today&oss=false&posStart=0&count=0", headers=headers)
        response.raise_for_status()
        data = response.json()
        return data.get('total_count')
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching total count: {e}")
        return None
    except json.JSONDecodeError:
        logging.error("Error decoding JSON from total count request.")
        return None

def get_all_items(total_count, monitoring_config):
    drops_url = monitoring_config.get('target_website_urls', {}).get('drops_url')
    headers = {"Host": "stocktrack.ca", "User-Agent": monitoring_config.get('user_agent')}
    if not drops_url:
        logging.error("Drops URL not configured.")
        return None
    if total_count is None or not isinstance(total_count, int) or total_count <= 0:
        logging.error(f"Invalid total count provided: {total_count}")
        return None

    logging.info(f"Fetching data for all {total_count} items...")
    try:
        response = requests.get(f"{drops_url}?t=today&oss=false&posStart=0&count={total_count}", headers=headers)
        response.raise_for_status()
        data = response.json()
        return data.get('data')
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching all item data: {e}")
        return None
    except json.JSONDecodeError:
        logging.error("Error decoding JSON from all item data request.")
        return None

def process_item_history(item_data, monitoring_config, sku_price_points):
    global LAST_NOTIFIED_PRICES
    sku = item_data.get('Sku')
    if not sku:
        logging.warning("Item data is missing Sku. Skipping.")
        return None

    logging.info(f"Checking historical data for SKU: {sku}")
    history_url = monitoring_config.get('target_website_urls', {}).get('history_url')
    headers = {"Host": "stocktrack.ca", "User-Agent": monitoring_config.get('user_agent')}
    if not history_url:
        logging.error("History URL not configured.")
        return None

    try:
        history_response = requests.get(f"{history_url}?sku={sku}", headers=headers)
        history_response.raise_for_status()
        history_data_json = history_response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching historical data for SKU {sku}: {e}")
        return None
    except json.JSONDecodeError:
        logging.error(f"Error decoding JSON from historical data for SKU {sku}.")
        return None

    historical_prices_str = [entry.get('y') for entry in history_data_json.get('1P', []) if entry.get('y') is not None]
    
    current_price_from_drops = safe_decimal(item_data.get('NewPrice'))
    add_price_point(sku_price_points, sku, current_price_from_drops) # Add current price to our history

    if not historical_prices_str:
        logging.info(f"No API historical price data found for SKU {sku}. Using stored history if available.")
        return None 

    historical_prices = [safe_decimal(price_str) for price_str in historical_prices_str]
    
    current_price = current_price_from_drops

    if not historical_prices:
        logging.warning(f"Converted historical prices list is empty for SKU {sku}. Skipping notification check.")
        return None

    lowest_historical_price = min(historical_prices)  # This is prior_atl_price
    highest_historical_price = max(historical_prices) # This is highest_price_offered

    should_notify = False
    notification_reason = ""

    # New notification logic
    # Condition 1: current_price < 50% of highest_price_offered
    condition_base_50_percent = False
    if highest_historical_price > Decimal('0'): # Avoid issues if highest price is zero
        condition_base_50_percent = (current_price < Decimal('0.5') * highest_historical_price)
    elif current_price < Decimal('0'): # If highest is 0, only negative current price meets <0.5*0
        condition_base_50_percent = True


    if condition_base_50_percent:
        current_stock_is_positive = item_data.get('InStock', False)

        # Condition 2a: current_price matches prior_atl_price, (prior_atl_stock was zero - ASSUMED), and current_stock > 0
        # Note: "prior_atl_stock associated with that prior_atl_price was zero" cannot be verified from current data.
        # This condition effectively triggers if an item hits its ATL and is (back) in stock.
        sub_condition_2a = (current_price == lowest_historical_price and current_stock_is_positive)

        # Condition 2b: current_price is at least 10% lower than prior_atl_price
        sub_condition_2b = False
        if lowest_historical_price > Decimal('0'): # Avoid issues if ATL is zero
            sub_condition_2b = (current_price <= lowest_historical_price * Decimal('0.9'))
        elif current_price < Decimal('0'): # If ATL is 0, only negative current price is < 0.9*0
             sub_condition_2b = (current_price < lowest_historical_price) # Effectively current_price < 0

        if sub_condition_2a or sub_condition_2b:
            if sku not in LAST_NOTIFIED_PRICES or current_price < LAST_NOTIFIED_PRICES[sku]:
                should_notify = True
                LAST_NOTIFIED_PRICES[sku] = current_price
                if sub_condition_2a:
                    notification_reason = f"Price at 50% below highest ({highest_historical_price}), matches ATL ({lowest_historical_price}), and now in stock."
                    logging.info(f"SKU {sku} meets notification criteria (ATL restock, 50% rule): Current Price {current_price}. Reason: {notification_reason}.")
                else:  # sub_condition_2b must be true
                    notification_reason = f"Price at 50% below highest ({highest_historical_price}) AND >=10% below ATL ({lowest_historical_price})."
                    logging.info(f"SKU {sku} meets notification criteria (Significant drop below ATL, 50% rule): Current Price {current_price}. Reason: {notification_reason}.")
            elif current_price == LAST_NOTIFIED_PRICES[sku]:
                logging.info(f"SKU {sku} meets new notification criteria at price {current_price}, but already notified at this price.")
            # No 'else' needed here as (current_price < LAST_NOTIFIED_PRICES[sku]) is the main re-notification trigger.
        else:
            logging.info(f"SKU {sku}: Base 50% condition met, but neither specific sub-condition (2a or 2b) met. Current: {current_price}, ATL: {lowest_historical_price}, Highest: {highest_historical_price}, InStock: {current_stock_is_positive}")
    else:
        logging.info(f"SKU {sku} does not meet base 50% condition (current price {current_price} vs 50% of highest {highest_historical_price}).")

    # Reset notification status if it no longer meets criteria
    if not should_notify and sku in LAST_NOTIFIED_PRICES:
        # This ensures that if an item was notified, but on a subsequent check it no longer meets *any*
        # of the new notification criteria (not just that the price hasn't dropped further),
        # its notification status is reset, allowing it to be notified again if it later re-qualifies.
        del LAST_NOTIFIED_PRICES[sku]
        logging.info(f"SKU {sku} no longer meets new notification criteria with current price {current_price}. Resetting its notification status.")

    if should_notify:
        result = item_data.copy()
        result['is_all_time_low'] = (current_price <= lowest_historical_price) # Keep original meaning for this field
        result['notification_trigger_reason'] = notification_reason
        result['lowest_historical_price'] = str(lowest_historical_price)
        result['highest_historical_price'] = str(highest_historical_price)
        
        average_price = sum(historical_prices) / Decimal(len(historical_prices)) if historical_prices else Decimal('0.00')
        result['average_historical_price'] = str(average_price.quantize(Decimal('0.01')))
        result['highest_to_current_diff'] = str((highest_historical_price - current_price).quantize(Decimal('0.01')))

        unique_sorted_prices = sorted(list(set(historical_prices)))
        second_lowest_historical_price = None
        if len(unique_sorted_prices) >= 2:
            second_lowest_historical_price = unique_sorted_prices[1]
        
        if second_lowest_historical_price is not None:
            result['second_lowest_to_current_diff'] = str((second_lowest_historical_price - current_price).quantize(Decimal('0.01')))
        else:
            result['second_lowest_to_current_diff'] = "N/A"

        if average_price > Decimal('0'):
            discount_vs_average = ((average_price - current_price) / average_price) * Decimal('100')
            result['discount_vs_average_percent'] = str(discount_vs_average.quantize(Decimal('0.01')))
        else:
            result['discount_vs_average_percent'] = "N/A"
        
        result['bestbuy_link'] = f"{monitoring_config.get('target_website_urls', {}).get('bestbuy_base_url', '')}{item_data.get('Href', '')}"
        return result
    
    return None

# --- Main Job Function ---
def check_prices():
    logging.info("Starting price check job...")
    monitoring_config = CONFIG.get('monitoring', {})
    if not monitoring_config:
        logging.error("Monitoring configuration is missing.")
        return

    sku_price_points = load_price_history() 

    total_items = get_total_count(monitoring_config)
    if total_items is None:
        logging.error("Failed to get total items count. Skipping this run.")
        return

    all_items_data = get_all_items(total_items, monitoring_config)
    if all_items_data is None:
        logging.error("Failed to get all items data. Skipping this run.")
        return

    logging.info(f"Processing {len(all_items_data)} items...")
    all_time_low_items_to_notify = []

    delay_seconds = monitoring_config.get('request_delay_seconds', 10)

    for i, item in enumerate(all_items_data):
        processed_item = process_item_history(item, monitoring_config, sku_price_points)
        if processed_item:
            all_time_low_items_to_notify.append(processed_item)

        if i < len(all_items_data) - 1:
            logging.debug(f"Waiting {delay_seconds} seconds before next item...")
            time.sleep(delay_seconds)
    
    sku_price_points = manage_price_history_size(sku_price_points)
    save_price_history(sku_price_points) 

    if all_time_low_items_to_notify:
        logging.info(f"\n--- Found {len(all_time_low_items_to_notify)} items at new all-time lows to notify ---")
        for item_detail in all_time_low_items_to_notify:
            send_discord_notification(item_detail) # Direct synchronous call
    else:
        logging.info("No new all-time low items to notify in this run.")
    
    logging.info("Price check job finished.")

# --- Main Execution ---
if __name__ == "__main__":
    if not load_config():
        sys.exit(1) 

    setup_logging() 

    logging.info("Starting Best Buy Price Drop Monitor...")
    
    check_prices() 

    check_interval_seconds = CONFIG.get('monitoring', {}).get('price_check_interval_seconds', 900)
    logging.info(f"Scheduling price checks every {check_interval_seconds} seconds.")
    schedule.every(check_interval_seconds).seconds.do(check_prices)

    try:
        while True:
            schedule.run_pending()
            time.sleep(1) 
    except KeyboardInterrupt:
        logging.info("Shutting down price monitor...")
    except Exception as e:
        logging.critical(f"An unexpected error occurred in the main loop: {e}", exc_info=True)
    finally:
        logging.info("Price monitor stopped.")