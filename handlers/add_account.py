import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)
from database import db
from client_manager import client_manager
from config import OWNER_ID, ADMIN_IDS

logger = logging.getLogger(__name__)

# Conversation states
SINGLE_SESSION, BULK_COUNT, BULK_SESSION = range(10, 13)
PHONE_NUMBER, OTP_CODE, TWO_FA = range(13, 16)

# Data keys for conversation
DATA_MODE = "add_mode"
DATA_BULK_TOTAL = "bulk_total"
DATA_BULK_INDEX = "bulk_index"
DATA_BULK_LIST = "bulk_list"


async def add_account_menu(update: Update, context):
    """Show the add account menu with Single / Bulk options."""
    query = update.callback_query
    if query:
        await query.answer()
        message = query.message
    else:
        message = update.message

    keyboard = [
        [InlineKeyboardButton("📱 Single Add", callback_data="add_single")],
        [InlineKeyboardButton("📋 Bulk Add", callback_data="add_bulk")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
    ]
    text = "📥 *Add Account*\n\nChoose how you want to add accounts:"
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ConversationHandler.END


async def add_single_start(update: Update, context):
    """Start single account addition — choose method."""
    query = update.callback_query
    await query.answer()
    context.user_data[DATA_MODE] = "single"

    keyboard = [
        [InlineKeyboardButton("🔑 Session String", callback_data="add_session_string")],
        [InlineKeyboardButton("📱 Phone Login", callback_data="add_phone_login")],
        [InlineKeyboardButton("🔙 Back", callback_data="add_account")],
    ]
    await query.edit_message_text(
        "📱 *Single Add*\n\nChoose how to add the account:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return SINGLE_SESSION


async def add_via_session_string(update: Update, context):
    """Ask for session string."""
    query = update.callback_query
    await query.answer()
    context.user_data["add_method"] = "session"
    await query.edit_message_text(
        "🔑 *Session String Method*\n\nPlease send the Telethon session string.\n\n"
        "💡 Generate with:\n"
        "`python3 -c \"from telethon.sessions import StringSession; "
        "from telethon import TelegramClient; "
        "c = TelegramClient(StringSession(), API_ID, API_HASH); "
        "c.start(); print(c.session.save()); c.disconnect()\"`",
        parse_mode="Markdown"
    )
    return SINGLE_SESSION


async def add_via_phone_login(update: Update, context):
    """Start phone login flow — ask for phone number."""
    query = update.callback_query
    await query.answer()
    context.user_data["add_method"] = "phone"
    await query.edit_message_text(
        "📱 *Phone Login*\n\nSend the phone number in international format.\n"
        "Example: `+1234567890`\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown"
    )
    return PHONE_NUMBER


async def add_bulk_start(update: Update, context):
    """Start bulk addition — ask for count."""
    query = update.callback_query
    await query.answer()
    context.user_data[DATA_MODE] = "bulk"
    await query.edit_message_text(
        "📋 *Bulk Add*\n\nHow many accounts do you want to add?",
        parse_mode="Markdown"
    )
    return BULK_COUNT


async def handle_single_session(update: Update, context):
    """Route to session-string or phone-login based on stored method."""
    method = context.user_data.get("add_method", "session")

    if method == "session":
        return await _handle_session_string(update, context)
    else:
        # Should not reach here for single session state with session method
        await update.message.reply_text("❌ Something went wrong. Use /cancel and try again.")
        return ConversationHandler.END


async def _handle_session_string(update: Update, context):
    """Receive and validate a session string."""
    session_string = update.message.text.strip()
    status_msg = await update.message.reply_text("⏳ Validating session...")

    success, phone, error = await client_manager.validate_session(session_string)

    if not success:
        await status_msg.edit_text(
            f"❌ *Invalid Session*\n\nError: {error}\n\nPlease try again or send /cancel to abort.",
            parse_mode="Markdown"
        )
        return SINGLE_SESSION

    added = await db.add_account(phone, session_string)
    if not added:
        await status_msg.edit_text(
            f"⚠️ Account {phone} already exists in the database.\n\nSend another session or /cancel to abort.",
            parse_mode="Markdown"
        )
        return SINGLE_SESSION

    await status_msg.edit_text(
        f"✅ *Account Added Successfully!*\n\n📱 Phone: `{phone}`\n\nUse /start to go back to main menu.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def handle_phone_number(update: Update, context):
    """Receive phone number and send OTP code request."""
    phone = update.message.text.strip()
    status_msg = await update.message.reply_text("⏳ Sending OTP code...")

    try:
        client, code_hash = await client_manager.start_phone_login(phone)
        context.user_data["login_phone"] = phone
        await status_msg.edit_text(
            f"✅ OTP sent to `{phone}`\n\nPlease send the OTP code you received.\n\n"
            f"Send /cancel to abort.",
            parse_mode="Markdown"
        )
        return OTP_CODE
    except ValueError as e:
        await status_msg.edit_text(
            f"❌ {str(e)}\n\nTry a different phone or use session string.\n\nSend /cancel to abort.",
            parse_mode="Markdown"
        )
        from client_manager import client_manager as cm
        await cm.cancel_pending_login(phone)
        return SINGLE_SESSION
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Failed to send OTP: {str(e)}\n\nSend /cancel to abort.",
            parse_mode="Markdown"
        )
        from client_manager import client_manager as cm
        await cm.cancel_pending_login(phone)
        return SINGLE_SESSION


async def handle_otp(update: Update, context):
    """Receive OTP code and attempt sign-in."""
    code = update.message.text.strip()
    phone = context.user_data.get("login_phone")

    if not phone:
        await update.message.reply_text("❌ Login session expired. Please start over.\n\nSend /cancel to abort.")
        return ConversationHandler.END

    status_msg = await update.message.reply_text("⏳ Verifying OTP...")

    success, result, error = await client_manager.submit_otp(phone, code)

    if success:
        # result is the session_string
        added = await db.add_account(phone, result)
        if added:
            await status_msg.edit_text(
                f"✅ *Account Added Successfully!*\n\n📱 Phone: `{phone}`\n\nUse /start for main menu.",
                parse_mode="Markdown"
            )
        else:
            await status_msg.edit_text(
                f"⚠️ Account {phone} already existed, but login successful.\n\nUse /start for main menu.",
                parse_mode="Markdown"
            )
        context.user_data.clear()
        return ConversationHandler.END
    elif error == "2FA_REQUIRED":
        context.user_data["login_awaiting_2fa"] = True
        await status_msg.edit_text(
            f"🔐 *Two-Factor Authentication Required*\n\n"
            f"Account `{phone}` has 2FA enabled.\nPlease send your 2FA password.\n\n"
            f"Send /cancel to abort.",
            parse_mode="Markdown"
        )
        return TWO_FA
    else:
        await status_msg.edit_text(
            f"❌ Invalid OTP: {error}\n\nPlease try again or send /cancel to abort.",
            parse_mode="Markdown"
        )
        return OTP_CODE


async def handle_2fa(update: Update, context):
    """Receive 2FA password and complete login."""
    password = update.message.text.strip()
    phone = context.user_data.get("login_phone")

    if not phone:
        await update.message.reply_text("❌ Login session expired. Start over.\n\nSend /cancel.")
        return ConversationHandler.END

    status_msg = await update.message.reply_text("⏳ Verifying 2FA password...")

    success, session_string, error = await client_manager.submit_2fa(phone, password)

    if success:
        added = await db.add_account(phone, session_string)
        if added:
            await status_msg.edit_text(
                f"✅ *Account Added Successfully!*\n\n📱 Phone: `{phone}`\n\nUse /start for main menu.",
                parse_mode="Markdown"
            )
        else:
            await status_msg.edit_text(
                f"⚠️ Account {phone} already existed, but login successful.\n\nUse /start for main menu.",
                parse_mode="Markdown"
            )
        context.user_data.clear()
        return ConversationHandler.END
    else:
        await status_msg.edit_text(
            f"❌ Invalid 2FA password: {error}\n\nPlease try again or send /cancel to abort.",
            parse_mode="Markdown"
        )
        return TWO_FA


async def handle_bulk_count(update: Update, context):
    """Receive bulk count and start asking for sessions."""
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text("❌ Please send a valid number greater than 0.\n\nOr /cancel to abort.")
        return BULK_COUNT

    total = int(text)
    context.user_data[DATA_BULK_TOTAL] = total
    context.user_data[DATA_BULK_INDEX] = 0
    context.user_data[DATA_BULK_LIST] = []

    await update.message.reply_text(
        f"📋 *Bulk Add* — {total} accounts\n\nSend session string **1/{total}**:",
        parse_mode="Markdown"
    )
    return BULK_SESSION


async def handle_bulk_session(update: Update, context):
    """Receive one session string during bulk addition."""
    session_string = update.message.text.strip()
    index = context.user_data.get(DATA_BULK_INDEX, 0) + 1
    total = context.user_data.get(DATA_BULK_TOTAL, 0)

    status_msg = await update.message.reply_text(f"⏳ Validating session {index}/{total}...")

    success, phone, error = await client_manager.validate_session(session_string)

    if not success:
        await status_msg.edit_text(
            f"❌ *Invalid Session* ({index}/{total})\nError: {error}\n\nPlease send a valid session string or /cancel to abort.",
            parse_mode="Markdown"
        )
        return BULK_SESSION

    added = await db.add_account(phone, session_string)
    if not added:
        await status_msg.edit_text(
            f"⚠️ Account {phone} already exists. Skipping.\n\nSend session **{index}/{total}** (this one was skipped):",
            parse_mode="Markdown"
        )
        return BULK_SESSION

    bulk_list = context.user_data.get(DATA_BULK_LIST, [])
    bulk_list.append(phone)
    context.user_data[DATA_BULK_LIST] = bulk_list
    context.user_data[DATA_BULK_INDEX] = index

    await status_msg.edit_text(f"✅ Account {phone} added! ({index}/{total})")

    if index >= total:
        all_phones = context.user_data[DATA_BULK_LIST]
        phone_list = "\n".join(f"• `{p}`" for p in all_phones)
        await update.message.reply_text(
            f"🎉 *Bulk Add Complete!*\n\n{len(all_phones)} accounts added:\n{phone_list}\n\nUse /start for main menu.",
            parse_mode="Markdown"
        )
        for key in [DATA_MODE, DATA_BULK_TOTAL, DATA_BULK_INDEX, DATA_BULK_LIST]:
            context.user_data.pop(key, None)
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            f"Send session string **{index + 1}/{total}**:",
            parse_mode="Markdown"
        )
        return BULK_SESSION


async def cancel(update: Update, context):
    """Cancel any ongoing conversation."""
    phone = context.user_data.get("login_phone")
    if phone:
        await client_manager.cancel_pending_login(phone)
    await update.message.reply_text("❌ Operation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


def get_add_account_handler():
    """Return the ConversationHandler for adding accounts."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_single_start, pattern="^add_single$"),
            CallbackQueryHandler(add_bulk_start, pattern="^add_bulk$"),
        ],
        states={
            SINGLE_SESSION: [
                CallbackQueryHandler(add_via_session_string, pattern="^add_session_string$"),
                CallbackQueryHandler(add_via_phone_login, pattern="^add_phone_login$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_single_session),
            ],
            PHONE_NUMBER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phone_number),
            ],
            OTP_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_otp),
            ],
            TWO_FA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_2fa),
            ],
            BULK_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bulk_count),
            ],
            BULK_SESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_bulk_session),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
        ],
  )
