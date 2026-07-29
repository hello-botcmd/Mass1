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
from telethon import types
from telethon.tl.functions.messages import SendReactionRequest
from telethon.errors import RPCError
from database import db
from client_manager import client_manager

logger = logging.getLogger(__name__)

# States
REACT_LINK, REACT_COUNT, REACT_EMOJI = range(30, 33)

_react_stop = asyncio.Event()


async def reactions_start(update: Update, context):
    """Start reaction flow."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "❤️ *Reaction Booster*\n\n"
        "Send the post link.\n"
        "Examples:\n"
        "• `https://t.me/username/1234`\n"
        "• `https://t.me/c/1234567890/1234`\n\n"
        "**Important:** Accounts must already be members of the channel/group to react.\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown"
    )
    return REACT_LINK


def parse_post_link(link: str):
    """Parse Telegram post link into (peer_identifier, message_id)."""
    pattern = r"https://t\.me/(?:c/)?([^/]+)/(\d+)"
    match = re.match(pattern, link.strip())
    if not match:
        return None, None
    identifier = match.group(1)
    msg_id = int(match.group(2))
    return identifier, msg_id


async def react_receive_link(update: Update, context):
    """Receive the post link."""
    link = update.message.text.strip()
    identifier, msg_id = parse_post_link(link)

    if not identifier or not msg_id:
        await update.message.reply_text(
            "❌ Invalid post link. Please send a valid Telegram post link.\n"
            "Example: `https://t.me/username/1234`\n\n"
            "Or /cancel to abort.",
            parse_mode="Markdown"
        )
        return REACT_LINK

    context.user_data["react_identifier"] = identifier
    context.user_data["react_msg_id"] = msg_id
    context.user_data["react_link"] = link

    await update.message.reply_text(
        f"✅ Post: `{link}`\n"
        f"   Message ID: `{msg_id}`\n\n"
        "How many reactions should be sent?",
        parse_mode="Markdown"
    )
    return REACT_COUNT


async def react_receive_count(update: Update, context):
    """Receive reaction count."""
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text("❌ Please send a valid positive number.\n\nOr /cancel to abort.")
        return REACT_COUNT

    count = int(text)
    context.user_data["react_count"] = count

    await update.message.reply_text(
        f"✅ Count: {count}\n\n"
        "Send the reaction emoji(s) separated by spaces.\n"
        "Example: `❤️ 🔥 👍`\n\n"
        "Or /cancel to abort.",
        parse_mode="Markdown"
    )
    return REACT_EMOJI


async def react_receive_emoji(update: Update, context):
    """Receive the emoji(s) and start reacting."""
    text = update.message.text.strip()
    emojis = text.split()
    if not emojis:
        await update.message.reply_text("❌ Please send at least one emoji.\n\nOr /cancel to abort.")
        return REACT_EMOJI

    identifier = context.user_data["react_identifier"]
    msg_id = context.user_data["react_msg_id"]
    count = context.user_data["react_count"]
    link = context.user_data.get("react_link", "")

    status_msg = await update.message.reply_text(
        f"🚀 Starting reactions...\n\n"
        f"Post: `{link}`\n"
        f"Count: {count}\n"
        f"Reactions: {' '.join(emojis)}\n"
        f"Gap: 2s between each\n\n"
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
    """Background task for sending reactions."""
    all_accounts = await db.get_all_accounts()
    available = [a for a in all_accounts if not a.get("currently_busy", False)]
    random.shuffle(available)

    if len(available) < count:
        await status_msg.edit_text(
            f"❌ Only {len(available)} accounts available. Need {count}.\n"
            f"Please add more accounts or reduce the count.",
            parse_mode="Markdown"
        )
        await db.set_process_running("reactions", False)
        return

    selected = available[:count]
    success = 0
    fail = 0
    errors = []

    for idx, acc in enumerate(selected):
        if _react_stop.is_set():
            await status_msg.edit_text(
                f"⏹ *Reaction process stopped.*\n\n"
                f"Completed: {idx}/{count}\n"
                f"✅ Success: {success} | ❌ Failed: {fail}",
                parse_mode="Markdown"
            )
            await db.set_process_running("reactions", False)
            return

        phone = acc["_id"]
        try:
            client = await client_manager.get_client(phone)

            # Come online first
            await client_manager.set_online(phone)

            # CRITICAL: Resolve the peer entity properly
            # The account MUST be a member of this channel/group
            try:
                entity = await client.get_entity(identifier)
            except Exception as e:
                logger.warning(f"{phone}: Cannot resolve entity {identifier}: {e}")
                fail += 1
                if idx < count - 1:
                    await asyncio.sleep(2)
                continue

            # Pick a random emoji
            emoji = random.choice(emojis)

            # Send reaction using proper ReactionEmoji format
            await client(SendReactionRequest(
                peer=entity,
                msg_id=msg_id,
                reaction=[types.ReactionEmoji(emoticon=emoji)]
            ))
            success += 1
            logger.info(f"{phone} reacted with {emoji} on {identifier}/{msg_id}")

        except RPCError as e:
            logger.error(f"RPC error for {phone}: {e}")
            fail += 1
            if "FLOOD" in str(e):
                errors.append(f"{phone}: Flood wait - {e}")
                # Longer sleep on flood
                if idx < count - 1:
                    await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Reaction error for {phone}: {e}")
            fail += 1
            errors.append(f"{phone}: {str(e)[:100]}")

        # 2s gap between reactions
        if idx < count - 1:
            await asyncio.sleep(2)

        # Update progress
        if (idx + 1) % 5 == 0 or idx == count - 1:
            try:
                text = (
                    f"🔄 Reacting... {idx + 1}/{count}\n"
                    f"✅ Success: {success} | ❌ Failed: {fail}"
                )
                await status_msg.edit_text(text, parse_mode="Markdown")
            except Exception:
                pass

    await db.set_process_running("reactions", False)

    summary = (
        f"🎉 *Reaction Process Complete!*\n\n"
        f"📊 Summary:\n"
        f"• Total requested: {count}\n"
        f"• ✅ Success: {success}\n"
        f"• ❌ Failed: {fail}\n\n"
        f"💡 *Tip:* Reactions only work if accounts are members of the channel."
    )

    if errors and fail > 0:
        # Show up to 3 errors
        sample = errors[:3]
        summary += "\n\nErrors:\n" + "\n".join(f"• {e}" for e in sample)

    try:
        await status_msg.edit_text(summary, parse_mode="Markdown")
    except Exception:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=summary,
            parse_mode="Markdown"
        )


async def stop_reactions():
    global _react_stop
    _react_stop.set()


async def cancel(update: Update, context):
    await update.message.reply_text("❌ Operation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


def get_reactions_handler():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(reactions_start, pattern="^reactions$"),
        ],
        states={
            REACT_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, react_receive_link),
            ],
            REACT_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, react_receive_count),
            ],
            REACT_EMOJI: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, react_receive_emoji),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
        ],
  )
