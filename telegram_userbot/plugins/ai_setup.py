# =============================================================================
#  FLEX FUCKER USERBOT Userbot Plugin - AI Setup Manager
#
#  Plugin Name:    ai_setup
#  Version:        1.0.0
#  Author:         FLEX FUCKER USERBOT Dev ()
#  Repository:     
#
#  LICENSE:        MIT
# =============================================================================

import os
import json
import asyncio
from pathlib import Path
from datetime import datetime
from telethon import events
from config.config import Config
import database.mongo as db
from utils.utils import CipherElite
from utils.decorators import rishabh
from plugins.bot import add_handler
from utils.gemini_rotation import add_key, get_keys, remove_key
from utils.ai_provider import ai_status_for_user

VERSION = "1.0.0"
CATEGORY = "utilities"

# Centralized AI config file
PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "DB"
CONFIG_DIR.mkdir(exist_ok=True)
AI_CONFIG_FILE = CONFIG_DIR / "ai_config.json"


class AIConfigManager:
    """Centralized manager for all AI API keys and settings"""
    
    def __init__(self):
        self.config = {
            "gemini_api_key": None,
            "ai_enabled": False,
            "last_updated": None
        }
        self._load()
    
    def _load(self):
        """Load config from file or environment"""
        try:
            if AI_CONFIG_FILE.exists():
                with open(AI_CONFIG_FILE, 'r') as f:
                    on_disk = json.load(f)
                    self.config.update(on_disk)
        except Exception as e:
            print(f"⚠️ AI Config load error: {e}")
        
        # Fallback to environment variable
        if not self.config["gemini_api_key"]:
            env_key = os.environ.get("GEMINI_API_KEY")
            if env_key:
                self.config["gemini_api_key"] = env_key
                self.config["ai_enabled"] = True
                self._save()
    
    def _save(self):
        """Save config to file"""
        try:
            with open(AI_CONFIG_FILE, 'w') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            print(f"⚠️ AI Config save error: {e}")
    
    def set_api_key(self, key):
        """Set Gemini API key"""
        previous_key = self.config.get("gemini_api_key")
        normalized = key.strip() if key else None
        self.config["gemini_api_key"] = normalized
        self.config["ai_enabled"] = bool(key)
        self.config["last_updated"] = datetime.now().isoformat()
        self._save()
        if normalized:
            add_key(normalized)
        elif previous_key:
            remove_key(previous_key)
    
    def get_api_key(self):
        """Get Gemini API key"""
        keys = get_keys()
        return keys[0] if keys else self.config.get("gemini_api_key")
    
    def is_enabled(self):
        """Check if AI is enabled"""
        return bool(get_keys() or self.config.get("gemini_api_key"))


# Global instance
ai_config = AIConfigManager()


async def _require_bot_owner(event) -> bool:
    """Protect shared AI configuration from every hosted non-owner."""
    if Config.OWNER_ID and event.sender_id == Config.OWNER_ID:
        return True
    await event.reply("⛔ Aapko is command ka access nahi hai.")
    return False


def init(client):
    """Initialize AI Setup plugin"""
    commands = [
        ".setai <key>      — Add a Gemini API key",
        ".rmai             — Remove the configured Gemini key",
        ".aistatus         — Show AI configuration status"
    ]
    add_handler("ai_setup", commands, "AI Configuration Manager")
    
    @CipherElite.on(events.NewMessage(outgoing=True, pattern=r"\.setai(?:\s+(.+))?$"))
    @rishabh()
    async def _setai(event):
        """Set Gemini API key"""
        if not await _require_bot_owner(event):
            return
        key = event.pattern_match.group(1)
        if not key:
            return await event.reply(
                "❌ **Usage:** `.setai <your_gemini_api_key>`\n\n"
                "📋 Get your key from: https://aistudio.google.com/"
            )
        
        ai_config.set_api_key(key.strip())
        msg = await event.reply(
            "✅ **Gemini API key saved!**\n\n"
            "🤖 Gemini is now available for AI plugins.\n"
            "🌐 Use `.ai provider openrouter` for the separate OpenRouter key list.\n"
            "💬 Use `.ai <question>` in FLEX AI\n"
            "🛡️ Group mention AI mode remains independently controlled"
        )
        
        # Auto-delete after 5 seconds
        await asyncio.sleep(5)
        try:
            await event.delete()
            await msg.delete()
        except:
            pass
    
    @CipherElite.on(events.NewMessage(outgoing=True, pattern=r"\.rmai$"))
    @rishabh()
    async def _rmai(event):
        """Remove AI key"""
        if not await _require_bot_owner(event):
            return
        ai_config.set_api_key(None)
        msg = await event.reply(
            "🛑 **Configured Gemini key removed!**\n\n"
            "OpenRouter keys and provider selection are unchanged."
        )
        
        await asyncio.sleep(5)
        try:
            await event.delete()
            await msg.delete()
        except:
            pass
    
    @CipherElite.on(events.NewMessage(outgoing=True, pattern=r"\.aistatus$"))
    @rishabh()
    async def _aistatus(event):
        """Show AI status"""
        if not await _require_bot_owner(event):
            return
        userbot = getattr(event.client, "_userbot_context", None)
        user_id = getattr(userbot, "user_id", None)
        if user_id is None:
            await event.reply("❌ Hosted account context is unavailable.")
            return
        enabled = bool(await db.get_setting(user_id, "ai_mode", False))
        provider_status = await ai_status_for_user(user_id, enabled=enabled)
        key_status = (
            "✅ **Enabled**"
            if provider_status["active_count"]
            else "❌ **No active key**"
        )
        
        status_msg = f"""📊 **AI Configuration Status:**

🔑 **Keys:** {key_status}
🤖 **Provider:** {provider_status["provider"].title()}
🔢 **Active keys:** {provider_status["active_count"]}/{provider_status["total_count"]}
🔄 **Rotation:** {provider_status["rotation"]}
🟢 **AI mode:** {"ON" if enabled else "OFF"}
⚙️ **Used By:** FLEX AI and group mention mode

📝 **Commands:**
• `.setai <key>` - Add a Gemini API key
• `.addopenrouterkey <key>` - Add an OpenRouter API key
• `.ai provider gemini|openrouter` - Select provider
• `.switchkey [provider] <number>` - Choose the first key
• `.rmai` - Remove API key
• `.aistatus` - Show this status

🔗 **Get API Key:** https://aistudio.google.com/

💡 **Note:** Set the API key once and it works across all AI plugins!"""
        
        await event.reply(status_msg)
    
    print("✅ AI Setup Plugin initialized")
    return ai_config
