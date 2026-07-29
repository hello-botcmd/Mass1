import asyncio
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
from telethon import functions
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.errors import (
    InviteHashExpiredError,
    InviteHashInvalidError,
    ChannelInvalidError,
    ChannelPrivateError,
    UserAlreadyParticipantError,
    UsernameNotOccupiedError,
    FloodWaitError,
)
from telethon.sessions import StringSession
from database import db
from client_manager import client_manager
from config import API_ID, API_HASH

logger = logging.getLogger(__name__)

# States
JOIN_LINK, JOIN_MODES, JOIN_TIMING = range(20, 23)

_join_stop = asyncio.Event()


async def join_start(update: Update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🔗 *Join Channel/Group*\n\n"
        "Send the invite link or username.\n"
        "Examples:\n"
        "• `@username` or `https://t.me/username`\n"
        "• `https://t.me/+invitehash`\n\n"
        "/cancel to abort.",
        parse_mode="Markdown"
    )
    return JOIN_LINK


async def join_receive_link(update: Update, context):
    text = update.message.text.strip()
    context.user_data["join_target"] = text
    await update.message.reply_text(
        f"✅ Target: `{text}`\n\n"
        "Send mode distribution as:\n"
        "`mode1,mode2,mode3`\n\n"
        "• **Mode 1** — Stay online permanently (profile shows online)\n"
        "• **Mode 2** — Hide last seen (profile shows 'last seen recently')\n"
        "• **Mode 3** — Online for 2min, then offline (shows 'last seen X ago')\n\n"
        "Example: `5,3,2`\n\n"
        "/cancel to abort.",
        parse_mode="Markdown"
    )
    return JOIN_MODES


async def join_receive_modes(update: Update, context):
    text = update.message.text.strip()
    parts = text.split(",")
    if len(parts) != 3:
        await update.message.reply_text("❌ Need exactly 3 numbers: `5,3,2`")
        return JOIN_MODES

    try:
        m1, m2, m3 = int(parts[0].strip()), int(parts[1].strip()), int(parts[2].strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid numbers. Example: `5,3,2`")
        return JOIN_MODES

    if m1 < 0 or m2 < 0 or m3 < 0:
        await update.message.reply_text("❌ No negative numbers.")
        return JOIN_MODES

    total = m1 + m2 + m3
    if total == 0:
        await update.message.reply_text("❌ Total must be > 0.")
        return JOIN_MODES

    db_total = await db.get_accounts_count()
    if total > db_total:
        await update.message.reply_text(f"❌ Only {db_total} accounts. Need {total}.")
        return JOIN_MODES

    context.user_data["mode1_count"] = m1
    context.user_data["mode2_count"] = m2
    context.user_data["mode3_count"] = m3
    context.user_data["join_total"] = total

    await update.message.reply_text(
        f"✅ Distribution:\n"
        f"• Mode 1 (Online): {m1}\n"
        f"• Mode 2 (Hidden): {m2}\n"
        f"• Mode 3 (Offline): {m3}\n\n"
        f"Send timing as `min-max`\n"
        f"Example: `1-8`\n\n"
        "/cancel to abort.",
        parse_mode="Markdown"
    )
    return JOIN_TIMING


async def join_receive_timing(update: Update, context):
    text = update.message.text.strip()
    try:
        parts = text.split("-")
        if len(parts) != 2:
            raise ValueError
        min_s = int(parts[0].strip().lower().replace("s", "").replace("min", "").strip())
        max_s = int(parts[1].strip().lower().replace("s", "").replace("max", "").strip())
        if min_s < 1 or max_s < min_s:
            raise ValueError
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Format: `1-8`\n\n/cancel")
        return JOIN_TIMING

    target_text = context.user_data["join_target"]
    m1c = context.user_data["mode1_count"]
    m2c = context.user_data["mode2_count"]
    m3c = context.user_data["mode3_count"]
    total = context.user_data["join_total"]

    all_accounts = await db.get_all_accounts()
    available = [a for a in all_accounts if not a.get("currently_busy", False)]
    random.shuffle(available)

    if len(available) < total:
        await update.message.reply_text(f"❌ Only {len(available)} available. Need {total}.")
        return ConversationHandler.END

    selected = available[:total]
    account_modes = []
    idx = 0
    for _ in range(m1c):
        account_modes.append((selected[idx], 1)); idx += 1
    for _ in range(m2c):
        account_modes.append((selected[idx], 2)); idx += 1
    for _ in range(m3c):
        account_modes.append((selected[idx], 3)); idx += 1

    status_msg = await update.message.reply_text(
        f"🚀 Joining {total} accounts...\n"
        f"Target: `{target_text}`\n"
        f"Modes: {m1c} online / {m2c} hidden / {m3c} offline\n"
        f"Timing: {min_s}-{max_s}s\n"
        f"Use /stop to abort.",
        parse_mode="Markdown"
    )

    global _join_stop
    _join_stop.clear()
    await db.set_process_running("join", True)

    context.application.create_task(
        _execute_join(update, context, account_modes, target_text, min_s, max_s, status_msg)
    )

    context.user_data.clear()
    return ConversationHandler.END


async def _execute_join(update, context, account_modes, target_text, min_s, max_s, status_msg):
    """Execute join — each account gets a fresh client, joins, then mode enforced."""
    target = target_text.strip()
    invite_hash = None
    username = None

    if target.startswith("https://t.me/+"):
        invite_hash = target.replace("https://t.me/+", "").replace("+", "")
    else:
        if "t.me/" in target:
            username = target.split("t.me/")[-1].split("/")[0]
        elif target.startswith("@"):
            username = target[1:]
        else:
            username = target

    results = {"success": 0, "fail": 0}
    total = len(account_modes)

    for idx, (acc, mode) in enumerate(account_modes):
        if _join_stop.is_set():
            await status_msg.edit_text(
                f"⏹ Stopped. {idx}/{total} | ✅ {results['success']} ❌ {results['fail']}",
                parse_mode="Markdown"
            )
            await db.set_process_running("join", False)
            return

        phone = acc["_id"]
        ss = acc["session_string"]

        try:
            # Fresh client per account
            session = StringSession(ss)
            from telethon import TelegramClient
            from config import API_ID, API_HASH
            async with TelegramClient(session, API_ID, API_HASH) as client:
                await client.connect()
                if not await client.is_user_authorized():
                    results["fail"] += 1
                    continue

                # Come online
                await client(functions.account.UpdateStatusRequest(offline=False))
                await asyncio.sleep(1)

                # Join
                try:
                    if invite_hash:
                        await client(ImportChatInviteRequest(invite_hash))
                    else:
                        entity = await client.get_entity(username)
                        await client(functions.channels.JoinChannelRequest(entity))
                except UserAlreadyParticipantError:
                    pass  # Still counts as success
                except Exception as e:
                    results["fail"] += 1
                    logger.error(f"Join failed for {phone}: {e}")
                    continue

            results["success"] += 1
            logger.info(f"✅ {phone} joined successfully (mode {mode})")

            # CRITICAL: Apply mode condition strictly AFTER joining
            # This updates the profile to match the mode
            await client_manager.enforce_mode(phone, ss, mode)

        except FloodWaitError as e:
            logger.warning(f"Flood on {phone}: {e.seconds}s")
            results["fail"] += 1
            await asyncio.sleep(min(e.seconds, 5))
        except Exception as e:
            logger.error(f"Error for {phone}: {e}")
            results["fail"] += 1

        # Random delay
        delay = random.uniform(min_s, max_s)
        await asyncio.sleep(delay)

        if (idx + 1) % 5 == 0 or idx == total - 1:
            try:
                await status_msg.edit_text(
                    f"🔄 {idx + 1}/{total} | ✅ {results['success']} ❌ {results['fail']}",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    await db.set_process_running("join", False)
    await status_msg.edit_text(
        f"🎉 *Join Complete!*\n"
        f"• Total: {total}\n"
        f"• ✅ Joined: {results['success']}\n"
        f"• ❌ Failed: {results['fail']}\n"
        f"• ✅ Mode conditions enforced on all accounts.\n"
        f"  - Mode 1: Online permanently\n"
        f"  - Mode 2: Last seen hidden\n"
        f"  - Mode 3: Will go offline in 2 min",
        parse_mode="Markdown"
    )


async def stop_join():
    global _join_stop
    _join_stop.set()


async def cancel(update: Update, context):
    await update.message.reply_text("❌ Cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


def get_join_handler():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(join_start, pattern="^join$")],
        states={
            JOIN_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, join_receive_link)],
            JOIN_MODES: [MessageHandler(filters.TEXT & ~filters.COMMAND, join_receive_modes)],
            JOIN_TIMING: [MessageHandler(filters.TEXT & ~filters.COMMAND, join_receive_timing)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
  )
