import os
import logging

logger = logging.getLogger(__name__)

def _int_env(*names: str, default: int = 0) -> int:
    for name in names:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            continue
        try:
            return int(raw.strip())
        except ValueError:
            logger.warning("Ignoring invalid integer environment variable %s.", name)
    return default


def _int_list_env(*names: str) -> list[int]:
    for name in names:
        raw = os.environ.get(name)
        if raw is None:
            continue
        values: list[int] = []
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                values.append(int(item))
            except ValueError:
                logger.warning("Ignoring invalid user ID in %s.", name)
        return values
    return []


BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
API_ID: int = _int_env("TELEGRAM_API_ID")
API_HASH: str = os.environ.get("TELEGRAM_API_HASH", "")
MONGO_URI: str = os.environ.get("MONGO_URI", "")
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
SESSION_SECRET: str = os.environ.get("SESSION_SECRET", "")
MINI_APP_URL: str = os.environ.get("MINI_APP_URL", "").strip()
MINI_APP_PATH: str = "/mini-app/"
MINI_APP_PUBLIC_PATH: str = os.environ.get(
    "MINI_APP_PUBLIC_PATH",
    "/api/mini-app/",
).strip()
WEBAPP_PORT: int = _int_env("PORT", "MINI_APP_PORT", default=8008)


class Config:
    """Compatibility configuration surface expected by imported plugins."""

    API_ID = API_ID
    API_HASH = API_HASH
    BOT_TOKEN = BOT_TOKEN
    MONGO_URI = MONGO_URI
    GEMINI_API_KEY = GEMINI_API_KEY
    SESSION_SECRET = SESSION_SECRET
    MINI_APP_URL = MINI_APP_URL
    MINI_APP_PATH = MINI_APP_PATH
    MINI_APP_PUBLIC_PATH = MINI_APP_PUBLIC_PATH
    WEBAPP_PORT = WEBAPP_PORT

    BOT_PREFIX = os.environ.get("ELITE_BOT_PREFIX", ".")
    BOT_NAME = os.environ.get("BOT_NAME", "FLEX FUCKER USERBOT")
    TG_BOT_USERNAME = os.environ.get("TG_BOT_USERNAME", "")

    OWNER_ID = _int_env("OWNER_ID", "TELEGRAM_OWNER_ID")
    SUDO_USERS = _int_list_env("SUDO_USERS")
    LOG_CHAT_ID = _int_env("LOG_CHAT_ID")

    STRING_SESSION = os.environ.get("ELITE_SESSION", "")
    DEFAULT_ALIVE_PIC = os.environ.get("ALIVE_PIC", "")
    DEFAULT_PING_PIC = os.environ.get("PING_PIC", "")
    ALIVE_NAME = os.environ.get("ALIVE_NAME", "FLEX FUCKER USERBOT")

    VERSION = os.environ.get("VERSION", "1.0.0")
    BRANCH = os.environ.get("BRANCH", "main")
    UPSTREAM_REPO = os.environ.get(
        "UPSTREAM_REPO",
        "https://github.com/shfhshddhd/Tohid0.2.git",
    )


def validate():
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not API_ID:
        missing.append("TELEGRAM_API_ID")
    if not API_HASH:
        missing.append("TELEGRAM_API_HASH")
    if not MONGO_URI:
        missing.append("MONGO_URI")
    if missing:
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")
    logger.info("Configuration validated successfully.")


def mini_app_url() -> str:
    """Return the configured public Mini App URL without exposing any secrets."""
    if MINI_APP_URL:
        return MINI_APP_URL.rstrip("/") + MINI_APP_PUBLIC_PATH

    # Replit supplies one of these domains at runtime. This fallback keeps local
    # development convenient; production should set MINI_APP_URL explicitly.
    domain = (
        os.environ.get("REPLIT_DOMAINS", "").split(",")[0].strip()
        or os.environ.get("REPLIT_DEV_DOMAIN", "").strip()
    )
    if domain:
        return f"https://{domain.rstrip('/')}{MINI_APP_PUBLIC_PATH}"
    return ""
