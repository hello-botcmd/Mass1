import asyncio
import random
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)
from telethon import functions
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.errors import (
    InviteHashExpiredError,
    InviteHashInvalidError,
    ChannelsTooMuchError,
    ChannelInvalidError,
    ChannelPrivateError,
    UserAlreadyParticipantError,
    UsernameNotOccupiedError,
    FloodWaitError,
)
from database import db
from client_manager import client_manager
from config import OWNER_ID, ADMIN_IDS

logger = logging.getLogger(__name__)

# States
JOIN_LINK, JOIN_COUNT, JOIN_TIMING = range(20, 23)

# Stop event
_join_stop = asyncio.Event()


async def join_start(update: Update, context):
    """Start the join flow."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🔗 *Join Channel/Group*\n\n"
        "Send the invite link or username of the channel/group.\n"
        "Examples:\n"
        "• Public: `@username` or `https://t.me/username`\n"
        "• Private: `https://t.me/+invitehash`\n\n"
        "Send /cancel to abort.",
        parse_mode="Markdown"
    )
    return JOIN_LINK


async def join_receive_link(update: Update, context):
    """Receive the join link/username."""
    text = update.message.text.strip()
    context.user_data["join_target"] = text
    await update.message.reply_text(
        f"✅ Target set: `{text}`\n\n"
        "How many accounts should join?",
        parse_mode="Markdown"
    )
    return JOIN_COUNT


async def join_receive_count(update: Update, context):
    """Receive the number of accounts to join."""
    text = update.message.text.strip()
    if not text.isdigit() or int(text) < 1:
        await update.message.reply_text("❌ Please send a valid positive number.\n\nOr /cancel to abort.")
        return JOIN_COUNT

    count = int(text)
    total_accounts = await db.get_accounts_count()
    idle_accounts = await db.get_accounts_count()  # total is fine, we'll use busy check

    if count > total_accounts:
        await update.message.reply_text(
            f"❌ Only {total_accounts} accounts in the database. Please enter a number ≤ {total_accounts}.",
            parse_mode="Markdown"
        )
        return JOIN_COUNT

    context.user_data["join_count"] = count

    # Show mode distribution
    mode1 = int(count * 0.4)
    mode2 = int(count * 0.4)
    mode3 = count - mode1 - mode2

    text = (
        f"📊 *Account Distribution:*\n"
        f"• Mode 1 (Stay Online): {mode1} accounts\n"
        f"• Mode 2 (Hide Last Seen): {mode2} accounts\n"
        f"• Mode 3 (Go Offline after 2min): {mode3} accounts\n\n"
        f"Now send the timing in format: `min-max`\n"
        f"Example: `1-8` (each account joins after random 1-8 seconds)\n\n"
        f"Or send /cancel to abort."
    )
    await update.message.reply_text(text, parse_mode="Markdown")
    return JOIN_TIMING


async def join_receive_timing(update: Update, context):
    """Receive timing and start the join process."""
    text = update.message.text.strip()
    try:
        parts = text.split("-")
        if len(parts) != 2:
            raise ValueError
        min_s = int(parts[0].strip().lower().replace("s", "").replace("min", "").replace(" ", ""))
        max_s = int(parts[1].strip().lower().replace("s", "").replace("max", "").replace(" ", ""))
        if min_s < 1 or max_s < min_s:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Use: `min-max`\nExample: `1-8`\n\nOr /cancel to abort.",
            parse_mode="Markdown"
        )
        return JOIN_TIMING

    target_text = context.user_data["join_target"]
    count = context.user_data["join_count"]

    # Calculate mode distribution
    mode1_count = int(count * 0.4)
    mode2_count = int(count * 0.4)
    mode3_count = count - mode1_count - mode2_count

    # Assign modes to accounts (all idle accounts)
    all_accounts = await db.get_all_accounts()
    # Filter out busy accounts
    available = [a for a in all_accounts if not a.get("currently_busy", False)]
    random.shuffle(available)

    if len(available) < count:
        await update.message.reply_text(
            f"❌ Only {len(available)} accounts are available (not busy). Need {count}.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    selected = available[:count]
    account_modes = []
    for i, acc in enumerate(selected):
        if i < mode1_count:
            acc_mode = 1
        elif i < mode1_count + mode2_count:
            acc_mode = 2
        else:
            acc_mode = 3
        account_modes.append((acc, acc_mode))

    status_msg = await update.message.reply_text(
        f"🚀 Starting join process for {count} accounts...\n\n"
        f"Target: `{target_text}`\n"
        f"Mode 1 (Stay Online): {mode1_count}\n"
        f"Mode 2 (Hide Last Seen): {mode2_count}\n"
        f"Mode 3 (Offline after 2min): {mode3_count}\n"
        f"Timing: {min_s}s - {max}s\n\n"
        f"Use /stop to abort.",
        parse_mode="Markdown"
    )

    # Reset stop event
    global _join_stop
    _join_stop.clear()
    await db.set_process_running("join", True)

    # Run join process in background
    context.application.create_task(
        _execute_join(update, context, account_modes, target_text, min_s, max_s, status_msg)
    )

    # End conversation
    context.user_data.clear()
    return ConversationHandler.END


async def _execute_join(update, context, account_modes, target_text, min_s, max_s, status_msg):
    """Background task that executes the joining."""
    target = target_text.strip()
    results = {"success": 0, "fail": 0, "details": []}
    total = len(account_modes)

    # Resolve target
    resolved_entity = None
    invite_hash = None

    if target.startswith("https://t.me/+"):
        invite_hash = target.replace("https://t.me/+", "").replace("+", "")
    else:
        # Could be username or full link
        if "t.me/" in target:
            username = target.split("t.me/")[-1]
        elif target.startswith("@"):
            username = target[1:]
        else:
            username = target

    for idx, (acc, mode) in enumerate(account_modes):
        # Check if stopped
        if _join_stop.is_set():
            await status_msg.edit_text(
                f"⏹ *Join process stopped.*\n\n"
                f"Completed: {idx}/{total} accounts\n"
                f"✅ Success: {results['success']} | ❌ Failed: {results['fail']}",
                parse_mode="Markdown"
            )
            await db.set_process_running("join", False)
            return

        phone = acc["_id"]
        session_string = acc["session_string"]
        account_result = {"phone": phone, "mode": mode, "status": "pending"}

        try:
            # Connect client
            client = await client_manager.create_client(session_string, phone)

            # Step 1: Come online
            await client(functions.account.UpdateStatusRequest(offline=False))
            await db.mark_online(phone, True)
            await db.set_status(phone, "joining")

            # Mode-specific pre-join setup
            if mode == 2:
                # Hide last seen
                await client_manager.set_hide_last_seen(phone)
                await db.update_account(phone, {"last_seen_hidden": True})

            elif mode == 1:
                # Start online ping
                await client_manager.start_online_ping(phone)

            # Step 2: Join the channel/group
            try:
                if invite_hash:
                    # Private invite
                    await client(ImportChatInviteRequest(invite_hash))
                else:
                    # Public join
                    entity = await client.get_entity(username)
                    await client(functions.channels.JoinChannelRequest(entity))

                account_result["status"] = "success"
                results["success"] += 1

                # Mode 3: schedule going offline after 2 minutes
                if mode == 3:
                    asyncio.create_task(_go_offline_after_delay(phone, 120))

            except UserAlreadyParticipantError:
                account_result["status"] = "already_in"
                results["success"] += 1  # still counts as success
            except (InviteHashExpiredError, InviteHashInvalidError) as e:
                account_result["status"] = f"invalid_invite: {str(e)}"
                results["fail"] += 1
            except (ChannelInvalidError, ChannelPrivateError, UsernameNotOccupiedError) as e:
                account_result["status"] = f"channel_error: {str(e)}"
                results["fail"] += 1
            except FloodWaitError as e:
                wait = e.seconds
                logger.warning(f"Flood wait on {phone}: {wait}s")
                account_result["status"] = f"flood_wait_{wait}s"
                results["fail"] += 1
                await client_manager.disconnect_client(phone)
                # Still wait before continuing
                await asyncio.sleep(min(wait, 10))
            except Exception as e:
                account_result["status"] = f"error: {str(e)}"
                results["fail"] += 1
                logger.error(f"Join error for {phone}: {e}")

            # Update DB
            await db.mark_busy(phone, False)
            await db.set_status(phone, "idle")
            await db.update_account(phone, {"mode": mode})

        except Exception as e:
            logger.error(f"Connection error for {phone}: {e}")
            account_result["status"] = f"conn_error: {str(e)}"
            results["fail"] += 1

        results["details"].append(account_result)

        # Update status message every 5 accounts
        if (idx + 1) % 5 == 0 or idx == total - 1:
            try:
                await status_msg.edit_text(
                    f"🔄 Joining in progress...\n\n"
                    f"Progress: {idx + 1}/{total}\n"
                    f"✅ Success: {results['success']} | ❌ Failed: {results['fail']}",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        # Random delay between accounts
        if idx < total - 1:
            delay = random.uniform(min_s, max_s)
            await asyncio.sleep(delay)

    # Complete
    await db.set_process_running("join", False)
    summary = (
        f"🎉 *Join Process Complete!*\n\n"
        f"📊 Summary:\n"
        f"• Total: {total}\n"
        f"• ✅ Success: {results['success']}\n"
        f"• ❌ Failed: {results['fail']}\n\n"
        f"Target: `{target_text}`"
    )
    try:
        await status_msg.edit_text(summary, parse_mode="Markdown")
    except Exception:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=summary,
            parse_mode="Markdown"
        )


async def _go_offline_after_delay(phone: str, delay: int):
    """Set account offline after delay seconds."""
    await asyncio.sleep(delay)
    try:
        await client_manager.set_offline(phone)
        await db.mark_online(phone, False)
        await db.set_status(phone, "idle")
        logger.info(f"{phone} went offline after {delay}s (Mode 3)")
    except Exception as e:
        logger.error(f"Failed to set {phone} offline after delay: {e}")


async def stop_join():
    """Signal join process to stop."""
    global _join_stop
    _join_stop.set()


def get_join_handler():
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(join_start, pattern="^join$"),
        ],
        states={
            JOIN_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, join_receive_link),
            ],
            JOIN_COUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, join_receive_count),
            ],
            JOIN_TIMING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, join_receive_timing),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
        ],
        per_message=True,
  )
