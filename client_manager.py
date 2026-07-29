import asyncio
import os
import logging
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    ApiIdInvalidError,
    PhoneNumberBannedError,
    RPCError,
)
from config import API_ID, API_HASH, SESSION_DIR

logger = logging.getLogger(__name__)


class ClientManager:
    def __init__(self):
        self.clients = {}            # phone -> TelegramClient
        self.online_tasks = {}       # phone -> asyncio.Task (background ping)
        self._pending_logins = {}    # phone -> {client, phone_code_hash, awaiting_2fa}
        self._lock = asyncio.Lock()

    async def create_client(self, session_string: str, phone: str) -> TelegramClient:
        """Create and connect a Telethon client from a session string."""
        session = StringSession(session_string)
        client = TelegramClient(session, API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError(f"Session for {phone} is not authorized")
        self.clients[phone] = client
        logger.info(f"Client for {phone} connected successfully")
        return client

    async def get_client(self, phone: str) -> TelegramClient:
        """Get existing client or create from stored session."""
        if phone in self.clients:
            client = self.clients[phone]
            if client.is_connected():
                return client
            # Reconnect
            try:
                await client.connect()
                if await client.is_user_authorized():
                    return client
            except Exception:
                pass
            # Session expired — clean up
            del self.clients[phone]
            raise ConnectionError(f"Session for {phone} expired or invalid")

        # Try to find in database and recreate
        from database import db
        acc = await db.get_account(phone)
        if acc and acc.get("session_string"):
            return await self.create_client(acc["session_string"], phone)

        raise KeyError(f"No client found for {phone}")

    async def disconnect_client(self, phone: str):
        """Disconnect and remove a client."""
        async with self._lock:
            await self.stop_online_ping(phone)
            if phone in self.clients:
                try:
                    await self.clients[phone].disconnect()
                except Exception:
                    pass
                del self.clients[phone]
                logger.info(f"Client for {phone} disconnected")

    async def disconnect_all(self):
        """Disconnect all clients."""
        phones = list(self.clients.keys())
        for phone in phones:
            await self.disconnect_client(phone)

    # ───────────── Online Keep-Alive ─────────────

    async def start_online_ping(self, phone: str):
        """Start background task to keep account online (ping every 25s)."""
        async with self._lock:
            if phone in self.online_tasks and not self.online_tasks[phone].done():
                return

            async def _ping_loop():
                try:
                    while True:
                        try:
                            client = await self.get_client(phone)
                            await client(functions.account.UpdateStatusRequest(offline=False))
                        except (KeyError, ConnectionError):
                            logger.warning(f"Cannot get client for {phone} in ping loop, reconnecting...")
                            # Try to reconnect from DB
                            from database import db
                            acc = await db.get_account(phone)
                            if acc and acc.get("session_string"):
                                try:
                                    client = await self.create_client(acc["session_string"], phone)
                                    await client(functions.account.UpdateStatusRequest(offline=False))
                                except Exception as e2:
                                    logger.error(f"Reconnect failed for {phone}: {e2}")
                        except Exception as e:
                            logger.error(f"Online ping transient error for {phone}: {e}")
                        await asyncio.sleep(25)
                except asyncio.CancelledError:
                    logger.info(f"Online ping cancelled for {phone}")
                except Exception as e:
                    logger.error(f"Online ping fatal error for {phone}: {e}")

            task = asyncio.create_task(_ping_loop())
            self.online_tasks[phone] = task
            logger.info(f"Started online ping for {phone}")

    async def stop_online_ping(self, phone: str):
        """Stop online ping for a specific account."""
        async with self._lock:
            if phone in self.online_tasks:
                self.online_tasks[phone].cancel()
                self.online_tasks[phone] = None
                del self.online_tasks[phone]
                logger.info(f"Stopped online ping for {phone}")

    async def stop_all_online_pings(self):
        """Stop all online ping tasks."""
        phones = list(self.online_tasks.keys())
        for phone in phones:
            await self.stop_online_ping(phone)

    async def set_offline(self, phone: str):
        """Set account offline explicitly."""
        try:
            client = await self.get_client(phone)
            await client(functions.account.UpdateStatusRequest(offline=True))
        except Exception as e:
            logger.warning(f"Could not set {phone} offline: {e}")

    async def set_hide_last_seen(self, phone: str):
        """Hide last seen timestamp from everyone (shows 'last seen recently')."""
        try:
            client = await self.get_client(phone)
            result = await client(functions.account.SetPrivacyRequest(
                key=types.InputPrivacyKeyStatusTimestamp(),
                rules=[
                    types.InputPrivacyValueDisallowAll()
                ]
            ))
            logger.info(f"✅ Hide last seen privacy set for {phone}")
            # Log the resulting rules to confirm
            for rule in result.rules:
                logger.info(f"  Privacy rule for {phone}: {type(rule).__name__}")
            return True
        except Exception as e:
            logger.error(f"❌ Failed to hide last seen for {phone}: {e}")
            return False

    async def set_online(self, phone: str):
        """Set account online one-time."""
        try:
            client = await self.get_client(phone)
            await client(functions.account.UpdateStatusRequest(offline=False))
        except Exception as e:
            logger.error(f"Failed to set {phone} online: {e}")

    async def validate_session(self, session_string: str) -> tuple:
        """
        Validate a session string by connecting and checking auth.
        Returns (success: bool, phone: str or None, error: str or None)
        """
        client = None
        try:
            session = StringSession(session_string)
            client = TelegramClient(session, API_ID, API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                return False, None, "Session is not authorized (user needs to login via phone first)"
            me = await client.get_me()
            phone = me.phone
            if not phone:
                phone = str(me.id)
            await client.disconnect()
            return True, phone, None
        except Exception as e:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            return False, None, str(e)

    # ───────────── Phone Login Flow ─────────────

    async def start_phone_login(self, phone: str) -> tuple:
        """
        Create a fresh client and send the code request.
        Returns (client, phone_code_hash) on success.
        Raises on error with descriptive message.
        """
        # Clean phone number
        phone = phone.strip().lstrip("+")
        if not phone.startswith("+"):
            phone = "+" + phone

        session = StringSession()
        client = TelegramClient(session, API_ID, API_HASH)
        await client.connect()

        # Ensure we're not already authorized
        if await client.is_user_authorized():
            await client.disconnect()
            raise ValueError(f"Phone {phone} already has an active session. Use session string instead.")

        try:
            result = await client.send_code_request(phone, force_sms=False)
        except PhoneNumberInvalidError:
            await client.disconnect()
            raise ValueError("Invalid phone number format. Use international format like +1234567890")
        except PhoneNumberBannedError:
            await client.disconnect()
            raise ValueError("This phone number is banned from Telegram")
        except ApiIdInvalidError:
            await client.disconnect()
            raise ValueError("Invalid API_ID or API_HASH. Check config.py")
        except RPCError as e:
            await client.disconnect()
            raise ValueError(f"Telegram API error: {e}")
        except Exception as e:
            await client.disconnect()
            raise ValueError(f"Connection error: {str(e)[:200]}")

        self._pending_logins[phone] = {
            "client": client,
            "phone_code_hash": result.phone_code_hash,
        }
        return client, result.phone_code_hash

    async def submit_otp(self, phone: str, code: str) -> tuple:
        """
        Submit OTP code for phone login.
        Returns (success: bool, session_string or None, error or None).
        If 2FA required, returns (False, None, "2FA_REQUIRED").
        """
        from telethon.errors import SessionPasswordNeededError

        # Clean phone
        if not phone.startswith("+"):
            phone = "+" + phone

        pending = self._pending_logins.get(phone)
        if not pending:
            return False, None, "No pending login for this phone. Use /cancel and start again."

        client = pending["client"]
        phone_code_hash = pending["phone_code_hash"]

        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            # Success! No 2FA needed
            session_string = client.session.save()
            self.clients[phone] = client
            self._pending_logins.pop(phone, None)
            return True, session_string, None
        except SessionPasswordNeededError:
            pending["awaiting_2fa"] = True
            return False, None, "2FA_REQUIRED"
        except PhoneCodeInvalidError:
            return False, None, "Invalid OTP code. Please check and try again."
        except PhoneCodeExpiredError:
            await client.disconnect()
            self._pending_logins.pop(phone, None)
            return False, None, "OTP code expired. Restart the login process."
        except Exception as e:
            await client.disconnect()
            self._pending_logins.pop(phone, None)
            return False, None, str(e)[:200]

    async def submit_2fa(self, phone: str, password: str) -> tuple:
        """
        Submit 2FA password for phone login.
        Returns (success: bool, session_string or None, error or None).
        """
        if not phone.startswith("+"):
            phone = "+" + phone

        pending = self._pending_logins.get(phone)
        if not pending or not pending.get("awaiting_2fa"):
            return False, None, "No pending 2FA for this phone. Restart the login process."

        client = pending["client"]

        try:
            await client.sign_in(password=password)
            session_string = client.session.save()
            self.clients[phone] = client
            self._pending_logins.pop(phone, None)
            return True, session_string, None
        except Exception as e:
            return False, None, f"Invalid 2FA password: {str(e)[:200]}"

    async def cancel_pending_login(self, phone: str):
        """Cancel an in-progress phone login and disconnect the client."""
        if not phone.startswith("+"):
            phone = "+" + phone
        pending = self._pending_logins.pop(phone, None)
        if pending:
            try:
                await pending["client"].disconnect()
            except Exception:
                pass


# Global instance
client_manager = ClientManager()
