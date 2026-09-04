from telegram.ext import Application, CallbackQueryHandler, CommandHandler
from bot.handlers.start import start_command, help_command, allcommands_command
from bot.handlers.host import build_host_handler, unhost_command
from bot.handlers.target import (
    targetadd_command,
    targetremove_command,
    targetlist_command,
    targetremoveall_command,
)
from bot.handlers.bot_toggle import boton_command, botoff_command
from bot.handlers.ai_mode import (
    aimode_command,
    aimodeon_command,
    aimodeoff_command,
)
from bot.handlers.gemini_keys import (
    addkey_command,
    addopenrouterkey_command,
    addclaudekey_command,
    delkey_command,
    listkeys_command,
    switchkey_command,
)
from bot.handlers.update_controls import update_control_callback
from bot.handlers.voice_chat import build_voice_chat_handler


def register_all(app: Application, manager) -> None:
    app.bot_data["manager"] = manager
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("allcommands", allcommands_command))
    app.add_handler(build_host_handler())
    app.add_handler(CommandHandler("unhost", unhost_command))
    app.add_handler(CommandHandler("targetadd", targetadd_command))
    app.add_handler(CommandHandler("targetremove", targetremove_command))
    app.add_handler(CommandHandler("targetlist", targetlist_command))
    app.add_handler(CommandHandler("targetremoveall", targetremoveall_command))
    app.add_handler(CommandHandler("boton", boton_command))
    app.add_handler(CommandHandler("botoff", botoff_command))
    app.add_handler(CommandHandler("aimode", aimode_command))
    app.add_handler(CommandHandler("aimodeon", aimodeon_command))
    app.add_handler(CommandHandler("aimodeoff", aimodeoff_command))
    app.add_handler(CommandHandler("addkey", addkey_command))
    app.add_handler(CommandHandler("addopenrouterkey", addopenrouterkey_command))
    app.add_handler(CommandHandler("addclaudekey", addclaudekey_command))
    app.add_handler(CommandHandler("listkeys", listkeys_command))
    app.add_handler(CommandHandler("delkey", delkey_command))
    app.add_handler(CommandHandler("switchkey", switchkey_command))
    # Dot-prefixed Voice Chat controls are deliberately private-chat only.
    app.add_handler(build_voice_chat_handler())
    app.add_handler(CallbackQueryHandler(update_control_callback, pattern=r"^self_update:"))
