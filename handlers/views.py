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
VIEWS_LINKS, VIEWS_COUNT = range(40, 42)

_views_stop = asyncio.Event()

LINK_PATTERN = re.compile(
    r"https?://t\.me/(?:c/(\d+)|([^/]+))/(\d+)"
)


def parse_post_link(link: str):
    m = LINK_PATTERN.match(link.strip())
    if not m:
        return None, None
    cid, uname, mid = m.groups()
    if cid:
        return int(cid), int(mid)
    return uname, int(mid)


async def views_start(update: Update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "👁 *View Booster*\n\n"
        "Send the post link(s), one per line.\n"
        "Examples:\n"
        "`https://t.me/username/1234`\n"
        "`https://t.me/c/1234567890/1234`\n\n"
        "**⚠️ Note:** Each account can only increment views ~1-2x/day per post.\n"
        "Accounts must be channel members.\n\n"
        "/cancel to abort.",
        parse_mode="Markdown"
    )
    return VIEWS_LINKS


async def views_receive_links(update: Update, context):
    text = update.message.text.strip()
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    parsed = []
    for link in lines:
        identifier, msg_id = parse_post_link(link)
        if identifier is not None and msg_id is not None:
            parsed.append({"identifier": identifier, "msg_id": msg_id, "link": link})
        else:
            await update.message.reply_text(f"❌ Skipping invalid: `{link}`", parse_mode="Markdown")

    if not parsed:
        await update.message.reply_text("❌ No valid links.")
        return VIEWS_LINKS

    context.user_data["views_links"] = parsed
    summary = "\n".join(f"• `{l['link']}`" for l in parsed)
    await update.message.reply_text(
        f"✅ {len(parsed)} posts:\n{summary}\n\n"
        "How many accounts should view each post?",
        parse_mode="Markdown"
    )
    return VIEWS_COUNT


async def views_receive_count(update: Update, context):
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text("❌ Enter a valid number.")
        return VIEWS_COUNT

    count = int(text)
    parsed = context.user_data["views_links"]

    status_msg = await update.message.reply_text(
        f"🚀 Boosting {len(parsed)} post(s), {count} views each...\n"
        f"Use /stop to abort.",
        parse_mode="Markdown"
    )

    global _views_stop
    _views_stop.clear()
    await db.set_process_running("views", True)

    context.application.create_task(
        _execute_views(update, context, parsed, count, status_msg)
    )

    context.user_data.clear()
    return ConversationHandler.END


async def _execute_views(update, context, parsed_links, target_count, status_msg):
    """Background task for views — fresh client per account per post."""
    from database import db as db_

    all_accounts = await db_.get_all_accounts()
    available = [a for a in all_accounts if not a.get("currently_busy", False)]
    random.shuffle(available)

    if len(available) < target_count:
        await status_msg.edit_text(f"❌ Only {len(available)} accounts available. Need {target_count}.")
        await db_.set_process_running("views", False)
        return

    selected = available[:target_count]
    overall_success = 0
    overall_fail = 0
    errors = []

    for post_idx, post in enumerate(parsed_links):
        if _views_stop.is_set():
            await status_msg.edit_text(
                f"⏹ Stopped. Post {post_idx + 1}/{len(parsed_links)} | ✅ {overall_success} ❌ {overall_fail}",
                parse_mode="Markdown"
            )
            await db_.set_process_running("views", False)
            return

        identifier = post["identifier"]
        msg_id = post["msg_id"]

        await status_msg.edit_text(
            f"📄 Post {post_idx + 1}/{len(parsed_links)} — 0/{target_count}",
            parse_mode="Markdown"
        )

        for acc_idx, acc in enumerate(selected):
            if _views_stop.is_set():
                break

            phone = acc["_id"]
            ss = acc["session_string"]

            try:
                session = StringSession(ss)
                async with TelegramClient(session, API_ID, API_HASH) as client:
                    await client.connect()
                    if not await client.is_user_authorized():
                        overall_fail += 1
                        continue

                    # Resolve peer
                    try:
                        if isinstance(identifier, int):
                            resolved = await client.get_entity(PeerChannel(identifier))
                        else:
                            resolved = await client.get_entity(identifier)
                    except Exception:
                        overall_fail += 1
                        errors.append(f"{phone}: Not in chat {identifier}")
                        continue

                    # Increment view
                    await client(functions.messages.GetMessagesViewsRequest(
                        peer=resolved,
                        id=[msg_id],
                        increment=True
                    ))
                    overall_success += 1
                    logger.info(f"✅ {phone} viewed msg {msg_id}")

            except FloodWaitError as e:
                logger.warning(f"Flood on {phone}: {e.seconds}s")
                overall_fail += 1
                await asyncio.sleep(min(e.seconds, 5))
            except Exception as e:
                logger.error(f"View error {phone}: {e}")
                overall_fail += 1
                errors.append(f"{phone}: {str(e)[:80]}")

            await asyncio.sleep(random.uniform(2, 4))

            if (acc_idx + 1) % 5 == 0 or acc_idx == target_count - 1:
                try:
                    await status_msg.edit_text(
                        f"📄 Post {post_idx + 1}/{len(parsed_links)} — {acc_idx + 1}/{target_count}\n"
                        f"✅ {overall_success} | ❌ {overall_fail}",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

    await db_.set_process_running("views", False)

    summary = (
        f"🎉 *View Boost Complete!*\n"
        f"• Posts: {len(parsed_links)}\n"
        f"• ✅ Views: {overall_success}\n"
        f"• ❌ Failed: {overall_fail}\n\n"
        f"💡 Each account can only increment views 1-2x/day per post."
    )
    if errors and overall_fail > 0:
        summary += "\n" + "\n".join(f"• {e}" for e in errors[:3])

    try:
        await status_msg.edit_text(summary, parse_mode="Markdown")
    except Exception:
        await context.bot.send_message(update.effective_chat.id, summary, parse_mode="Markdown")


async def stop_views():
    global _views_stop
    _views_stop.set()


async def cancel(update: Update, context):
    await update.message.reply_text("❌ Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


def get_views_handler():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(views_start, pattern="^views$")],
        states={
            VIEWS_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, views_receive_links)],
            VIEWS_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, views_receive_count)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
  )
