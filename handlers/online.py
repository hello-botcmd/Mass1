import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler
from database import db
from client_manager import client_manager

logger = logging.getLogger(__name__)


async def all_accounts_online(update: Update, context):
    """Bring all accounts online and keep them online."""
    query = update.callback_query
    await query.answer()

    status_msg = await query.edit_message_text(
        "🌐 *Bringing all accounts online...*",
        parse_mode="Markdown"
    )

    all_accounts = await db.get_all_accounts()

    if not all_accounts:
        await status_msg.edit_text("❌ No accounts in the database.")
        return

    success = 0
    fail = 0

    for acc in all_accounts:
        phone = acc["_id"]
        try:
            client = await client_manager.get_client(phone)
            await client_manager.set_online(phone)
            await client_manager.start_online_ping(phone)
            await db.mark_online(phone, True)
            await db.set_status(phone, "online")
            success += 1
        except KeyError:
            # Client not connected yet
            try:
                client = await client_manager.create_client(acc["session_string"], phone)
                await client_manager.set_online(phone)
                await client_manager.start_online_ping(phone)
                await db.mark_online(phone, True)
                await db.set_status(phone, "online")
                success += 1
            except Exception as e:
                logger.error(f"Failed to bring {phone} online: {e}")
                fail += 1
        except Exception as e:
            logger.error(f"Failed to bring {phone} online: {e}")
            fail += 1

        await asyncio.sleep(1)  # Small delay between accounts

    await status_msg.edit_text(
        f"🌐 *All Accounts Online*\n\n"
        f"📊 Results:\n"
        f"• ✅ Online: {success}\n"
        f"• ❌ Failed: {fail}\n"
        f"• Total: {len(all_accounts)}\n\n"
        f"Accounts will be kept online with continuous ping.",
        parse_mode="Markdown"
  )
