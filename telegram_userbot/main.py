"""
Entry point for the Multi-User Telegram Userbot Manager.
"""
import asyncio
import logging
import sys

from telegram import BotCommand
from telegram.ext import ApplicationBuilder, Application

import config
from database.mongo import connect
from userbot.manager import UserbotManager
from bot.handlers import register_all
from webapp import MiniAppServer

logging.basicConfig(
    stream=sys.stdout,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

CONTROL_BOT_COMMANDS = [
    BotCommand("start", "Open the main menu"),
    BotCommand("help", "Show main commands"),
    BotCommand("host", "Host a Telegram account"),
    BotCommand("unhost", "Remove the hosted account"),
    BotCommand("targetadd", "Add a group target mapping"),
    BotCommand("targetremove", "Remove a target mapping"),
    BotCommand("targetlist", "List target mappings"),
    BotCommand("targetremoveall", "Remove all target mappings"),
    BotCommand("boton", "Enable the message bridge"),
    BotCommand("botoff", "Disable the message bridge"),
    BotCommand("aimode", "Select provider or add its API key"),
    BotCommand("aimodeon", "Enable AI mention replies"),
    BotCommand("aimodeoff", "Disable AI mention replies"),
    BotCommand("cancel", "Cancel the current operation"),
    BotCommand("addkey", "Owner: add a Gemini API key"),
    BotCommand("addopenrouterkey", "Owner: add an OpenRouter key"),
    BotCommand("addclaudekey", "Owner: add an Anthropic key"),
    BotCommand("listkeys", "Owner: list masked provider keys"),
    BotCommand("delkey", "Owner: delete a provider key"),
    BotCommand("switchkey", "Owner: choose a provider key"),
    BotCommand("allcommands", "Show all control-bot commands"),
]


async def post_init(application: Application) -> None:
    """Called once after the Application is fully initialised."""
    database = await connect()
    if database is None:
        logger.warning(
            "MongoDB is unavailable. The control bot will still answer /start, "
            "but hosted-account persistence and restoration are paused."
        )
    manager: UserbotManager = application.bot_data["manager"]
    await manager.start_all()
    mini_app = MiniAppServer(manager)
    try:
        await mini_app.start()
    except Exception:
        logger.exception(
            "Mini App server failed to start; the Telegram control bot will continue."
        )
    application.bot_data["mini_app_server"] = mini_app
    try:
        await application.bot.set_my_commands(CONTROL_BOT_COMMANDS)
        logger.info("Control-bot command menu configured.")
    except Exception as exc:
        logger.warning("Could not configure Telegram command menu: %s", exc)
    logger.info("All systems up.")


async def post_shutdown(application: Application) -> None:
    """Called once during shutdown."""
    manager: UserbotManager = application.bot_data["manager"]
    mini_app = application.bot_data.get("mini_app_server")
    if mini_app is not None:
        await mini_app.stop()
    clients = list(manager._clients.values())
    await asyncio.gather(*(c.stop() for c in clients), return_exceptions=True)
    logger.info("All userbots stopped.")


def main() -> None:
    config.validate()

    manager = UserbotManager()

    app = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    # Hosted userbot plugins use this Bot API client for owner-facing
    # self-update progress notifications. This keeps progress messages coming
    # from the control bot instead of the hosted Telegram account.
    manager.control_bot = app.bot
    app.bot_data["manager"] = manager
    register_all(app, manager)

    logger.info("Starting bot polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
