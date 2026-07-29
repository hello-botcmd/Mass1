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
from telethon.tl.functions.messages import GetMessagesViewsRequest
from telethon.tl.types import PeerChannel
from telethon.errors import RPCError
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
        "Examples:\n"
        "`https://t.me/username/1234`\n"
        "`https://t.me/username/5678`\n\n"
        "**⚠️ Important:**\n"
        "• Accounts MUST already be members of the channel\n"
        "• Each account can only increment views **once or twice per day** per post\n"
        "• Running in a loop will NOT increase views further\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown"
    )
    return VIEWS_LINKS


def parse_post_link(link: str):
    """Parse link -> (identifier, msg_id)."""
    pattern = r"https://t\.me/(?:c/)?([^/]+)/(\d+)"
    match = re.match(pattern, link.strip())
    if not match:
        return None, None
    identifier = match.group(1)
    msg_id = int(match.group(2))
    return identifier, msg_id


async def views_receive_links(update: Update, context):
    """Receive post links."""
    text = update.message.text.strip()
    lines = text.strip().split("\n")
    links = [l.strip() for l in lines if l.strip()]

    parsed_links = []
    for link in links:
        identifier, msg_id = parse_post_link(link)
        if identifier and msg_id:
            parsed_links.append({"identifier": identifier, "msg_id": msg_id, "link": link})
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
        "How many accounts should view each post?\n"
        "(Recommended: 1 view per account per post per day)",
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
        f"Each post: {count} accounts viewing\n"
        f"Gap: 2s between each view\n\n"
        f"Use /stop to abort.\n\n"
        f"💡 Remember: Each account can only increment views once per day per post.",
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

    if len(available) < target_count:
        await status_msg.edit_text(
            f"❌ Only {len(available)} accounts available. Need {target_count} per post.\n"
            f"Please add more accounts or reduce the count.",
            parse_mode="Markdown"
        )
        await db.set_process_running("views", False)
        return

    selected = available[:target_count]
    overall_success = 0
    overall_fail = 0
    errors = []

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

        identifier = post["identifier"]
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

                # CRITICAL: Resolve the peer entity properly
                # Try to resolve the identifier
                try:
                    entity = await client.get_entity(identifier)
                except ValueError:
                    # If it looks like a channel ID, wrap in PeerChannel
                    if identifier.startswith("100") or identifier.startswith("-100"):
                        try:
                            peer_id = int(identifier)
                            entity = await client.get_entity(PeerChannel(peer_id))
                        except Exception:
                            fail_text = f"❌ Cannot resolve channel {identifier} for {phone}. Is the account a member?"
                            overall_fail += 1
                            errors.append(f"{phone}: Cannot resolve {identifier}")
                            logger.warning(fail_text)
                            if acc_idx < target_count - 1:
                                await asyncio.sleep(2)
                            continue
                    else:
                        overall_fail += 1
                        errors.append(f"{phone}: Unknown entity {identifier}")
                        if acc_idx < target_count - 1:
                            await asyncio.sleep(2)
                        continue
                except Exception as e:
                    overall_fail += 1
                    errors.append(f"{phone}: get_entity error - {str(e)[:100]}")
                    if acc_idx < target_count - 1:
                        await asyncio.sleep(2)
                    continue

                # Increment view count
                await client(GetMessagesViewsRequest(
                    peer=entity,
                    id=[msg_id],
                    increment=True
                ))
                overall_success += 1
                logger.info(f"{phone} viewed {identifier}/{msg_id}")

            except RPCError as e:
                logger.error(f"View RPC error for {phone}: {e}")
                overall_fail += 1
                errors.append(f"{phone}: {str(e)[:100]}")
                if "FLOOD" in str(e):
                    await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"View error for {phone}: {e}")
                overall_fail += 1
                errors.append(f"{phone}: {str(e)[:100]}")

            # 2s gap
            if acc_idx < target_count - 1:
                await asyncio.sleep(2)

            # Update progress every 5 views
            if (acc_idx + 1) % 5 == 0 or acc_idx == target_count - 1:
                try:
                    await status_msg.edit_text(
                        f"📄 Post {post_idx + 1}/{len(parsed_links)}: `{link}`\n"
                        f"Progress: {acc_idx + 1}/{target_count}\n"
                        f"✅ Views: {overall_success} | ❌ Failed: {overall_fail}",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

    await db.set_process_running("views", False)

    summary = (
        f"🎉 *View Boost Complete!*\n\n"
        f"📊 Summary:\n"
        f"• Posts: {len(parsed_links)}\n"
        f"• Accounts used: {target_count} per post\n"
        f"• ✅ Views sent: {overall_success}\n"
        f"• ❌ Failed: {overall_fail}\n\n"
        f"💡 *Note:* Each account can only increment views once per day per post.\n"
        f"Accounts must be members of the channel for this to work."
    )

    if errors and overall_fail > 0:
        sample = errors[:3]
        summary += "\n\nSample errors:\n" + "\n".join(f"• {e}" for e in sample)

    try:
        await status_msg.edit_text(summary, parse_mode="Markdown")
    except Exception:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=summary,
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
      )
