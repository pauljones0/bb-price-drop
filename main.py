import requests
import json
import time
from decimal import Decimal, InvalidOperation
import sys

# --- Configuration ---
HISTORY_URL = "https://stocktrack.ca/bb/hist_data.php"
DROPS_URL = "https://stocktrack.ca/bb/drops_data.php"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0"
HEADERS = {"Host": "stocktrack.ca", "User-Agent": USER_AGENT}
DELAY_SECONDS = 10 # Delay between checking historical data for each SKU
BESTBUY_BASE_URL = "https://www.bestbuy.ca"

# --- Helper function for safe Decimal conversion ---
def safe_decimal(value):
    if value is None or value == "":
        return Decimal('0.00') # Treat missing/empty price as 0 for calculations
    try:
        # Remove any non-digit/non-decimal point characters except the first potential minus sign
        cleaned_value = "".join(c for i, c in enumerate(str(value)) if c.isdigit() or (c == '.' and '.' not in str(value)[:i]) or (c == '-' and i == 0))
        return Decimal(cleaned_value)
    except (InvalidOperation, TypeError):
        print(f"Warning: Could not convert price '{value}' to Decimal. Treating as 0.", file=sys.stderr)
        return Decimal('0.00')

# --- Function to get total item count ---
def get_total_count():
    try:
        response = requests.get(f"{DROPS_URL}?t=today&oss=false&posStart=0&count=0", headers=HEADERS)
        response.raise_for_status() # Raise an exception for bad status codes
        data = response.json()
        return data.get('total_count')
    except requests.exceptions.RequestException as e:
        print(f"Error fetching total count: {e}", file=sys.stderr)
        return None
    except json.JSONDecodeError:
        print("Error decoding JSON from total count request.", file=sys.stderr)
        return None

# --- Function to get all item data ---
def get_all_items(total_count):
    if total_count is None or not isinstance(total_count, int) or total_count <= 0:
        print(f"Invalid total count provided: {total_count}", file=sys.stderr)
        return None

    print(f"Fetching data for all {total_count} items...")
    try:
        response = requests.get(f"{DROPS_URL}?t=today&oss=false&posStart=0&count={total_count}", headers=HEADERS)
        response.raise_for_status()
        data = response.json()
        return data.get('data')
    except requests.exceptions.RequestException as e:
        print(f"Error fetching all item data: {e}", file=sys.stderr)
        return None
    except json.JSONDecodeError:
        print("Error decoding JSON from all item data request.", file=sys.stderr)
        return None

# --- Function to process historical data for a single item ---
def process_item_history(item_data):
    sku = item_data.get('Sku')
    if not sku:
        print("Item data is missing Sku. Skipping.", file=sys.stderr)
        return None

    print(f"Checking historical data for SKU: {sku}")

    try:
        history_response = requests.get(f"{HISTORY_URL}?sku={sku}", headers=HEADERS)
        history_response.raise_for_status()
        history_data = history_response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching historical data for SKU {sku}: {e}", file=sys.stderr)
        return None
    except json.JSONDecodeError:
        print(f"Error decoding JSON from historical data for SKU {sku}.", file=syserr.stderr)
        return None

    # Extract prices from the "1P" array
    historical_prices_str = [entry.get('y') for entry in history_data.get('1P', []) if entry.get('y') is not None]

    if not historical_prices_str:
        print(f"No historical price data found for SKU {sku}. Skipping calculations.", file=sys.stderr)
        return None

    # Convert prices to Decimal for accurate calculations
    historical_prices = [safe_decimal(price_str) for price_str in historical_prices_str]

    # Get the most recent price (assuming the last entry is the most recent)
    most_recent_price = historical_prices[-1]
    current_price = safe_decimal(item_data.get('NewPrice')) # Use the NewPrice from the drops data as the "current" price

    # Check if the current price is the lowest price in the historical data
    # Use safe_decimal for comparison too
    lowest_historical_price = min(historical_prices)

    # Handle potential floating point inaccuracies by comparing with a small tolerance
    is_all_time_low = current_price <= lowest_historical_price

    if is_all_time_low:
        print(f"SKU {sku} is at an all-time low price: {current_price}")

        # Calculate average price
        average_price = sum(historical_prices) / Decimal(len(historical_prices)) if historical_prices else Decimal('0.00')

        # Calculate highest historical price
        highest_historical_price = max(historical_prices)

        # Calculate second lowest historical price
        second_lowest_historical_price = None
        if len(historical_prices) >= 2:
            sorted_historical_prices = sorted(historical_prices)
            # Find the second distinct lowest price if possible
            unique_sorted_prices = sorted(list(set(historical_prices)))
            if len(unique_sorted_prices) >= 2:
                 second_lowest_historical_price = unique_sorted_prices[1]
            elif len(sorted_historical_prices) >= 2:
                 # Fallback to simply the second item if unique prices are not enough
                 second_lowest_historical_price = sorted_historical_prices[1]


        # Prepare results dictionary
        result = item_data.copy() # Start with all original item data

        # Add calculated metrics
        result['is_all_time_low'] = True
        result['lowest_historical_price'] = str(lowest_historical_price)
        result['highest_historical_price'] = str(highest_historical_price)
        result['average_historical_price'] = str(average_price.quantize(Decimal('0.01'))) # Round to 2 decimal places

        result['highest_to_current_diff'] = str((highest_historical_price - current_price).quantize(Decimal('0.01')))

        if second_lowest_historical_price is not None:
             result['second_lowest_to_current_diff'] = str((second_lowest_historical_price - current_price).quantize(Decimal('0.01')))
        else:
             result['second_lowest_to_current_diff'] = "N/A (less than 2 distinct historical prices)"


        # Calculate discount vs average (handle division by zero)
        if average_price > Decimal('0'):
            discount_vs_average = ((average_price - current_price) / average_price) * Decimal('100')
            result['discount_vs_average_percent'] = str(discount_vs_average.quantize(Decimal('0.01')))
        else:
            result['discount_vs_average_percent'] = "N/A (average price is 0)"

        # Add a direct link to the Best Buy product page
        result['bestbuy_link'] = f"{BESTBUY_BASE_URL}{item_data.get('Href', '')}"

        return result
    else:
        print(f"SKU {sku} current price ({current_price}) is not an all-time low (lowest historical was {lowest_historical_price}).")
        return None

# --- Main Execution ---

def main():
    print("Starting stock tracking and all-time low script...")

    # 1. Get the total count of items
    total_items = get_total_count()
    if total_items is None:
        sys.exit(1)

    # 2. Get all item data
    all_items = get_all_items(total_items)
    if all_items is None:
        sys.exit(1)

    print(f"Processing {len(all_items)} items...")

    all_time_low_items = []

    # 3. Process each item with a delay and check history
    for i, item in enumerate(all_items):
        processed_item = process_item_history(item)
        if processed_item:
            all_time_low_items.append(processed_item)

        # Wait before the next item, unless it's the last one
        if i < len(all_items) - 1:
            print(f"Waiting {DELAY_SECONDS} seconds before next item...")
            time.sleep(DELAY_SECONDS)

    # 4. Output the all-time low items in JSON format
    print("\n--- All-Time Low Items ---")
    # Use json.dumps for pretty printing the output JSON
    print(json.dumps(all_time_low_items, indent=2))

    # --- Instructions for Discord Bot ---
    print("\n--- For Discord Bot Integration ---")
    print("The JSON output above contains information about items currently at an all-time low.")
    print("A separate Discord bot script can:")
    print("1. Execute this script.")
    print("2. Capture the JSON output.")
    print("3. Parse the JSON.")
    print("4. Iterate through the list of all-time low items.")
    print("5. Format each item's data (Name, NewPrice, BestBuy link, calculated differences, etc.) into a Discord message or embed.")
    print("6. Use your Discord bot token and the Discord API to send these messages to a designated channel.")
    print("\nNote: Directly sending Discord messages requires adding discord library, managing bot token security, and implementing message formatting logic within a dedicated bot application.")

if __name__ == "__main__":
    main()