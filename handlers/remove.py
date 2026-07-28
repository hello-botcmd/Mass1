import re
import logging
import asyncio
from telegram import Update
from telegram.ext import CommandHandler, filters
from telethon.tl.functions.channels import LeaveChannelRequest
from database import db
from client_manager import client_manager
from config import OWNER_ID, ADMIN_IDS

logger = logging.getLogger(__name__)


async def remove_from_chat(update: Update, context):
    """Remove all joined accounts from a specified chat."""
    user_id = update.effective_user.id

    if user_id != OWNER_ID and user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ Usage: `/remove chat_identifier`\n\n"
            "Examples:\n"
            "• `/remove @username`\n"
            "• `/remove https://t.me/username`\n"
            "• `/remove -1001234567890`",
            parse_mode="Markdown"
        )
        return

    chat_input = " ".join(args)

    status_msg = await update.message.reply_text(
        f"🔄 Removing all accounts from `{chat_input}`...\n"
        f"This may take a while. Please wait.",
        parse_mode="Markdown"
    )

    # Resolve chat identifier
    if chat_input.startswith("https://t.me/+"):
        # Private invite link — can't leave via this, need the chat ID
        await status_msg.edit_text(
            "❌ Cannot resolve private invite link to a chat ID.\n"
            "Please use the chat ID or public username.",
            parse_mode="Markdown"
        )
        return

    # Clean username
    if "t.me/" in chat_input:
        username = chat_input.split("t.me/")[-1].split("/")[0]
    elif chat_input.startswith("@"):
        username = chat_input[1:]
    else:
        username = chat_input

    all_accounts = await db.get_all_accounts()
    success = 0
    fail = 0

    for acc in all_accounts:
        phone = acc["_id"]
        try:
            client = await client_manager.get_client(phone)

            # Get entity
            try:
                entity = await client.get_entity(username)
            except Exception:
                # Try as chat ID
                try:
                    entity = await client.get_entity(int(chat_input))
                except Exception:
                    fail += 1
                    continue

            # Leave channel/group
            await client(LeaveChannelRequest(entity))
            success += 1
            logger.info(f"{phone} left {username}")

        except KeyError:
            # Client not connected, create and leave
            try:
                client = await client_manager.create_client(acc["session_string"], phone)
                entity = await client.get_entity(username)
                await client(LeaveChannelRequest(entity))
                success += 1
            except Exception as e:
                logger.error(f"Failed to remove {phone}: {e}")
                fail += 1
        except Exception as e:
            logger.error(f"Failed to remove {phone}: {e}")
            fail += 1

        await asyncio.sleep(2)  # Small delay to avoid flood

    await status_msg.edit_text(
        f"✅ *Removal Complete*\n\n"
        f"Chat: `{chat_input}`\n"
        f"• ✅ Left: {success}\n"
        f"• ❌ Failed: {fail}\n"
        f"• Total accounts: {len(all_accounts)}",
        parse_mode="Markdown"
    )
