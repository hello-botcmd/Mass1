import asyncio
import re
import random
import logging
from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)
from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import PeerChannel
from database import db
from config import API_ID, API_HASH

logger = logging.getLogger(__name__)

# States
REACT_LINK, REACT_COUNT, REACT_EMOJI = range(30, 33)

_react_stop = asyncio.Event()

LINK_PATTERN = re.compile(
    r"https?://t\.me/(?:c/(\d+)|([^/]+))/(\d+)"
)


def parse_post_link(link: str):
    """Parse a Telegram post link."""
    m = LINK_PATTERN.match(link.strip())
    if not m:
        return None, None
    channel_id_str, username, msg_id_str = m.groups()
    msg_id = int(msg_id_str)
    if channel_id_str:
        return int(channel_id_str), msg_id
    else:
        return username, msg_id


async def reactions_start(update: Update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "❤️ *Reaction Booster*\n\n"
        "Send the post link.\n"
        "Examples:\n"
        "• `https://t.me/username/1234`\n"
        "• `https://t.me/c/1234567890/1234`\n\n"
        "**Important:** Accounts must already be members of the channel.\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown"
    )
    return REACT_LINK


async def react_receive_link(update: Update, context):
    link = update.message.text.strip()
    identifier, msg_id = parse_post_link(link)

    if identifier is None or msg_id is None:
        await update.message.reply_text(
            "❌ Invalid post link.\n"
            "Examples:\n"
            "• `https://t.me/username/1234`\n"
            "• `https://t.me/c/1234567890/1234`",
            parse_mode="Markdown"
        )
        return REACT_LINK

    context.user_data["react_identifier"] = identifier
    context.user_data["react_msg_id"] = msg_id
    context.user_data["react_link"] = link

    await update.message.reply_text(
        f"✅ Post: `{link}`\n\n"
        "How many reactions should be sent?",
        parse_mode="Markdown"
    )
    return REACT_COUNT


async def react_receive_count(update: Update, context):
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text("❌ Enter a valid number.\n\n/cancel to abort.")
        return REACT_COUNT
    context.user_data["react_count"] = int(text)

    await update.message.reply_text(
        f"✅ Count: {int(text)}\n\n"
        "Send reaction emoji(s) separated by spaces.\n"
        "Example: `❤️ 🔥 👍`\n\n"
        "/cancel to abort.",
        parse_mode="Markdown"
    )
    return REACT_EMOJI


async def react_receive_emoji(update: Update, context):
    text = update.message.text.strip()
    emojis = text.split()
    if not emojis:
        await update.message.reply_text("❌ Send at least one emoji.")
        return REACT_EMOJI

    identifier = context.user_data["react_identifier"]
    msg_id = context.user_data["react_msg_id"]
    count = context.user_data["react_count"]
    link = context.user_data.get("react_link", "")

    status_msg = await update.message.reply_text(
        f"🚀 Starting {count} reactions on `{link}`...\n"
        f"Use /stop to abort.",
        parse_mode="Markdown"
    )

    global _react_stop
    _react_stop.clear()
    await db.set_process_running("reactions", True)

    context.application.create_task(
        _execute_reactions(update, context, identifier, msg_id, count, emojis, status_msg)
    )

    context.user_data.clear()
    return ConversationHandler.END


async def _execute_reactions(update, context, identifier, msg_id, count, emojis, status_msg):
    """Background task for reactions — fresh client per account."""
    from database import db as db_
    all_accounts = await db_.get_all_accounts()
    available = [a for a in all_accounts if not a.get("currently_busy", False)]
    random.shuffle(available)

    if len(available) < count:
        await status_msg.edit_text(f"❌ Only {len(available)} accounts available. Need {count}.")
        await db_.set_process_running("reactions", False)
        return

    selected = available[:count]
    success = 0
    fail = 0
    not_in_chat = []
    errors = []

    for idx, acc in enumerate(selected):
        if _react_stop.is_set():
            await status_msg.edit_text(
                f"⏹ Stopped. Done: {idx}/{count} | ✅ {success} ❌ {fail}",
                parse_mode="Markdown"
            )
            await db_.set_process_running("reactions", False)
            return

        phone = acc["_id"]
        ss = acc["session_string"]
        reaction_emoji = random.choice(emojis)

        try:
            # Fresh client per operation
            session = StringSession(ss)
            async with TelegramClient(session, API_ID, API_HASH) as client:
                await client.connect()
                if not await client.is_user_authorized():
                    fail += 1
                    continue

                # Resolve peer
                try:
                    if isinstance(identifier, int):
                        resolved = await client.get_entity(PeerChannel(identifier))
                    else:
                        resolved = await client.get_entity(identifier)
                except Exception:
                    fail += 1
                    not_in_chat.append(phone)
                    continue

                # Send reaction
                await client(functions.messages.SendReactionRequest(
                    peer=resolved,
                    msg_id=msg_id,
                    reaction=[types.ReactionEmoji(emoticon=reaction_emoji)]
                ))
                success += 1
                logger.info(f"✅ {phone} reacted {reaction_emoji} on msg {msg_id}")

        except FloodWaitError as e:
            logger.warning(f"Flood on {phone}: wait {e.seconds}s")
            fail += 1
            await asyncio.sleep(min(e.seconds, 5))
        except Exception as e:
            logger.error(f"Reaction error {phone}: {e}")
            fail += 1
            errors.append(f"{phone}: {str(e)[:80]}")

        # Polite gap
        await asyncio.sleep(random.uniform(1.5, 3))

        if (idx + 1) % 5 == 0 or idx == count - 1:
            try:
                await status_msg.edit_text(
                    f"🔄 {idx + 1}/{count} | ✅ {success} ❌ {fail}",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    await db_.set_process_running("reactions", False)

    summary = (
        f"🎉 *Reactions Complete!*\n"
        f"• ✅ Sent: {success}\n"
        f"• ❌ Failed: {fail}\n"
    )
    if not_in_chat:
        summary += f"• 👻 Not in chat: {len(not_in_chat)}\n"
    if errors and fail > 0:
        summary += "\nSample errors:\n" + "\n".join(f"• {e}" for e in errors[:3])

    try:
        await status_msg.edit_text(summary, parse_mode="Markdown")
    except Exception:
        await context.bot.send_message(update.effective_chat.id, summary, parse_mode="Markdown")


async def stop_reactions():
    global _react_stop
    _react_stop.set()


async def cancel(update: Update, context):
    await update.message.reply_text("❌ Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


def get_reactions_handler():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(reactions_start, pattern="^reactions$")],
        states={
            REACT_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, react_receive_link)],
            REACT_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, react_receive_count)],
            REACT_EMOJI: [MessageHandler(filters.TEXT & ~filters.COMMAND, react_receive_emoji)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
  )
