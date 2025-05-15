# Best Buy Price Drop Monitor

## 1. Project Overview

This project provides a Dockerized Python application to monitor Best Buy product prices for drops, specifically identifying items that reach an "all-time low" based on historical data from StockTrack.ca. When a new all-time low price is detected, the application sends a notification to a configured Discord webhook.

The application is designed to run continuously (24/7) within a Docker container, checking prices at user-defined intervals. Configuration is managed externally via a `config.json` file, and price history data can be persisted to prevent re-notifying for the same price drop and to track price trends over time.

## 2. Prerequisites

Before you begin, ensure you have the following software installed on your system:

*   **Docker:** [Installation Guide](https://docs.docker.com/get-docker/)
*   **Docker Compose:** Usually included with Docker Desktop. If not, [Installation Guide](https://docs.docker.com/compose/install/)
*   **Git:** (Optional, for cloning the repository) [Installation Guide](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git)

## 3. Setup Instructions

Follow these steps to get the Best Buy Price Drop Monitor up and running:

### 3.1. Clone the Repository (Optional)

If you haven't already, clone this repository to your local machine:

```bash
git clone <repository_url>
cd <repository_directory_name>
```

If you downloaded the files directly, navigate to the project's root directory.

### 3.2. Create `config.json`

The application requires a `config.json` file for its settings. A sample configuration file, [`config.sample.json`](config.sample.json:1), is provided.

1.  **Copy the sample file:**
    ```bash
    cp config.sample.json config.json
    ```
    On Windows, you can use:
    ```bash
    copy config.sample.json config.json
    ```
2.  **Edit `config.json`:**
    Open the newly created `config.json` file with a text editor and fill in your specific details. Refer to **Section 5: Configuration (`config.json`)** for a detailed explanation of each parameter.
    **Important:** Ensure you provide your Discord Webhook URL.

### 3.3. Build and Run the Application

Once `config.json` is configured, you can build and run the application using Docker Compose:

```bash
docker-compose up -d --build
```

*   `docker-compose up`: Starts the services defined in [`docker-compose.yml`](docker-compose.yml:1).
*   `-d`: Runs the containers in detached mode (in the background).
*   `--build`: Forces Docker Compose to build the image before starting the container. This is useful if you've made changes to the [`Dockerfile`](Dockerfile:1) or application code.

The first time you run this command, Docker will download the base Python image and build your application image, which might take a few minutes. Subsequent starts will be much faster unless the image needs to be rebuilt.

### 3.4. Accessing Logs

To view the application's logs while it's running:

```bash
docker-compose logs -f price-monitor
```

*   `price-monitor` is the service name defined in [`docker-compose.yml`](docker-compose.yml:1).
*   `-f`: Follows the log output in real-time. Press `Ctrl+C` to stop following.

### 3.5. Stopping and Restarting

*   **To stop the application:**
    ```bash
    docker-compose down
    ```
    This command stops and removes the containers. Your persisted data in the Docker volume (`price_monitor_data`) will remain.

*   **To stop the application without removing containers (e.g., for a quick restart):**
    ```bash
    docker-compose stop
    ```

*   **To restart the application after stopping:**
    ```bash
    docker-compose up -d
    ```
    If you made code changes and want to rebuild the image, use `docker-compose up -d --build`.

*   **To remove data volume (use with caution, this deletes persisted price history):**
    ```bash
    docker-compose down -v
    ```

## 4. Technical Overview

### 4.1. Script Operation (`main.py`)

The core logic resides in [`main.py`](main.py:1). Here's a high-level overview:

1.  **Initialization:**
    *   Loads configuration from `config.json`.
    *   Sets up logging to both console and a log file (specified in `config.json`).
    *   Loads persisted price history and last notified prices from a JSON file (e.g., `data/price_history.json`).
2.  **Price Checking (`check_prices` function):**
    *   Fetches the total count of items currently listed with price drops from StockTrack.ca.
    *   Fetches detailed data for all these items.
    *   For each item:
        *   Retrieves its historical price data from StockTrack.ca.
        *   Compares the current price against its historical prices to determine if it's an "all-time low" (based on StockTrack's data).
        *   Adds the current price point to its local price history.
        *   If an item is at an all-time low and hasn't been notified at this price (or lower) before, it's flagged for notification.
    *   Manages the size of the local price history data (pruning old entries).
    *   Saves the updated price history and last notified prices.
3.  **Discord Notifications:**
    *   If any items are flagged for notification, the script formats the details (name, price, link, historical stats) into a Discord embed.
    *   Sends the embed to the configured Discord webhook URL using an HTTP POST request.
4.  **Scheduling:**
    *   The `schedule` library is used to run the `check_prices` function at regular intervals, as defined by `price_check_interval_seconds` in `config.json`.
    *   An initial price check is performed immediately upon script startup.

### 4.2. Data Persistence

*   **Configuration (`config.json`):** Mounted read-only into the Docker container. This file is essential and must be created by the user. It is gitignored.
*   **Price History (`data/price_history.json` or as configured):**
    *   Stored in a Docker volume named `price_monitor_data` to persist across container restarts.
    *   Contains:
        *   `sku_price_points`: A dictionary where keys are SKUs, and values are lists of price points (timestamp and price).
        *   `last_notified_prices`: A dictionary where keys are SKUs and values are the prices at which the last "all-time low" notification was sent for that SKU. This prevents duplicate notifications for the same price drop.
    *   **Data Size Management:**
        *   `max_history_days`: Price points older than this many days are pruned from `sku_price_points`.
        *   `max_sku_entries`: A configurable limit for the number of SKUs to track in the history (currently logs a warning if exceeded, future implementation could prune SKUs).

### 4.3. Docker Setup

*   **[`Dockerfile`](Dockerfile:1):**
    *   Uses a multi-stage build for a smaller final image.
    *   **Builder Stage:** Installs Python, creates a virtual environment, and installs dependencies from [`requirements.txt`](requirements.txt:1).
    *   **Final Stage:** Copies the virtual environment and the application script (`main.py`) into a slim Python base image.
    *   The `CMD` instruction runs `python main.py` when the container starts.
*   **[`docker-compose.yml`](docker-compose.yml:1):**
    *   Defines a single service `price-monitor`.
    *   Builds the Docker image using the `Dockerfile` in the current context.
    *   Sets `restart: unless-stopped` to ensure the container restarts automatically unless manually stopped.
    *   Mounts `./config.json` from the host into `/app/config.json` inside the container (read-only).
    *   Mounts the Docker volume `price_monitor_data` to `/app/data` inside the container, allowing `main.py` to read/write persistent data.
    *   Configures basic JSON file logging for the container.
*   **[`requirements.txt`](requirements.txt:1):** Lists Python dependencies (`requests`, `schedule`).
*   **[`.dockerignore`](.dockerignore:1):** Specifies files and directories to exclude from the Docker build context, helping to keep the image size small and build times fast.
*   **[`.gitignore`](.gitignore:1):** Specifies intentionally untracked files that Git should ignore (e.g., `config.json`, virtual environments, `__pycache__`).

## 5. Configuration (`config.json`)

This file contains all user-specific settings for the application.

```json
{
  "discord_webhook_url": "YOUR_DISCORD_WEBHOOK_URL_HERE", // Required: The full URL for your Discord webhook.
  "discord": { // Optional: Settings to customize webhook appearance and behavior.
    "webhook_username": "StockTrack Price Monitor", // Optional: Custom username for messages sent via webhook.
    "webhook_avatar_url": "YOUR_AVATAR_URL_HERE_OR_LEAVE_EMPTY", // Optional: URL for a custom avatar for messages.
    "webhook_max_retries": 3, // Optional: Number of retries if sending a message fails (e.g., due to rate limits).
    "webhook_retry_delay_base_seconds": 5 // Optional: Base delay in seconds for retries (uses exponential backoff).
  },
  "monitoring": {
    "price_check_interval_seconds": 900, // Required: How often to check for price drops, in seconds (e.g., 900 = 15 minutes).
    "request_delay_seconds": 10, // Required: Delay in seconds between fetching historical data for each SKU to avoid overwhelming the server.
    "target_website_urls": { // Required: URLs for StockTrack.ca API.
      "history_url": "https://stocktrack.ca/bb/hist_data.php",
      "drops_url": "https://stocktrack.ca/bb/drops_data.php",
      "bestbuy_base_url": "https://www.bestbuy.ca" // Base URL for constructing product links.
    },
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0" // User-Agent string for HTTP requests.
  },
  "data_persistence": {
    "enabled": true, // Required: Set to true to enable saving price history and last notified prices. False to disable.
    "data_file_path": "data/price_history.json", // Required: Path (relative to /app in container) to store the price history JSON file. The 'data/' directory is mapped to the 'price_monitor_data' Docker volume.
    "max_history_days": 30, // Required: How many days of price points to keep for each SKU. Older entries are pruned.
    "max_sku_entries": 1000 // Required: Maximum number of SKUs to maintain detailed history for. (Currently logs a warning if exceeded).
  },
  "logging": {
    "log_level": "INFO", // Required: Logging level (e.g., DEBUG, INFO, WARNING, ERROR, CRITICAL).
    "log_file_path": "data/app.log", // Required: Path (relative to /app in container) for the application log file.
    "log_file_max_bytes": 10485760, // Optional (not directly implemented by basic logging): Max size of log file before rotation (e.g., 10MB).
    "log_file_backup_count": 5 // Optional (not directly implemented by basic logging): Number of backup log files to keep.
  },
  "api_keys": {
    "some_optional_api_key": "" // Placeholder for any other API keys you might need if extending the script.
  }
}
```

**Key Parameters to Configure:**

*   `discord_webhook_url`
*   `monitoring.price_check_interval_seconds`

## 6. Troubleshooting

*   **"CRITICAL: config.json not found."**
    *   **Cause:** The `config.json` file is missing from the root directory of your project (where `docker-compose.yml` is located).
    *   **Solution:** Copy [`config.sample.json`](config.sample.json:1) to `config.json` and fill in your details. Ensure it's in the same directory as your `docker-compose.yml` file.

*   **"CRITICAL: Error decoding config.json."**
    *   **Cause:** The `config.json` file has a syntax error (e.g., missing comma, incorrect bracket).
    *   **Solution:** Validate your `config.json` using a JSON linter or carefully review its structure.

*   **"Discord webhook URL is not configured." or "CRITICAL: 'discord_webhook_url' not found in config.json."**
    *   **Cause:** The `discord_webhook_url` is missing or empty in `config.json`.
    *   **Solution:** Ensure you have provided a valid Discord webhook URL in `config.json`.

*   **Error sending to Discord webhook: 4xx/5xx ...**
    *   **Cause:**
        *   `400 Bad Request`: The payload sent to Discord might be malformed. Check logs for the payload.
        *   `401 Unauthorized` / `403 Forbidden`: The webhook URL might be invalid or deactivated.
        *   `404 Not Found`: The webhook URL is incorrect or the webhook has been deleted.
        *   `429 Too Many Requests`: The script is sending messages too quickly and is being rate-limited by Discord. The script has a built-in retry mechanism for this.
        *   `5xx Server Error`: Discord is experiencing temporary issues.
    *   **Solution:**
        *   Verify the `discord_webhook_url` in `config.json` is correct and active.
        *   Check the application logs for more details on the error and the payload sent.
        *   If rate limited (429), the script should retry. If it persists, consider increasing `price_check_interval_seconds` or `webhook_retry_delay_base_seconds` in `config.json`.

*   **Container keeps restarting or exits with an error:**
    *   **Solution:** Check the logs using `docker-compose logs -f price-monitor`. The error message will usually indicate the problem (e.g., Python script error, network issue).

*   **No notifications are being sent:**
    *   **Check Logs:** Look for errors related to fetching data, processing items, or sending Discord webhook messages.
    *   **Verify Configuration:** Ensure the `discord_webhook_url` in `config.json` is correct and active. Check other `discord` settings if customized.
    *   **StockTrack.ca Availability:** The external service might be temporarily unavailable.
    *   **No All-Time Lows:** It's possible no items are currently meeting the "all-time low" criteria.
    *   **`LAST_NOTIFIED_PRICES`:** If an item hit an ATL and was notified, it won't be re-notified unless its price drops further below the last notified ATL price. Check `data/price_history.json`.

## 7. Known Limitations

*   **Dependency on StockTrack.ca:** The accuracy and availability of price data depend entirely on StockTrack.ca. Changes to their API or website structure could break this script.
*   **"All-Time Low" Definition:** The "all-time low" is based on the historical data provided by StockTrack.ca at the time of checking. This might not be a true all-time low if StockTrack's data is incomplete or reset. Exact logic is not guaranteed.
*   **Rate Limiting:** While a `request_delay_seconds` is implemented, aggressive polling (very short `price_check_interval_seconds` or many items) could still lead to IP blocking or rate limiting by StockTrack.ca.
*   **Error Handling:** While basic error handling is in place, more sophisticated retry mechanisms or error reporting could be added.
*   **Scalability for Many SKUs:** The current approach of fetching all items and then their individual histories might become slow if StockTrack lists thousands of "drops."
*   **Log Rotation:** Basic file logging is used. For long-term production use, more advanced log rotation (e.g., using Python's `RotatingFileHandler`) should be implemented directly in `main.py` or handled by Docker's logging drivers if configured for external log management. The `log_file_max_bytes` and `log_file_backup_count` in `config.json` are placeholders for such an implementation.

## 8. Advanced Users: Customization & Extension

*   **Modifying Price Logic:** The core price comparison logic is in `process_item_history` in [`main.py`](main.py:1). You can adjust how "all-time low" is determined or add other conditions.
*   **Adding More Data Sources:** Extend the script to pull data from other websites or APIs.
*   **Different Notification Channels:** Modify or add functions similar to `send_discord_notification` to send alerts via email, SMS, Slack, etc.
*   **Enhanced Data Analysis:** Use the persisted `price_history.json` for more detailed trend analysis or reporting.
*   **Improving Robustness:** Implement more sophisticated error handling, retry logic with backoff, or circuit breaker patterns for external API calls.
*   **Alternative Database:** The current `price_history.json` is simple. For larger datasets or more complex queries, consider integrating a lightweight database like SQLite (using Python's `sqlite3` module) within the Docker volume. The user's initial prompt mentioned Waitress, but Waitress is a WSGI server, not a database. SQLite would be a more appropriate built-in database solution.