import asyncio
import re
import logging
from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)
from telethon.tl.functions.messages import GetMessagesViewsRequest
from database import db
from client_manager import client_manager

logger = logging.getLogger(__name__)

# States
VIEWS_LINKS, VIEWS_COUNT = range(40, 42)

_views_stop = asyncio.Event()


async def views_start(update: Update, context):
    """Start views flow."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "👁 *View Booster*\n\n"
        "Send the post link(s).\n"
        "You can send single or multiple links (one per line).\n\n"
        "Example:\n"
        "`https://t.me/username/1234`\n"
        "`https://t.me/username/5678`\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown"
    )
    return VIEWS_LINKS


def parse_post_link(link: str):
    """Parse link -> (peer, [msg_ids]) or None."""
    # Format: https://t.me/username/1234 or https://t.me/c/1234567890/1234
    pattern = r"https://t\.me/(?:c/)?([^/]+)/(\d+)"
    match = re.match(pattern, link.strip())
    if not match:
        return None, None

    identifier = match.group(1)
    msg_id = int(match.group(2))

    # Handle private channel format: c/chat_id
    if link.strip().startswith("https://t.me/c/"):
        peer = int(f"-100{identifier}")
    else:
        peer = identifier

    return peer, msg_id


async def views_receive_links(update: Update, context):
    """Receive post links."""
    text = update.message.text.strip()
    lines = text.strip().split("\n")
    links = [l.strip() for l in lines if l.strip()]

    parsed_links = []
    for link in links:
        peer, msg_id = parse_post_link(link)
        if peer and msg_id:
            parsed_links.append({"peer": peer, "msg_id": msg_id, "link": link})
        else:
            await update.message.reply_text(f"❌ Could not parse: `{link}`\nSkipping...", parse_mode="Markdown")

    if not parsed_links:
        await update.message.reply_text(
            "❌ No valid links found. Please send valid Telegram post links.\n\nOr /cancel to abort.",
            parse_mode="Markdown"
        )
        return VIEWS_LINKS

    context.user_data["views_links"] = parsed_links
    link_summary = "\n".join(f"• `{l['link']}`" for l in parsed_links)
    await update.message.reply_text(
        f"✅ Posts detected ({len(parsed_links)}):\n{link_summary}\n\n"
        "How many views should each post get?",
        parse_mode="Markdown"
    )
    return VIEWS_COUNT


async def views_receive_count(update: Update, context):
    """Receive view count and start boosting."""
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text("❌ Please send a valid positive number.\n\nOr /cancel to abort.")
        return VIEWS_COUNT

    count = int(text)
    parsed_links = context.user_data["views_links"]

    status_msg = await update.message.reply_text(
        f"🚀 Starting view boost for {len(parsed_links)} post(s)...\n"
        f"Each post: {count} views\n"
        f"Gap: 2s between each view\n\n"
        f"Use /stop to abort.",
        parse_mode="Markdown"
    )

    global _views_stop
    _views_stop.clear()
    await db.set_process_running("views", True)

    context.application.create_task(
        _execute_views(update, context, parsed_links, count, status_msg)
    )

    context.user_data.clear()
    return ConversationHandler.END


async def _execute_views(update, context, parsed_links, target_count, status_msg):
    """Background task for boosting views."""
    all_accounts = await db.get_all_accounts()
    available = [a for a in all_accounts if not a.get("currently_busy", False)]
    random.shuffle(available)

    total_needed = target_count * len(parsed_links)

    if len(available) < target_count:
        await status_msg.edit_text(
            f"❌ Only {len(available)} accounts available. Need {target_count} per post.\n"
            f"Total needed: {total_needed}",
            parse_mode="Markdown"
        )
        await db.set_process_running("views", False)
        return

    selected = available[:target_count]
    overall_success = 0
    overall_fail = 0

    for post_idx, post in enumerate(parsed_links):
        if _views_stop.is_set():
            await status_msg.edit_text(
                f"⏹ *View boost stopped.*\n\n"
                f"Post {post_idx}/{len(parsed_links)}\n"
                f"✅ Success: {overall_success} | ❌ Failed: {overall_fail}",
                parse_mode="Markdown"
            )
            await db.set_process_running("views", False)
            return

        peer = post["peer"]
        msg_id = post["msg_id"]
        link = post["link"]

        await status_msg.edit_text(
            f"📄 Boosting post {post_idx + 1}/{len(parsed_links)}: `{link}`\n"
            f"Progress: 0/{target_count}",
            parse_mode="Markdown"
        )

        for acc_idx, acc in enumerate(selected):
            if _views_stop.is_set():
                break

            phone = acc["_id"]
            try:
                client = await client_manager.get_client(phone)

                # Account must be in the channel to view
                # Use GetMessagesViewsRequest to increment view
                await client(GetMessagesViewsRequest(
                    peer=peer,
                    id=[msg_id],
                    increment=True
                ))
                overall_success += 1

            except Exception as e:
                logger.error(f"View error for {phone} on {link}: {e}")
                overall_fail += 1

            # Update progress every 5 views
            if (acc_idx + 1) % 5 == 0 or acc_idx == target_count - 1:
                try:
                    await status_msg.edit_text(
                        f"📄 Post {post_idx + 1}/{len(parsed_links)}: `{link}`\n"
                        f"Progress: {acc_idx + 1}/{target_count}\n"
                        f"✅ Success: {overall_success} | ❌ Failed: {overall_fail}",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

            # 2s gap between views (simultaneous per account)
            await asyncio.sleep(2)

    await db.set_process_running("views", False)
    await status_msg.edit_text(
        f"🎉 *View Boost Complete!*\n\n"
        f"📊 Summary:\n"
        f"• Posts: {len(parsed_links)}\n"
        f"• Views per post: {target_count}\n"
        f"• ✅ Success: {overall_success}\n"
        f"• ❌ Failed: {overall_fail}",
        parse_mode="Markdown"
    )


async def stop_views():
    global _views_stop
    _views_stop.set()


async def cancel(update: Update, context):
    await update.message.reply_text("❌ Operation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


def get_views_handler():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(views_start, pattern="^views$"),
        ],
        states={
            VIEWS_LINKS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, views_receive_links),
            ],
            VIEWS_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, views_receive_count),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
        ],
        per_message=True,
        )
