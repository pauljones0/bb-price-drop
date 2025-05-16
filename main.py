#!/usr/bin/env python3
"""
Best Buy Price Drop Monitor

This script monitors Best Buy product prices from stocktrack.ca,
identifies significant price drops, and sends notifications via Discord webhook.
It periodically checks for price updates, maintains a history of fetched SKUs
to manage API call frequency, and logs its operations.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Dict, Any, Optional, List, Tuple, Union, cast

import requests
import schedule

# --- Global Configuration Store ---
CONFIG: Dict[str, Any] = {}

# --- Default Configuration Values ---
DEFAULT_SKU_FETCH_TIMESTAMPS_FILE: str = "data/sku_fetch_timestamps.json"
DEFAULT_SKU_FETCH_COOLDOWN_HOURS: int = 24
DEFAULT_MAX_SKU_ENTRIES: int = 1000
DEFAULT_LOG_FILE_PATH: str = "data/app.log"
DEFAULT_LOG_LEVEL: str = "INFO"
DEFAULT_REQUEST_TIMEOUT: int = 10  # seconds
DEFAULT_WEBHOOK_MAX_RETRIES: int = 3
DEFAULT_WEBHOOK_RETRY_DELAY_BASE: int = 5  # seconds

# --- Custom Exception Classes ---
class DiscordWebhookError(Exception):
    """Base class for Discord webhook sending errors."""

class DiscordWebhookTimeoutError(DiscordWebhookError):
    """Timeout error when sending to Discord webhook."""

class DiscordWebhookRequestError(DiscordWebhookError):
    """Request-related error (e.g., network) when sending to Discord webhook."""

class DiscordWebhookHTTPError(DiscordWebhookError):
    """HTTP error (e.g., 4xx, 5xx) from Discord, excluding 429."""
    def __init__(self, message: str, response: Optional[requests.Response] = None):
        super().__init__(message)
        self.response: Optional[requests.Response] = response

class DiscordWebhookRateLimitError(DiscordWebhookHTTPError):
    """Rate limit error (429) from Discord."""
    # Inherits __init__ from DiscordWebhookHTTPError

# --- Logging Setup ---
def setup_logging() -> None:
    """Configures logging for the application based on CONFIG."""
    log_config: Dict[str, Any] = cast(Dict[str, Any], CONFIG.get('logging', {}))
    log_level_str: str = str(log_config.get('log_level', DEFAULT_LOG_LEVEL)).upper()
    log_level: int = getattr(logging, log_level_str, logging.INFO)
    log_file_path: str = str(log_config.get('log_file_path', DEFAULT_LOG_FILE_PATH))

    os.makedirs(os.path.dirname(log_file_path), exist_ok=True)

    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file_path, encoding='utf-8')
        ]
    )
    # TODO: Add log rotation if needed, using logging.handlers.RotatingFileHandler

# --- Configuration Loading ---
def load_config() -> bool:
    """
    Loads configuration from 'config.json', merges with defaults,
    and stores it in the global CONFIG.
    Returns True on success, False on critical errors.
    """
    global CONFIG
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            user_config: Dict[str, Any] = json.load(f)
        CONFIG.update(user_config)

        # Ensure essential config sections and defaults
        CONFIG['logging'] = CONFIG.get('logging', {})
        CONFIG['data_persistence'] = CONFIG.get('data_persistence', {})
        CONFIG['monitoring'] = CONFIG.get('monitoring', {})
        CONFIG['discord'] = CONFIG.get('discord', {})

        # Populate specific settings with defaults if not present
        data_persistence_config: Dict[str, Any] = cast(Dict[str, Any], CONFIG['data_persistence'])
        discord_config: Dict[str, Any] = cast(Dict[str, Any], CONFIG['discord'])

        data_persistence_config['sku_fetch_timestamps_file_path'] = \
            data_persistence_config.get('sku_fetch_timestamps_file_path', DEFAULT_SKU_FETCH_TIMESTAMPS_FILE)
        data_persistence_config['sku_fetch_cooldown_hours'] = \
            data_persistence_config.get('sku_fetch_cooldown_hours', DEFAULT_SKU_FETCH_COOLDOWN_HOURS)
        data_persistence_config['max_sku_entries'] = \
            data_persistence_config.get('max_sku_entries', DEFAULT_MAX_SKU_ENTRIES)

        timestamps_file: str = str(data_persistence_config['sku_fetch_timestamps_file_path'])
        os.makedirs(os.path.dirname(timestamps_file), exist_ok=True)

        if not discord_config.get('discord_webhook_url'):
            logging.error("CRITICAL: 'discord_webhook_url' not found in config.json under 'discord' section.")
            return False
        return True

    except FileNotFoundError:
        logging.error("CRITICAL: config.json not found. Please create it, perhaps from config.sample.json.")
        return False
    except json.JSONDecodeError as e:
        logging.error(f"CRITICAL: Error decoding config.json: {e}. Please check its format.")
        return False
    except Exception as e:  # pylint: disable=broad-except
        logging.error(f"CRITICAL: An unexpected error occurred during config loading: {e}")
        return False

# --- SKU Fetch Timestamp Persistence ---
def get_sku_timestamps_path() -> str:
    """Returns the configured path for SKU fetch timestamps."""
    data_persistence_config: Dict[str, Any] = cast(Dict[str, Any], CONFIG.get('data_persistence', {}))
    return str(data_persistence_config.get('sku_fetch_timestamps_file_path', DEFAULT_SKU_FETCH_TIMESTAMPS_FILE))

def is_persistence_enabled() -> bool:
    """Checks if data persistence for SKU timestamps is enabled."""
    data_persistence_config: Dict[str, Any] = cast(Dict[str, Any], CONFIG.get('data_persistence', {}))
    return bool(data_persistence_config.get('enabled', False))

def load_sku_fetch_timestamps() -> Dict[str, str]:
    """Loads SKU fetch timestamps from the JSON file if persistence is enabled."""
    if not is_persistence_enabled():
        logging.info("Data persistence for SKU fetch timestamps is disabled.")
        return {}

    timestamps_file: str = get_sku_timestamps_path()
    try:
        if os.path.exists(timestamps_file):
            with open(timestamps_file, 'r', encoding='utf-8') as f:
                timestamps: Dict[str, str] = json.load(f)
            # Ensure values are strings, as expected by datetime.fromisoformat
            # The json.load already ensures timestamps is a Dict if the file is a valid JSON object.
            # The type hint Dict[str, str] for `timestamps` and the loop below handle type validation.
            return {k: str(v) for k, v in timestamps.items()}
        logging.info(f"No existing SKU fetch timestamps file found at {timestamps_file}. Starting fresh.")
    except (json.JSONDecodeError, IOError) as e:
        logging.error(f"Error loading SKU fetch timestamps from {timestamps_file}: {e}. Starting fresh.")
    return {}

def save_sku_fetch_timestamps(sku_fetch_timestamps: Dict[str, str]) -> None:
    """Saves SKU fetch timestamps to the JSON file if persistence is enabled."""
    if not is_persistence_enabled():
        return

    timestamps_file: str = get_sku_timestamps_path()
    try:
        with open(timestamps_file, 'w', encoding='utf-8') as f:
            json.dump(sku_fetch_timestamps, f, indent=2)
        logging.info(f"SKU fetch timestamps saved to {timestamps_file}")
    except IOError as e:
        logging.error(f"Error saving SKU fetch timestamps to {timestamps_file}: {e}")

def manage_sku_fetch_timestamps(sku_fetch_timestamps: Dict[str, str]) -> Dict[str, str]:
    """
    Manages the size of SKU fetch timestamps data, pruning oldest entries
    if it exceeds MAX_SKU_ENTRIES.
    """
    if not is_persistence_enabled():
        return sku_fetch_timestamps

    data_persistence_config: Dict[str, Any] = cast(Dict[str, Any], CONFIG.get('data_persistence', {}))
    max_entries: int = int(data_persistence_config.get('max_sku_entries', DEFAULT_MAX_SKU_ENTRIES))

    if len(sku_fetch_timestamps) > max_entries:
        logging.info(
            f"SKU timestamp history ({len(sku_fetch_timestamps)}) exceeds max ({max_entries}). Pruning..."
        )
        # Sort by timestamp (value), oldest first
        # Ensure item[1] is a valid ISO format string before calling fromisoformat
        valid_items: List[Tuple[str, str]] = []
        for k, v_str in sku_fetch_timestamps.items():
            try:
                datetime.fromisoformat(v_str) # Validate format
                valid_items.append((k, v_str))
            except ValueError:
                logging.warning(f"Invalid timestamp format '{v_str}' for SKU '{k}' during pruning. Skipping this entry for sorting.")

        sorted_skus: List[Tuple[str, str]] = sorted(
            valid_items,
            key=lambda item: datetime.fromisoformat(item[1])
        )

        num_to_prune: int = len(sorted_skus) - max_entries
        if num_to_prune > 0 : # Ensure num_to_prune is positive
            pruned_count = 0
            for i in range(num_to_prune):
                if i < len(sorted_skus): # Boundary check
                    sku_to_remove: str = sorted_skus[i][0]
                    if sku_to_remove in sku_fetch_timestamps:
                        del sku_fetch_timestamps[sku_to_remove]
                        pruned_count += 1
            if pruned_count > 0:
                logging.info(f"Pruned {pruned_count} oldest SKU fetch timestamp entries.")
    return sku_fetch_timestamps

# --- Helper function for safe Decimal conversion ---
def safe_decimal(value: Any, context: str = "price") -> Decimal:
    """
    Safely converts a value to a Decimal, cleaning common non-numeric characters.
    Logs a warning and returns Decimal('0.00') on failure.
    """
    if value is None:
        return Decimal('0.00')
    s_value: str = str(value).strip()
    if not s_value:
        return Decimal('0.00')

    cleaned_chars: List[str] = []
    has_decimal_point: bool = False
    for i, char_code in enumerate(s_value):
        if char_code.isdigit():
            cleaned_chars.append(char_code)
        elif char_code == '.' and not has_decimal_point:
            cleaned_chars.append(char_code)
            has_decimal_point = True
        elif char_code == '-' and i == 0:
            cleaned_chars.append(char_code)

    cleaned_value_str: str = "".join(cleaned_chars)

    if not cleaned_value_str or (cleaned_value_str == '-' and len(cleaned_value_str) == 1):
        logging.debug(f"Value '{value}' for {context} cleaned to empty or '-'. Treating as 0.")
        return Decimal('0.00')
    try:
        return Decimal(cleaned_value_str)
    except InvalidOperation:
        logging.warning(
            f"Could not convert {context} '{value}' (cleaned: '{cleaned_value_str}') to Decimal. Treating as 0."
        )
        return Decimal('0.00')

# --- Discord Webhook Integration ---
def _attempt_discord_request(webhook_url: str, payload_json: str, headers: Dict[str, str]) -> requests.Response:
    """
    Makes a single POST request to the Discord webhook.
    Raises custom exceptions for different error types.
    """
    discord_cfg: Dict[str, Any] = cast(Dict[str, Any], CONFIG.get('discord', {}))
    timeout: int = int(discord_cfg.get('request_timeout_seconds', DEFAULT_REQUEST_TIMEOUT))
    try:
        response = requests.post(webhook_url, data=payload_json, headers=headers, timeout=timeout)
        response.raise_for_status()  # Raises HTTPError for 4xx/5xx responses
        return response
    except requests.exceptions.Timeout as e:
        raise DiscordWebhookTimeoutError("Discord webhook request timed out.") from e
    except requests.exceptions.HTTPError as http_err:
        if http_err.response is not None and http_err.response.status_code == 429:
            raise DiscordWebhookRateLimitError(
                f"Discord rate limit hit (429). Response: {http_err.response.text[:200]}",
                response=http_err.response
            ) from http_err
        err_msg = f"Discord webhook request failed with HTTP status {http_err.response.status_code if http_err.response else 'Unknown'}: " \
                  f"{http_err.response.text[:200] if http_err.response else 'No response body'}"
        raise DiscordWebhookHTTPError(err_msg, response=http_err.response) from http_err
    except requests.exceptions.RequestException as e:  # Other network errors
        raise DiscordWebhookRequestError(f"Discord webhook request failed due to a network issue: {e}") from e

def send_discord_webhook_message(payload: Dict[str, Any]) -> bool:
    """
    Sends a message to the configured Discord webhook with retry logic.
    Returns True on success, False on failure after retries.
    """
    discord_cfg: Dict[str, Any] = cast(Dict[str, Any], CONFIG.get('discord', {}))
    webhook_url: Optional[str] = cast(Optional[str], discord_cfg.get('discord_webhook_url'))
    if not webhook_url:
        logging.error("Discord webhook URL is not configured. Cannot send message.")
        return False

    headers: Dict[str, str] = {'Content-Type': 'application/json'}
    max_retries: int = int(discord_cfg.get('webhook_max_retries', DEFAULT_WEBHOOK_MAX_RETRIES))
    retry_delay_base: int = int(discord_cfg.get('webhook_retry_delay_base_seconds', DEFAULT_WEBHOOK_RETRY_DELAY_BASE))
    payload_json_str: str = json.dumps(payload)  # Dump once

    for attempt in range(max_retries):
        try:
            response: requests.Response = _attempt_discord_request(webhook_url, payload_json_str, headers)
            if response.status_code in (200, 204):
                logging.info(f"Successfully sent message to Discord webhook. Status: {response.status_code}")
                return True
            logging.error(f"Unexpected success status from Discord: {response.status_code}. Body: {response.text[:200]}")
            return False

        except DiscordWebhookRateLimitError as e:
            logging.warning(str(e))
            retry_after_header: Optional[str] = None
            if e.response and e.response.headers:
                retry_after_header = e.response.headers.get('Retry-After')

            wait_time: int = retry_delay_base * (2 ** attempt)
            if retry_after_header:
                try:
                    wait_time = int(float(retry_after_header)) + 1
                    logging.info(f"Using Retry-After header: waiting {wait_time}s.")
                except ValueError:
                    logging.warning(
                        f"Could not parse Retry-After header '{retry_after_header}'. Using exponential backoff: {wait_time}s."
                    )
            if attempt + 1 < max_retries:
                logging.info(f"Retrying Discord message (attempt {attempt + 2}/{max_retries}) after {wait_time}s...")
                time.sleep(wait_time)
            else:
                logging.error(f"Discord rate limit: Max retries ({max_retries}) reached. Payload: {payload_json_str[:200]}...")
                return False

        except (DiscordWebhookTimeoutError, DiscordWebhookRequestError, DiscordWebhookHTTPError) as e:
            logging.error(f"{e.__class__.__name__}: {e}. Payload: {payload_json_str[:200]}...")
            if isinstance(e, (DiscordWebhookTimeoutError, DiscordWebhookRequestError)) and attempt + 1 < max_retries:
                exp_wait_time: int = retry_delay_base * (2 ** attempt)
                logging.info(f"Retrying Discord message (attempt {attempt + 2}/{max_retries}) after {exp_wait_time}s due to {e.__class__.__name__}...")
                time.sleep(exp_wait_time)
                continue
            return False

        except Exception as e:  # pylint: disable=broad-except
            logging.error(f"Unexpected error during Discord webhook sending: {e}. Payload: {payload_json_str[:200]}...")
            return False

    logging.error(f"Failed to send message to Discord webhook after {max_retries} attempts. Payload: {payload_json_str[:200]}...")
    return False

def send_discord_notification(item_details: Dict[str, Any]) -> None:
    """Constructs and sends a Discord notification for a price drop."""
    discord_cfg: Dict[str, Any] = cast(Dict[str, Any], CONFIG.get('discord', {}))
    webhook_username: str = str(discord_cfg.get('webhook_username', 'StockTrack Price Monitor'))
    webhook_avatar_url: Optional[str] = cast(Optional[str], discord_cfg.get('webhook_avatar_url'))

    monitoring_cfg: Dict[str, Any] = cast(Dict[str, Any], CONFIG.get('monitoring', {}))
    base_url_cfg: Dict[str, Any] = cast(Dict[str, Any], monitoring_cfg.get('target_website_urls', {}))
    bestbuy_base_url: str = str(base_url_cfg.get('bestbuy_base_url', ''))

    item_name: str = str(item_details.get('Name', 'N/A'))
    item_sku: str = str(item_details.get('Sku', 'N/A'))
    item_new_price: str = str(item_details.get('NewPrice', '0.00'))
    item_is_atl: bool = bool(item_details.get('is_all_time_low', False))
    item_lowest_hist: str = str(item_details.get('lowest_historical_price', 'N/A'))
    item_highest_hist: str = str(item_details.get('highest_historical_price', 'N/A'))
    item_avg_hist: str = str(item_details.get('average_historical_price', 'N/A'))
    item_h_to_c_diff: str = str(item_details.get('highest_to_current_diff', 'N/A'))
    item_sl_to_c_diff: str = str(item_details.get('second_lowest_to_current_diff', 'N/A'))
    item_disc_vs_avg: str = str(item_details.get('discount_vs_average_percent', 'N/A'))
    item_reason: str = str(item_details.get('notification_trigger_reason', 'Details in logs'))
    item_href: str = str(item_details.get('Href', ''))
    item_image_url: Optional[str] = cast(Optional[str], item_details.get('Image'))


    embed: Dict[str, Any] = {
        "title": f"🚨 Price Drop: {item_name}",
        "url": str(item_details.get('bestbuy_link', f"{bestbuy_base_url}{item_href}")),
        "color": 15158332,  # Red
        "fields": [
            {"name": "SKU", "value": item_sku, "inline": True},
            {"name": "Current Price", "value": f"${item_new_price}", "inline": True},
            {"name": "All-Time Low", "value": "Yes" if item_is_atl else "No", "inline": True},
            {"name": "Lowest Hist.", "value": f"${item_lowest_hist}", "inline": True},
            {"name": "Highest Hist.", "value": f"${item_highest_hist}", "inline": True},
            {"name": "Average Hist.", "value": f"${item_avg_hist}", "inline": True},
            {"name": "Diff (Highest-Now)", "value": f"${item_h_to_c_diff}", "inline": True},
            {"name": "Diff (2ndLow-Now)", "value": item_sl_to_c_diff, "inline": True},
            {"name": "Discount vs Avg.", "value": f"{item_disc_vs_avg}%", "inline": True},
            {"name": "Alert Reason", "value": item_reason, "inline": False}
        ],
        "footer": {"text": f"StockTrack Monitor | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"}
    }
    if item_image_url:
        embed["thumbnail"] = {"url": item_image_url}

    payload: Dict[str, Any] = {"embeds": [embed]}
    if webhook_username:
        payload['username'] = webhook_username
    if webhook_avatar_url:
        payload['avatar_url'] = webhook_avatar_url

    logging.info(f"Preparing to send Discord webhook for SKU {item_sku}")
    if send_discord_webhook_message(payload):
        logging.info(f"Successfully queued/sent Discord webhook for SKU {item_sku}")
    else:
        logging.error(f"Failed to send Discord webhook for SKU {item_sku}")

# --- Core Logic: Fetching Item Data ---
def _get_api_data(url: str, headers: Dict[str, str], description: str) -> Optional[Dict[str, Any]]:
    """Helper to fetch and parse JSON data from an API endpoint."""
    monitoring_cfg: Dict[str, Any] = cast(Dict[str, Any], CONFIG.get('monitoring', {}))
    timeout: int = int(monitoring_cfg.get('request_timeout_seconds', DEFAULT_REQUEST_TIMEOUT))
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return cast(Dict[str, Any], response.json())
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching {description}: {e}")
    except json.JSONDecodeError:
        logging.error(f"Error decoding JSON from {description} request.")
    return None

def get_total_count(monitoring_config: Dict[str, Any]) -> Optional[int]:
    """Fetches the total count of items from the drops URL."""
    target_urls: Dict[str, Any] = cast(Dict[str, Any], monitoring_config.get('target_website_urls', {}))
    drops_url: Optional[str] = cast(Optional[str], target_urls.get('drops_url'))
    if not drops_url:
        logging.error("Drops URL not configured in 'monitoring.target_website_urls.drops_url'.")
        return None
    headers: Dict[str, str] = {"Host": "stocktrack.ca", "User-Agent": str(monitoring_config.get('user_agent'))}
    api_url: str = f"{drops_url}?t=today&oss=false&posStart=0&count=0"
    data: Optional[Dict[str, Any]] = _get_api_data(api_url, headers, "total count")
    return cast(Optional[int], data.get('total_count')) if data else None

def get_all_items(total_count: Optional[int], monitoring_config: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Fetches all item data based on the total count."""
    target_urls: Dict[str, Any] = cast(Dict[str, Any], monitoring_config.get('target_website_urls', {}))
    drops_url: Optional[str] = cast(Optional[str], target_urls.get('drops_url'))

    if not drops_url:
        logging.error("Drops URL not configured.")
        return None
    if not isinstance(total_count, int) or total_count <= 0:
        logging.error(f"Invalid total_count ({total_count}) for fetching all items.")
        return None

    headers: Dict[str, str] = {"Host": "stocktrack.ca", "User-Agent": str(monitoring_config.get('user_agent'))}
    api_url: str = f"{drops_url}?t=today&oss=false&posStart=0&count={total_count}"
    logging.info(f"Fetching data for all {total_count} items...")
    data: Optional[Dict[str, Any]] = _get_api_data(api_url, headers, "all item data")
    return cast(Optional[List[Dict[str, Any]]], data.get('data')) if data else None

# --- Core Logic: Processing Individual Items ---
def _fetch_sku_history_if_needed(sku: str, monitoring_config: Dict[str, Any], sku_fetch_timestamps: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Fetches historical data for a SKU if not on cooldown."""
    data_persistence_cfg: Dict[str, Any] = cast(Dict[str, Any], CONFIG.get('data_persistence', {}))
    cooldown_hours: int = int(data_persistence_cfg.get('sku_fetch_cooldown_hours', DEFAULT_SKU_FETCH_COOLDOWN_HOURS))

    if is_persistence_enabled() and sku in sku_fetch_timestamps:
        try:
            last_fetch_time_str = sku_fetch_timestamps[sku]
            # Ensure last_fetch_time_str is a valid ISO format string
            last_fetch_time = datetime.fromisoformat(last_fetch_time_str)
            if datetime.now() - last_fetch_time < timedelta(hours=cooldown_hours):
                logging.info(f"SKU {sku} history fetched within cooldown ({cooldown_hours}h). Skipping API call.")
                return None
        except ValueError: # Catches fromisoformat errors
            logging.warning(f"Invalid timestamp format '{sku_fetch_timestamps.get(sku)}' for SKU {sku} in history. Will fetch.")


    target_urls: Dict[str, Any] = cast(Dict[str, Any], monitoring_config.get('target_website_urls', {}))
    history_url_template: Optional[str] = cast(Optional[str], target_urls.get('history_url'))
    if not history_url_template:
        logging.error("History URL not configured in 'monitoring.target_website_urls.history_url'.")
        return None

    logging.info(f"Fetching historical data for SKU: {sku}")
    headers: Dict[str, str] = {"Host": "stocktrack.ca", "User-Agent": str(monitoring_config.get('user_agent'))}
    history_api_url: str = f"{history_url_template}?sku={sku}"
    history_data_json: Optional[Dict[str, Any]] = _get_api_data(history_api_url, headers, f"historical data for SKU {sku}")

    if history_data_json and is_persistence_enabled():
        sku_fetch_timestamps[sku] = datetime.now().isoformat()
        logging.debug(f"Updated fetch timestamp for SKU {sku}.")
    return history_data_json

PriceStats = Dict[str, Union[Decimal, List[Decimal]]]

def _calculate_price_stats(history_data_json: Dict[str, Any], current_price_str: Optional[Any]) -> Optional[PriceStats]:
    """Calculates various price statistics from historical data."""
    history_1p: List[Dict[str, Any]] = cast(List[Dict[str, Any]], history_data_json.get('1P', []))
    historical_prices_str: List[Any] = [entry.get('y') for entry in history_1p if entry.get('y') is not None]
    current_price: Decimal = safe_decimal(current_price_str, context="current price for item")

    if not historical_prices_str:
        logging.info(f"No API historical price data found for SKU associated with current price {current_price_str}.")
        return None

    historical_prices: List[Decimal] = [safe_decimal(p_str, context="historical price") for p_str in historical_prices_str]
    # safe_decimal returns Decimal('0.00') for Nones, so no need to filter Nones here.

    if not historical_prices: # Should not happen if historical_prices_str was not empty and safe_decimal works
        logging.warning("Converted historical prices list is empty for SKU. Skipping notification check.")
        return None

    lowest_historical: Decimal = min(historical_prices)
    highest_historical: Decimal = max(historical_prices)
    average_historical: Decimal = sum(historical_prices) / Decimal(len(historical_prices)) if historical_prices else Decimal('0.00')

    return {
        "current_price": current_price,
        "lowest_historical": lowest_historical,
        "highest_historical": highest_historical,
        "average_historical": average_historical,
        "all_historical_prices": historical_prices
    }

def _check_notification_conditions(item_data: Dict[str, Any], stats: PriceStats) -> Tuple[bool, str]:
    """Determines if a notification should be sent based on price conditions."""
    current_price: Decimal = cast(Decimal, stats["current_price"])
    lowest_historical: Decimal = cast(Decimal, stats["lowest_historical"])
    highest_historical: Decimal = cast(Decimal, stats["highest_historical"])
    item_sku: Optional[str] = cast(Optional[str], item_data.get('Sku'))

    condition_base_50_percent: bool = False
    if highest_historical > Decimal('0'):
        condition_base_50_percent = (current_price < Decimal('0.5') * highest_historical)
    elif current_price < Decimal('0'):
        condition_base_50_percent = True

    if not condition_base_50_percent:
        logging.info(f"SKU {item_sku}: Base 50% rule not met (Current: {current_price}, Highest: {highest_historical}).")
        return False, ""

    current_stock_is_positive: bool = bool(item_data.get('InStock', False))
    sub_condition_2a: bool = (current_price == lowest_historical and current_stock_is_positive)
    sub_condition_2b: bool = False
    if lowest_historical > Decimal('0'):
        sub_condition_2b = (current_price <= lowest_historical * Decimal('0.9'))
    elif current_price < Decimal('0'):
         sub_condition_2b = (current_price < lowest_historical)

    if sub_condition_2a:
        reason = f"Price at 50% below highest ({highest_historical}), matches ATL ({lowest_historical}), and now in stock."
        logging.info(f"SKU {item_sku} meets notification criteria (ATL restock, 50% rule). Reason: {reason}")
        return True, reason
    if sub_condition_2b:
        reason = f"Price at 50% below highest ({highest_historical}) AND >=10% below ATL ({lowest_historical})."
        logging.info(f"SKU {item_sku} meets notification criteria (Significant drop below ATL, 50% rule). Reason: {reason}")
        return True, reason

    logging.info(
        f"SKU {item_sku}: Base 50% met, but sub-conditions not met. "
        f"Current: {current_price}, ATL: {lowest_historical}, InStock: {current_stock_is_positive}"
    )
    return False, ""

def _prepare_notification_details(item_data: Dict[str, Any], stats: PriceStats, notification_reason: str) -> Dict[str, Any]:
    """Formats the item details for notification, adding calculated stats."""
    result: Dict[str, Any] = item_data.copy()
    current_price: Decimal = cast(Decimal, stats["current_price"])
    lowest_historical: Decimal = cast(Decimal, stats["lowest_historical"])
    highest_historical: Decimal = cast(Decimal, stats["highest_historical"])
    average_historical: Decimal = cast(Decimal, stats["average_historical"])
    all_historical_prices: List[Decimal] = cast(List[Decimal], stats["all_historical_prices"])

    result['NewPrice'] = str(current_price.quantize(Decimal('0.01')))
    result['is_all_time_low'] = (current_price <= lowest_historical)
    result['notification_trigger_reason'] = notification_reason
    result['lowest_historical_price'] = str(lowest_historical.quantize(Decimal('0.01')))
    result['highest_historical_price'] = str(highest_historical.quantize(Decimal('0.01')))
    result['average_historical_price'] = str(average_historical.quantize(Decimal('0.01')))
    result['highest_to_current_diff'] = str((highest_historical - current_price).quantize(Decimal('0.01')))

    unique_sorted_prices: List[Decimal] = sorted(list(set(all_historical_prices)))
    second_lowest_price_diff_str: str = "N/A"
    if len(unique_sorted_prices) >= 2:
        second_lowest_val: Decimal = unique_sorted_prices[1]
        second_lowest_price_diff_str = str((second_lowest_val - current_price).quantize(Decimal('0.01')))
    result['second_lowest_to_current_diff'] = second_lowest_price_diff_str

    discount_vs_avg_percent_str: str = "N/A"
    if average_historical > Decimal('0'):
        discount: Decimal = ((average_historical - current_price) / average_historical) * Decimal('100')
        discount_vs_avg_percent_str = str(discount.quantize(Decimal('0.01')))
    result['discount_vs_average_percent'] = discount_vs_avg_percent_str

    monitoring_cfg: Dict[str, Any] = cast(Dict[str, Any], CONFIG.get('monitoring', {}))
    target_urls: Dict[str, Any] = cast(Dict[str, Any], monitoring_cfg.get('target_website_urls', {}))
    base_url: str = str(target_urls.get('bestbuy_base_url', ''))
    result['bestbuy_link'] = f"{base_url}{item_data.get('Href', '')}"
    return result

def process_item_history(item_data: Dict[str, Any], monitoring_config: Dict[str, Any], sku_fetch_timestamps: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """
    Processes a single item: fetches history, checks conditions, and prepares notification data.
    Returns item details for notification if criteria met, else None.
    """
    sku: Optional[str] = cast(Optional[str], item_data.get('Sku'))
    if not sku:
        logging.warning("Item data is missing Sku. Skipping.")
        return None

    history_data_json: Optional[Dict[str, Any]] = _fetch_sku_history_if_needed(sku, monitoring_config, sku_fetch_timestamps)
    if history_data_json is None:
        return None

    stats: Optional[PriceStats] = _calculate_price_stats(history_data_json, item_data.get('NewPrice'))
    if stats is None:
        return None

    should_notify, reason = _check_notification_conditions(item_data, stats)
    if should_notify:
        return _prepare_notification_details(item_data, stats, reason)

    return None

# --- Main Job Function ---
def check_prices() -> None:
    """Main job function to check prices and send notifications."""
    logging.info("Starting price check job...")
    monitoring_config: Optional[Dict[str, Any]] = cast(Optional[Dict[str, Any]], CONFIG.get('monitoring'))
    if not monitoring_config:
        logging.error("Monitoring configuration is missing. This is unexpected. Aborting job.")
        return

    sku_fetch_timestamps: Dict[str, str] = load_sku_fetch_timestamps()

    total_items: Optional[int] = get_total_count(monitoring_config)
    if total_items is None:
        logging.error("Failed to get total items count. Skipping this run.")
        save_sku_fetch_timestamps(sku_fetch_timestamps)
        return

    all_items_data: Optional[List[Dict[str, Any]]] = get_all_items(total_items, monitoring_config)
    if all_items_data is None:
        logging.error("Failed to get all items data. Skipping this run.")
        save_sku_fetch_timestamps(sku_fetch_timestamps)
        return

    logging.info(f"Processing {len(all_items_data)} items...")
    items_to_notify: List[Dict[str, Any]] = []
    request_delay_seconds: int = int(monitoring_config.get('request_delay_seconds', 10))

    for i, item in enumerate(all_items_data):
        processed_item: Optional[Dict[str, Any]] = process_item_history(item, monitoring_config, sku_fetch_timestamps)
        if processed_item:
            items_to_notify.append(processed_item)

        if i < len(all_items_data) - 1:
            logging.debug(f"Waiting {request_delay_seconds} seconds before next item...")
            time.sleep(request_delay_seconds)

    sku_fetch_timestamps = manage_sku_fetch_timestamps(sku_fetch_timestamps)
    save_sku_fetch_timestamps(sku_fetch_timestamps)

    if items_to_notify:
        logging.info(f"--- Found {len(items_to_notify)} items meeting notification criteria ---")
        for item_detail in items_to_notify:
            send_discord_notification(item_detail)
    else:
        logging.info("No new items to notify in this run.")
    logging.info("Price check job finished.")

# --- Main Execution ---
if __name__ == "__main__":
    if not load_config():
        print("CRITICAL: Configuration loading failed. Exiting.", file=sys.stderr)
        sys.exit(1)

    setup_logging()

    logging.info("Starting Best Buy Price Drop Monitor...")
    check_prices()

    monitoring_cfg_main: Dict[str, Any] = cast(Dict[str, Any], CONFIG.get('monitoring', {}))
    check_interval_seconds: int = int(monitoring_cfg_main.get('price_check_interval_seconds', 900))
    logging.info(f"Scheduling price checks every {check_interval_seconds} seconds.")
    schedule.every(check_interval_seconds).seconds.do(check_prices) # type: ignore[attr-defined]

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Shutting down price monitor (KeyboardInterrupt)...")
    except Exception as e:  # pylint: disable=broad-except
        logging.critical(f"An unexpected error occurred in the main loop: {e}", exc_info=True)
    finally:
        logging.info("Price monitor stopped.")