import os

# --- Telegram API credentials (for Telethon user clients) ---
API_ID = 12345678           # Replace with your API ID from my.telegram.org
API_HASH = "your_api_hash_here"  # Replace with your API Hash

# --- Bot token from @BotFather ---
BOT_TOKEN = "your_bot_token_here"

# --- MongoDB Configuration ---
MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "telegram_account_bot"

# --- Owner & Admin IDs ---
OWNER_ID = 123456789        # Your Telegram user ID
ADMIN_IDS = [123456789]     # List of admin user IDs (include owner)

# --- Session storage ---
SESSION_DIR = "sessions"
