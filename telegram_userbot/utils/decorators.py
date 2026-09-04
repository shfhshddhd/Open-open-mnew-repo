from functools import wraps
from telethon import events  # Required to check the event type
from config.config import Config
from utils.message_ui import install_telethon_reply_style


install_telethon_reply_style()

# ==========================================
# HELPER FUNCTION
# ==========================================
async def is_owner_or_sudo(event):
    """
    Checks if the user sending the command is the deployer (Owner) 
    or a registered Sudo User.
    """
    sender_id = event.sender_id
    
    # 1. Check if the sender is the automatically extracted OWNER_ID
    if hasattr(Config, 'OWNER_ID') and sender_id == Config.OWNER_ID:
        return True
        
    # 2. Check if the sender is in the SUDO_USERS list
    if hasattr(Config, 'SUDO_USERS') and sender_id in Config.SUDO_USERS:
        return True

    # 3. Fallback: If the event is on the userbot client, me.id will match sender_id
    try:
        me = await event.client.get_me()
        if sender_id == me.id:
            return True
    except Exception:
        pass
        
    return False


async def is_bot_owner(event):
    """Return True only for the configured control-bot owner.

    This deliberately does not accept the owner of a hosted Telethon session,
    sudo users, or ``client.get_me()``.  Those identities are valid for
    per-session commands, but must never authorize shared key, update, backup,
    memory, or configuration operations.
    """
    sender_id = getattr(event, "sender_id", None)
    owner_id = getattr(Config, "OWNER_ID", 0)
    return bool(owner_id and sender_id == owner_id)


async def is_hosted_owner(event):
    """Return True only for the Telegram account running this hosted client."""
    sender_id = getattr(event, "sender_id", None)
    client = getattr(event, "client", None)
    if client is None:
        return False

    # Telethon marks messages sent by the hosted account as outgoing. This is
    # authoritative for self-messages and avoids depending on sender hydration.
    if getattr(event, "out", False):
        return True
    if sender_id is None:
        return False

    hosted_userbot = getattr(client, "_userbot_context", None)
    hosted_owner_id = getattr(hosted_userbot, "_own_id", None)
    if hosted_owner_id is not None:
        return sender_id == hosted_owner_id

    try:
        me = await client.get_me()
    except Exception:
        return False
    return sender_id == getattr(me, "id", None)

# ==========================================
# 1. ADMIN / OWNER / SUDO DECORATOR
# ==========================================
def authorized_users_only(func=None):
    def decorator(f):
        @wraps(f)
        async def wrapper(event):
            # Hosted-account commands are never authorized by private-chat
            # status or group admin rights; only the hosted account itself may
            # execute them.
            if not await is_hosted_owner(event):
                return
            return await f(event)

        return wrapper

    # Magic logic to allow both @authorized_users_only and @authorized_users_only()
    if func is None:
        return decorator
    else:
        return decorator(func)

# ==========================================
# 2. OWNER & SUDO ONLY DECORATOR (Silent Fail)
# ==========================================
def rishabh(func=None):
    def decorator(f):
        @wraps(f)
        async def wrapper(event):
            # Every hosted-account command is silent for non-owner senders.
            # The loader also installs a front-door guard for handlers that
            # do not use this decorator.
            if not await is_hosted_owner(event):
                return
            try:
                return await f(event)
            except Exception as e:
                import traceback
                print(f"❌ Exception in command {f.__name__}: {e}")
                traceback.print_exc()
                await event.reply(f"❌ **Command error in `{f.__name__}`:**\n`{e}`")
                raise
        return wrapper

    # Magic logic to allow both @rishabh and @rishabh()
    if func is None:
        return decorator
    else:
        return decorator(func)

# ==========================================
# 3. OWNER & SUDO ONLY DECORATOR (With Alert)
# ==========================================
def rishabh_help(func=None):
    def decorator(f):
        @wraps(f)
        async def wrapper(event):
            
            if not await is_hosted_owner(event):
                return
            
            try:
                return await f(event)
            except Exception as e:
                import traceback
                print(f"❌ Exception in command {f.__name__}: {e}")
                traceback.print_exc()
                if isinstance(event, events.CallbackQuery.Event):
                    await event.answer(f"❌ Error in {f.__name__}: {e}", alert=True)
                elif isinstance(event, events.InlineQuery.Event):
                    await event.answer([])
                else:
                    await event.reply(f"❌ **Command error in `{f.__name__}`:**\n`{e}`")
                raise
        return wrapper

    # Magic logic to allow both @rishabh_help and @rishabh_help()
    if func is None:
        return decorator
    else:
        return decorator(func)

