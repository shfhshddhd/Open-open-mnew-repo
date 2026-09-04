
# 🎭 CipherElite Plugin Development Guide

CipherElite has **two kinds of plugins**:

1. **Normal plugins** — userbot replies directly in the chat (`.reverse`, `.upper`, most commands).
2. **Inline plugins** — userbot sends the message *through the assistant bot* using Telegram inline mode, then hides the "via @bot" tag so it looks native (`.alive`, `.ping`).

Both live in `plugins/*.py` and are auto-loaded on startup — you never register a plugin manually anywhere else.

---

## 1️⃣ Normal Plugin (direct reply)

### Basic Structure

```python
from telethon import events
from utils.utils import CipherElite
from utils.decorators import rishabh
from plugins.bot import add_handler

def init(client_instance):
    commands = [
        ".command <param> - Description of command"
    ]
    description = "🎭 Plugin Name - Brief description"
    add_handler("plugin_name", commands, description)

async def register_commands():
    @CipherElite.on(events.NewMessage(pattern=r"\.command\s+(.+)"))
    @rishabh()
    async def command_handler(event):
        try:
            param = event.pattern_match.group(1).strip()
            await event.reply("✅ **Success!**")
        except Exception as e:
            await event.reply(f"❌ **Error:** {str(e)}")
```

### Required Components

**Imports**
```python
from telethon import events
from utils.utils import CipherElite       # the userbot client
from utils.decorators import rishabh      # access control
from plugins.bot import add_handler       # registers plugin in .help menu
```

**`init()`** — runs once at startup, registers the plugin so it shows up in `.help`:
```python
def init(client_instance):
    commands = [
        ".cmd <param> - Description"   # full syntax with parameters
    ]
    description = "🎭 Plugin - What it does"
    add_handler("short_name", commands, description)   # keep the name short
```

**`register_commands()`** — must be `async`, this is where you attach the actual `@CipherElite.on(...)` handlers:
```python
async def register_commands():
    @CipherElite.on(events.NewMessage(pattern=r"\.cmd\s+(.+)"))
    @rishabh()
    async def handler(event):
        try:
            await event.reply("🎭 **Cipher Elite Result**\n\n✅ Success")
        except Exception as e:
            await event.reply(f"❌ **Error:** {str(e)}")
```

> The loader (`startup/startup.py`) does exactly this for every file in `plugins/`:
> `module.init(client)` → `await module.register_commands()`. If either is missing/throws, only that plugin fails to load — the rest of the bot keeps running.

---

## 2️⃣ Inline Plugin (userbot + assistant bot combo)

Used when you want the message to look like it came straight from the userbot with rich media/buttons, but you're actually letting the **assistant bot** build it via Telegram's inline mode. This is the `.alive` / `.ping` pattern.

### How the flow works

```
.alive typed  ──▶  userbot builds text/media
                    │
                    ▼
        stores it in a global INLINE_DATA dict
                    │
                    ▼
   userbot calls event.client.inline_query(BOT_USERNAME, "alive")
                    │
                    ▼
     assistant bot's @bot.on(events.InlineQuery) handler
     reads INLINE_DATA and returns a photo/article result
                    │
                    ▼
   userbot does results[0].click(chat_id, hide_via=True)
     → sends it as if typed directly (no "via @bot" tag)
                    │
                    ▼
        original ".alive" trigger message is deleted
```

If the assistant bot is offline / not configured, the handler **must** fall back to a plain `event.reply(...)` so the command still works.

### Template

```python
from telethon import events, Button
from plugins.bot import add_handler, bot          # bot = the assistant TelegramClient
from utils.utils import CipherElite
from utils.decorators import rishabh
from config.config import Config

VERSION = "1.0.0"
CATEGORY = "utilities"

# Bridge: userbot writes here, assistant bot reads from here
INLINE_DATA = {
    "mycmd_text": "Hello from Cipher Elite",
    "mycmd_media": None,
}

BUTTONS = [[Button.url("💬 Support", "https://t.me/cipherelite_support")]]


def init(client_instance):
    commands = [".mycmd - Inline example command"]
    add_handler("mycmd", commands, "🎭 My inline command")


async def register_commands():
    # ---- USERBOT SIDE: trigger ----
    @CipherElite.on(events.NewMessage(pattern=r"\.mycmd"))
    @rishabh()
    async def mycmd(event):
        text = f"Hello {event.sender.first_name}, this is inline!"

        global INLINE_DATA
        INLINE_DATA["mycmd_text"] = text
        INLINE_DATA["mycmd_media"] = None   # or a file/URL for a photo result

        try:
            results = await event.client.inline_query(Config.TG_BOT_USERNAME, "mycmd")
            await results[0].click(
                event.chat_id,
                reply_to=event.reply_to_msg_id,
                hide_via=True
            )
            await event.delete()
        except Exception:
            # Fallback: bot unavailable, just reply normally
            await event.reply(text, file=INLINE_DATA["mycmd_media"], parse_mode='html')

    # ---- BOT SIDE: builds the inline result ----
    if bot:
        @bot.on(events.InlineQuery(pattern=r"^mycmd$"))
        async def inline_mycmd(event):
            builder = event.builder
            text = INLINE_DATA["mycmd_text"]
            media = INLINE_DATA["mycmd_media"]

            if media:
                result = builder.photo(media, text=text, parse_mode='html', buttons=BUTTONS)
            else:
                result = builder.article("My Command", text=text, parse_mode='html', buttons=BUTTONS)

            await event.answer([result], cache_time=1)
```

### Key rules for inline plugins
- `INLINE_DATA` keys must be **unique per plugin** — don't reuse `"alive_text"` etc. from other plugins.
- The bot-side `InlineQuery` pattern (e.g. `r"^mycmd$"`) must exactly match the string passed to `event.client.inline_query(Config.TG_BOT_USERNAME, "mycmd")`.
- Always wrap the inline trigger in `try/except` with a plain-text fallback — the assistant bot may be down, unset, or not @-mentionable yet.
- Only build the `@bot.on(...)` handler `if bot:` — `bot` can be `None` if `plugins/bot.py` failed to init the assistant client.
- `.help` itself doesn't follow this per-plugin pattern — it's handled centrally by the catch-all `@bot.on(events.InlineQuery)` in `plugins/bot.py`, so you don't need to touch that for a normal inline plugin.

---

## 🔐 Access Control Decorators

Pick the right one from `utils/decorators.py`:

| Decorator | Who can use it | Behavior on deny |
|---|---|---|
| `@rishabh()` | Owner + sudo users only | Silently ignores the command (no reply) |
| `@rishabh_help()` | Owner + sudo users only | Replies/alerts with an access-denied message — use for callback/inline handlers |
| `@authorized_users_only()` | Owner/sudo, **or** group admins, **or** anyone in a private chat | Sends a "🎭 Access Denied" message |

```python
@CipherElite.on(events.NewMessage(pattern=r"\.ban"))
@authorized_users_only()   # group admins can use this one too
async def ban_handler(event):
    ...
```

> Admin checks are cached for 5 minutes per (chat, user) to keep group commands fast — you don't need to do anything extra for this, it's automatic in the decorator.

---

## Pattern Examples

```python
# Basic command
pattern=r"\.command"

# Required parameter
pattern=r"\.command\s+(.+)"

# Optional parameter
pattern=r"\.command\s*(.*)"

# Multiple parameters
pattern=r"\.command\s+(\w+)\s*(.*)"

# Exact match only (recommended to avoid clashing with similarly-named commands)
pattern=r"^\.command$"
```

⚠️ **Avoid command collisions**: two plugins registering the exact same pattern will *both* fire and double-reply. Run `python3 scan_conflicts2.py` from the project root before committing a new plugin to check for clashes with existing commands.

---

## Message Formatting

```python
# Success message
await event.reply("🎭 **Cipher Elite Success**\n\n"
                 "✅ **Result:** Your result here\n"
                 "🤖 **Powered by Cipher Elite**")

# Error message
await event.reply(f"🎭 **Cipher Elite Error**\n\n"
                 f"❌ **Error:** {str(e)}\n"
                 f"💡 **Try again with correct parameters**")

# Status updates
status = await event.reply("🔄 **Processing...**")
await status.edit("✅ **Complete!**")
```

---

## Complete Example — Normal Plugin

```python
from telethon import events
from utils.utils import CipherElite
from utils.decorators import rishabh
from plugins.bot import add_handler

def init(client_instance):
    commands = [
        ".reverse <text> - Reverse text with Cipher Elite",
        ".upper <text> - Convert text to uppercase"
    ]
    description = "🎭 Text Tools - Basic text manipulation"
    add_handler("texttools", commands, description)

async def register_commands():
    @CipherElite.on(events.NewMessage(pattern=r"\.reverse\s+(.+)"))
    @rishabh()
    async def reverse_text(event):
        try:
            text = event.pattern_match.group(1).strip()
            result = text[::-1]

            await event.reply("🎭 **Cipher Elite Text Reverser**\n\n"
                            f"📝 **Original:** `{text}`\n"
                            f"🔄 **Reversed:** `{result}`\n"
                            f"✅ **Success!**")
        except Exception as e:
            await event.reply(f"❌ **Error:** {str(e)}")

    @CipherElite.on(events.NewMessage(pattern=r"\.upper\s+(.+)"))
    @rishabh()
    async def upper_text(event):
        try:
            text = event.pattern_match.group(1).strip()
            result = text.upper()

            await event.reply(f"🎭 **Uppercase Result**\n\n`{result}`")
        except Exception as e:
            await event.reply(f"❌ **Error:** {str(e)}")
```

## Complete Example — Inline Plugin

See the full `.mycmd` template in section 2️⃣ above — it's copy-paste ready. For a real reference implementation, read `plugins/alive.py` (`.alive`/`.ping`) end to end.

---

## Quick Checklist

### ✅ Must Have (all plugins)
- [ ] `init()` function with a `commands` list and `add_handler(...)` call
- [ ] `register_commands()` async function
- [ ] Correct decorator (`@rishabh()`, `@rishabh_help()`, or `@authorized_users_only()`)
- [ ] Try/except error handling around any logic that can fail
- [ ] Command syntax documented with `<required>` / `[optional]` parameters

### ✅ Extra for Inline Plugins
- [ ] Unique `INLINE_DATA` keys (not shared with other plugins)
- [ ] Bot-side `@bot.on(events.InlineQuery(pattern=...))` guarded with `if bot:`
- [ ] Fallback `event.reply(...)` if `inline_query()`/`.click()` throws
- [ ] `hide_via=True` on `.click()` so it doesn't show "via @yourbot"

### ✅ Best Practices
- [ ] Short plugin name for the `.help` menu button
- [ ] Cipher Elite branding in messages
- [ ] Clear parameter descriptions
- [ ] Input validation
- [ ] Ran `scan_conflicts2.py` to check for duplicate command patterns

---

## Quick Start

1. **Create file:** `plugins/myplugin.py`
2. **Pick a type:** normal (direct reply) or inline (via assistant bot)
3. **Copy the matching template above**
4. **Replace:** plugin name, commands, logic
5. **Check for conflicts:** `python3 scan_conflicts2.py`
6. **Test:** restart the bot, use `.help myplugin`
7. **Deploy:** commands work automatically — no manual registration needed anywhere else

Your plugin will appear in the `.help` menu and support direct access via `.help myplugin`!
