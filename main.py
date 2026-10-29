import asyncio
import json
import logging
import os
import re
import feedparser
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ==================== CONFIGURATION ====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEFAULT_SUBREDDIT = os.getenv("DEFAULT_SUBREDDIT", "homelabsales")
# Reddit-compliant descriptive User-Agent
HEADERS = {
    "User-Agent": "linux:reddit.rss.monitor:v3.0 (by /u/script_user)"
}
STATE_FILE = "bot_state.json"
SUBREDDIT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,20}$")
# =======================================================

# Enable logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

if not TELEGRAM_BOT_TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN environment variable is missing! Set it before running the script.")
    exit(1)

def valid_subreddit(value):
    """Returns a safe subreddit name, falling back to the default."""
    if isinstance(value, str) and SUBREDDIT_PATTERN.fullmatch(value):
        return value
    return "homelabsales"

def load_state():
    """Loads bot state from a local JSON file to persist across restarts."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return {
                    "target_chat_id": data.get("target_chat_id"),
                    "seen_ids": set(data.get("seen_ids", [])),
                    "subreddit": valid_subreddit(data.get("subreddit", DEFAULT_SUBREDDIT)),
                    "is_running": data.get("is_running", True)
                }
        except Exception as e:
            logger.error(f"Error loading state file: {e}")
            
    return {
        "target_chat_id": None,
        "seen_ids": set(),
        "subreddit": valid_subreddit(DEFAULT_SUBREDDIT),
        "is_running": True
    }

def save_state():
    """Saves the current target chat id and running state to disk."""
    try:
        data = {
            "target_chat_id": bot_state["target_chat_id"],
            "seen_ids": list(bot_state["seen_ids"]),
            "subreddit": bot_state["subreddit"],
            "is_running": bot_state["is_running"]
        }
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Error saving state file: {e}")

bot_state = load_state()

def get_main_keyboard():
    """Generates the interactive control panel markup."""
    keyboard = [[InlineKeyboardButton("📊 Check Status", callback_data="status")]]
    return InlineKeyboardMarkup(keyboard)

async def safe_edit_message(query, text, reply_markup=None):
    """Safely edits a message, swallowing errors if the content hasn't changed."""
    try:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    except BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Failed to edit message: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Captures the working chat ID and displays the control panel."""
    chat = update.effective_chat
    if not chat:
        logger.warning("Received /start without an effective chat.")
        return

    chat_id = chat.id
    bot_state["target_chat_id"] = chat_id
    save_state()
    
    welcome_text = (
        "🤖 *Reddit [FS] Monitor Initialized!*\n\n"
        f"📍 *Working Chat Captured:* `{chat_id}`\n"
        f"📣 *Chat Type:* `{chat.type}`\n"
        f"You will receive new selling posts from r/{bot_state['subreddit']}.\n\n"
        "Use `/subreddit name` to select another subreddit.\n"
        "Use `/chatid` to display this chat ID again."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=get_main_keyboard())

async def chat_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays and registers the current private or group chat."""
    chat = update.effective_chat
    if not chat or not update.message:
        logger.warning("Received /chatid without an effective chat.")
        return

    bot_state["target_chat_id"] = chat.id
    save_state()
    await update.message.reply_text(
        f"Active chat ID: `{chat.id}`\n"
        f"Chat type: `{chat.type}`\n"
        f"Polling r/{bot_state['subreddit']} every 30 seconds.",
        parse_mode="Markdown",
    )

async def subreddit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Selects the subreddit to poll for the current target chat."""
    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            f"Usage: `/subreddit name`\nCurrently selected: r/{bot_state['subreddit']}",
            parse_mode="Markdown",
        )
        return

    subreddit = context.args[0].strip()
    if not SUBREDDIT_PATTERN.fullmatch(subreddit):
        await update.message.reply_text(
            "Invalid subreddit name. Use 2-21 letters, numbers, underscores, or hyphens."
        )
        return

    chat = update.effective_chat
    if not chat:
        logger.warning("Received subreddit command without an effective chat.")
        return

    bot_state["target_chat_id"] = chat.id
    bot_state["subreddit"] = subreddit
    bot_state["seen_ids"].clear()
    save_state()
    await update.message.reply_text(
        f"Now polling r/{subreddit} every 30 seconds for [FS] posts.",
        reply_markup=get_main_keyboard(),
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline button clicks."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "status":
        chat_status = f"`{bot_state['target_chat_id']}`" if bot_state['target_chat_id'] else "⚠️ None"
        status_msg = (
            "📊 *Bot Status Report*\n\n"
            f"• *Active:* {'🟢 Running' if bot_state['is_running'] else '🔴 Stopped'}\n"
            f"• *Subreddit:* `r/{bot_state['subreddit']}`\n"
            f"• *Target Chat ID:* {chat_status}\n"
            f"• *Cached Post IDs:* {len(bot_state['seen_ids'])}"
        )
        await safe_edit_message(query, status_msg, reply_markup=get_main_keyboard())

    elif data == "back_to_menu":
        await safe_edit_message(query, "🛠 *Control Panel:*", reply_markup=get_main_keyboard())

async def check_reddit_rss(context: ContextTypes.DEFAULT_TYPE):
    """Fetches new subreddit posts and alerts on [FS] listings."""
    if not bot_state["is_running"] or not bot_state["target_chat_id"]:
        return

    try:
        subreddit = bot_state["subreddit"]
        rss_url = f"https://www.reddit.com/r/{subreddit}/new.rss"
        response = await asyncio.to_thread(
            requests.get, rss_url, headers=HEADERS, timeout=15
        )
        
        if response.status_code != 200:
            logger.warning(f"Reddit RSS returned status code {response.status_code}")
            return

        feed = feedparser.parse(response.text)
        if not feed or not feed.entries:
            return

        for entry in reversed(feed.entries):
            post_id = entry.get("id", entry.get("link"))
            
            if post_id not in bot_state["seen_ids"]:
                if not post_id:
                    continue
                bot_state["seen_ids"].add(post_id)

                if len(bot_state["seen_ids"]) > 5000:
                    bot_state["seen_ids"].pop()

                title = entry.get("title", "")
                if not title.lstrip().lower().startswith("[fs]"):
                    continue

                link = entry.get("link", "")
                author = entry.get("author", "Unknown")

                alert_text = (
                    "REDDIT LISTING\n\n"
                    f"Title: {title}\n"
                    f"Author: {author}\n"
                    f"{link}"
                )

                await context.bot.send_message(
                    chat_id=bot_state["target_chat_id"],
                    text=alert_text,
                    disable_web_page_preview=True
                )
    except Exception as e:
        logger.error(f"Error fetching r/{bot_state['subreddit']} RSS feed: {e}")
    finally:
        save_state()

def main():
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler(["sub", "subreddit"], subreddit_command))
    application.add_handler(CommandHandler("chatid", chat_id_command))
    application.add_handler(CallbackQueryHandler(button_handler))

    job_queue = application.job_queue
    # Checks the selected subreddit every 30 seconds.
    job_queue.run_repeating(check_reddit_rss, interval=30, first=5)

    print(f"[*] r/{bot_state['subreddit']} [FS] RSS monitor running every 30 seconds.")
    # Discard updates accumulated while the bot was stopped. Only one process
    # using this token may call getUpdates at a time.
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()