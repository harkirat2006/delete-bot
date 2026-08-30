import logging
import os

# Try to load a local .env file for development if python-dotenv is installed.
try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv():
        return

from pymongo import MongoClient
from pymongo.errors import PyMongoError
from telegram import Chat, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Persistence: MongoDB collection "blocked_channels"
# One document per (chat_id, channel_id) pair:
#   { chat_id: "<chat_id>", channel_id: "<channel_id>", username: "<username or None>" }
# --------------------------------------------------------------------------
# Load environment from .env for local development before reading variables
load_dotenv()

MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB = os.environ.get("MONGODB_DB", "channel_blocker_bot")

try:
    mongo_client = MongoClient(MONGODB_URI)
    db = mongo_client[MONGODB_DB]
    blocked_collection = db["blocked_channels"]

    # Ensure lookups are fast and each (chat, channel) pair is unique.
    try:
        blocked_collection.create_index([("chat_id", 1), ("channel_id", 1)], unique=True)
    except Exception as e:  # PyMongoError or network errors
        logger.warning("Could not create indexes on blocked_channels: %s", e)
except Exception as e:
    logger.warning("Could not connect to MongoDB (%s). Using in-memory fallback.", e)

    class _InMemoryCollection:
        def __init__(self):
            # store mapping (chat_id, channel_id) -> username
            self.store = {}

        def update_one(self, filter, update, upsert=False):
            chat_id = filter.get("chat_id")
            channel_id = filter.get("channel_id")
            username = update.get("$set", {}).get("username")
            self.store[(chat_id, channel_id)] = username

        def delete_one(self, filter):
            key = (filter.get("chat_id"), filter.get("channel_id"))
            class _R:
                deleted_count = 0
            res = _R()
            if key in self.store:
                del self.store[key]
                res.deleted_count = 1
            return res

        def find(self, filter):
            chat_id = filter.get("chat_id")
            for (c, cid), uname in list(self.store.items()):
                if c == chat_id:
                    yield {"chat_id": c, "channel_id": cid, "username": uname}

        def find_one(self, filter):
            # support queries by channel_id or username
            chat_id = filter.get("chat_id")
            channel_id = filter.get("channel_id")
            if channel_id is not None:
                key = (chat_id, channel_id)
                if key in self.store:
                    return {"chat_id": chat_id, "channel_id": channel_id, "username": self.store[key]}
                return None
            username = filter.get("username")
            for (c, cid), uname in self.store.items():
                if c == chat_id and uname == username:
                    return {"chat_id": c, "channel_id": cid, "username": uname}
            return None

    blocked_collection = _InMemoryCollection()


def add_blocked_channel(chat_id: int, channel_id: int, username: str | None) -> None:
    blocked_collection.update_one(
        {"chat_id": str(chat_id), "channel_id": str(channel_id)},
        {"$set": {"username": username}},
        upsert=True,
    )


def remove_blocked_channel(chat_id: int, channel_id: str) -> bool:
    result = blocked_collection.delete_one(
        {"chat_id": str(chat_id), "channel_id": str(channel_id)}
    )
    return result.deleted_count > 0


def get_blocked_for_chat(chat_id: int) -> dict:
    """Returns {channel_id: username_or_None} for this chat."""
    docs = blocked_collection.find({"chat_id": str(chat_id)})
    return {doc["channel_id"]: doc.get("username") for doc in docs}


def find_blocked_by_username(chat_id: int, username: str) -> str | None:
    """Returns channel_id if a channel with this username is blocked in this chat."""
    doc = blocked_collection.find_one(
        {"chat_id": str(chat_id), "username": username}
    )
    return doc["channel_id"] if doc else None


def is_channel_blocked(chat_id: int, channel_id: int, username: str | None) -> bool:
    query = {"chat_id": str(chat_id), "channel_id": str(channel_id)}
    if blocked_collection.find_one(query):
        return True
    if username:
        return blocked_collection.find_one(
            {"chat_id": str(chat_id), "username": username}
        ) is not None
    return False


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Only allow group admins/creator to manage the blocklist."""
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == ChatType.PRIVATE:
        return True  # allow testing/config in DM with the bot
    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def resolve_channel(context: ContextTypes.DEFAULT_TYPE, identifier: str) -> Chat | None:
    """Resolve @username or numeric id to a Chat object via get_chat."""
    try:
        if identifier.lstrip("-").isdigit():
            identifier = int(identifier)
        chat = await context.bot.get_chat(identifier)
        return chat
    except Exception as e:
        logger.info("Could not resolve channel %s: %s", identifier, e)
        return None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Channel Blocker Bot\n\n"
        "Make me an admin with 'Delete messages' permission, then use:\n"
        "/block @channelusername  or  /block -100xxxxxxxxxx\n"
        "/unblock @channelusername  or  /unblock -100xxxxxxxxxx\n"
        "/blocklist\n"
    )


async def block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not await is_admin(update, context):
        await update.message.reply_text("Only group admins can use this command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /block @channelusername  or  /block -100xxxxxxxxxx")
        return

    identifier = context.args[0]
    target = await resolve_channel(context, identifier)

    if target is None or target.type != ChatType.CHANNEL:
        await update.message.reply_text(
            f"Couldn't resolve '{identifier}' as a channel. "
            "Make sure it's a valid public @username or numeric channel id, "
            "and that I (the bot) can see it (e.g. it's public, or I'm a member/admin of it)."
        )
        return

    try:
        add_blocked_channel(chat.id, target.id, target.username)
    except PyMongoError as e:
        logger.error("MongoDB error while blocking channel: %s", e)
        await update.message.reply_text("Couldn't save that — database error. Check the bot's MongoDB connection.")
        return

    label = f"@{target.username}" if target.username else str(target.id)
    await update.message.reply_text(f"Blocked channel {label} ({target.title}) in this chat.")


async def unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not await is_admin(update, context):
        await update.message.reply_text("Only group admins can use this command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /unblock @channelusername  or  /unblock -100xxxxxxxxxx")
        return

    identifier = context.args[0]
    blocked = get_blocked_for_chat(chat.id)

    removed = None
    # Try direct id match first
    key = identifier.lstrip("-")
    for cid, uname in list(blocked.items()):
        if cid.lstrip("-") == key or (uname and uname.lower() == identifier.lstrip("@").lower()):
            removed = (cid, uname)
            break

    if removed is None:
        # Fall back to resolving via Telegram in case they passed a username
        # for a channel already stored only by id, or vice versa.
        target = await resolve_channel(context, identifier)
        if target and str(target.id) in blocked:
            removed = (str(target.id), target.username)

    if removed:
        try:
            remove_blocked_channel(chat.id, removed[0])
        except PyMongoError as e:
            logger.error("MongoDB error while unblocking channel: %s", e)
            await update.message.reply_text("Couldn't update that — database error. Check the bot's MongoDB connection.")
            return
        cid, uname = removed
        label = f"@{uname}" if uname else cid
        await update.message.reply_text(f"Unblocked channel {label}.")
    else:
        await update.message.reply_text("That channel wasn't on the blocklist.")


async def blocklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    blocked = get_blocked_for_chat(chat.id)
    if not blocked:
        await update.message.reply_text("No channels are blocked in this chat.")
        return
    lines = []
    for cid, uname in blocked.items():
        lines.append(f"@{uname} (id: {cid})" if uname else f"id: {cid}")
    await update.message.reply_text("Blocked channels:\n" + "\n".join(lines))


# --------------------------------------------------------------------------
# Core moderation logic
# --------------------------------------------------------------------------
async def delete_if_blocked_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.sender_chat is None:
        return  # not a "posted as channel" message

    sender = message.sender_chat
    if sender.type != ChatType.CHANNEL:
        return  # e.g. sender_chat can also be the group itself for anonymous admins

    chat_id = update.effective_chat.id

    if is_channel_blocked(chat_id, sender.id, sender.username):
        try:
            await message.delete()
            logger.info(
                "Deleted message from blocked channel %s (%s) in chat %s",
                sender.id, sender.username, chat_id,
            )
        except Exception as e:
            logger.warning(
                "Failed to delete message from %s in chat %s: %s. "
                "Make sure the bot is an admin with delete permission.",
                sender.id, chat_id, e,
            )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Set the TELEGRAM_BOT_TOKEN environment variable to your bot token first."
        )

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("block", block))
    app.add_handler(CommandHandler("unblock", unblock))
    app.add_handler(CommandHandler("blocklist", blocklist_cmd))

    # Watch every non-command message in groups/supergroups for blocked-channel posts
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND,
            delete_if_blocked_channel,
        )
    )

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()