import asyncio
import os
import logging
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
from config import API_ID, API_HASH, SESSION_DIR

logger = logging.getLogger(__name__)


class ClientManager:
    def __init__(self):
        self.clients = {}       # phone -> TelegramClient
        self.online_tasks = {}  # phone -> asyncio.Task (background ping)
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
            await client.connect()
            if await client.is_user_authorized():
                return client
            # Session expired
            del self.clients[phone]
            raise ConnectionError(f"Session for {phone} expired")
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
                return  # Already running

            async def _ping_loop():
                try:
                    while True:
                        client = await self.get_client(phone)
                        await client(functions.account.UpdateStatusRequest(offline=False))
                        await asyncio.sleep(25)
                except asyncio.CancelledError:
                    logger.info(f"Online ping cancelled for {phone}")
                except Exception as e:
                    logger.error(f"Online ping error for {phone}: {e}")

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
        """Hide last seen timestamp (shows 'last seen recently')."""
        try:
            client = await self.get_client(phone)
            await client(functions.account.SetPrivacyRequest(
                key=types.InputPrivacyKeyStatusTimestamp(),
                rules=[types.InputPrivacyValueDisallowAll()]
            ))
            logger.info(f"Hide last seen set for {phone}")
        except Exception as e:
            logger.error(f"Failed to hide last seen for {phone}: {e}")

    async def set_online(self, phone: str):
        """Set account online one-time (call start_online_ping for persist)."""
        try:
            client = await self.get_client(phone)
            await client(functions.account.UpdateStatusRequest(offline=False))
        except Exception as e:
            logger.error(f"Failed to set {phone} online: {e}")

    # ───────────── Phone Login Flow ─────────────

    async def start_phone_login(self, phone: str) -> tuple:
        """
        Create a fresh client and send the code request.
        Returns (client, phone_code_hash) on success, raises on error.
        """
        session = StringSession()  # Empty session string
        client = TelegramClient(session, API_ID, API_HASH)
        await client.connect()
        
        # Ensure we're not authorized yet
        if await client.is_user_authorized():
            await client.disconnect()
            raise ValueError(f"Phone {phone} is already authorized. Use session string instead.")
        
        result = await client.send_code_request(phone)
        # Store in a temporary dict so we can retrieve it later
        self._pending_logins[phone] = {
            "client": client,
            "phone_code_hash": result.phone_code_hash,
        }
        return client, result.phone_code_hash

    async def submit_otp(self, phone: str, code: str) -> tuple:
        """
        Submit OTP code for phone login.
        Returns (success: bool, session_string: str or None, error: str or None).
        If 2FA is required, returns (False, None, "2FA_REQUIRED") — call submit_2fa next.
        """
        from telethon.errors import SessionPasswordNeededError
        
        pending = self._pending_logins.get(phone)
        if not pending:
            return False, None, "No pending login for this phone. Start again."
        
        client = pending["client"]
        phone_code_hash = pending["phone_code_hash"]
        
        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            # Success! No 2FA needed
            session_string = client.session.save()
            # Clean up pending
            self.clients[phone] = client
            self._pending_logins.pop(phone, None)
            return True, session_string, None
        except SessionPasswordNeededError:
            # 2FA is needed — keep client, mark it
            pending["awaiting_2fa"] = True
            return False, None, "2FA_REQUIRED"
        except Exception as e:
            await client.disconnect()
            self._pending_logins.pop(phone, None)
            return False, None, str(e)

    async def submit_2fa(self, phone: str, password: str) -> tuple:
        """
        Submit 2FA password for phone login.
        Returns (success: bool, session_string: str or None, error: str or None).
        """
        pending = self._pending_logins.get(phone)
        if not pending or not pending.get("awaiting_2fa"):
            return False, None, "No pending 2FA for this phone. Start again."
        
        client = pending["client"]
        
        try:
            await client.sign_in(password=password)
            session_string = client.session.save()
            self.clients[phone] = client
            self._pending_logins.pop(phone, None)
            return True, session_string, None
        except Exception as e:
            return False, None, str(e)

    async def cancel_pending_login(self, phone: str):
        """Cancel an in-progress phone login and disconnect the client."""
        pending = self._pending_logins.pop(phone, None)
        if pending:
            try:
                await pending["client"].disconnect()
            except Exception:
                pass
                
    async def validate_session(self, session_string: str) -> tuple:
        """
        Validate a session string by connecting and checking auth.
        Returns (success: bool, phone: str or None, error: str or None)
        """
        phone = None
        client = None
        try:
            session = StringSession(session_string)
            client = TelegramClient(session, API_ID, API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                return False, None, "Session is not authorized"
            me = await client.get_me()
            phone = me.phone
            if not phone:
                # Fallback: use id as identifier
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


# Global instance
client_manager = ClientManager()
