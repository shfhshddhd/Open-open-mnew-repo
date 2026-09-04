"""Environment-backed compatibility values for upstream plugins.

The source repository shipped account-specific fallback values.  The target
project must never use those values, so every credential and identity setting
comes from the target environment instead.
"""

import os

from dotenv import load_dotenv


load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0") or "0")
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
ELITE_SESSION = os.getenv("ELITE_SESSION", "")
ELITE_BOT_PREFIX = os.getenv("ELITE_BOT_PREFIX", ".")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ELITE_BOT_USERNAME = os.getenv("TG_BOT_USERNAME", "")

SUDO_USERS = [
    int(value.strip())
    for value in os.getenv("SUDO_USERS", "").split(",")
    if value.strip().lstrip("-").isdigit()
]
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "0") or "0")

ALIVE_PIC = os.getenv("ALIVE_PIC", "")
PING_PIC = os.getenv("PING_PIC", "")
ALIVE_NAME = os.getenv("ALIVE_NAME", "FLEX FUCKER USERBOT")

MONGO_URI = os.getenv("MONGO_URI", "")
UPSTREAM_REPO = os.getenv(
    "UPSTREAM_REPO",
    "https://github.com/shfhshddhd/Tohid0.2.git",
)
BRANCH = os.getenv("BRANCH", "main")
