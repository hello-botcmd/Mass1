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
from telethon.tl.types import PeerChannel
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

    async def get_or_create_client(self, session_string: str, phone: str) -> TelegramClient:
        """Get existing client or create a new one. Returns client or None."""
        # First try cache
        if phone in self.clients:
            try:
                client = self.clients[phone]
                if client.is_connected():
                    return client
                await client.connect()
                if await client.is_user_authorized():
                    return client
            except Exception:
                pass
            # Clean up stale entry
            try:
                del self.clients[phone]
            except KeyError:
                pass

        # Create fresh
        try:
            return await self.create_client(session_string, phone)
        except Exception as e:
            logger.error(f"Failed to create client for {phone}: {e}")
            return None

    async def get_client(self, phone: str) -> TelegramClient:
        """Get existing client by phone."""
        if phone in self.clients:
            client = self.clients[phone]
            if client.is_connected():
                return client
            try:
                await client.connect()
                if await client.is_user_authorized():
                    return client
            except Exception:
                pass
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

    # ───────────── MODE ENFORCEMENT ─────────────
    # Each mode profile is STRICTLY applied and enforced

    async def enforce_mode(self, phone: str, session_string: str, mode: int):
        """
        Apply mode condition STRICTLY to an account.
        This creates a fresh client, applies the mode, then keeps it alive.
        Mode 1: Permanent online — continuous keep-alive ping
        Mode 2: Hide last seen from everyone — sets privacy + stays online briefly
        Mode 3: Online for 2 min then offline
        """
        from database import db

        try:
            # Create fresh client
            client = await self.create_client(session_string, phone)

            # Step 1: Always come online first
            await client(functions.account.UpdateStatusRequest(offline=False))
            await asyncio.sleep(1)

            # Step 2: Apply mode-specific conditions
            if mode == 1:
                # Mode 1: PERMANENT ONLINE
                # Profile status must show "online" at all times
                await client(functions.account.UpdateStatusRequest(offline=False))
                # Start continuous keep-alive ping (every 25s)
                await self.start_online_ping(phone)
                await db.update_account(phone, {
                    "mode": 1,
                    "is_online": True,
                    "last_seen_hidden": False,
                    "status": "online_permanent"
                })
                logger.info(f"✅ Mode 1 applied for {phone}: permanent online")

            elif mode == 2:
                # Mode 2: HIDE LAST SEEN — shows "last seen recently"
                # Apply privacy setting: nobody can see last seen
                await client(functions.account.SetPrivacyRequest(
                    key=types.InputPrivacyKeyStatusTimestamp(),
                    rules=[types.InputPrivacyValueDisallowAll()]
                ))
                await asyncio.sleep(0.5)
                # Keep online briefly so the "recently" status updates
                await client(functions.account.UpdateStatusRequest(offline=False))
                # Start a moderate ping (every 60s is enough, just to keep session alive)
                await self.start_online_ping(phone)
                await db.update_account(phone, {
                    "mode": 2,
                    "is_online": True,
                    "last_seen_hidden": True,
                    "status": "online_hidden"
                })
                logger.info(f"✅ Mode 2 applied for {phone}: last seen hidden (shows 'recently')")

            elif mode == 3:
                # Mode 3: Online for 2 MINUTES then go offline
                await client(functions.account.UpdateStatusRequest(offline=False))
                await db.update_account(phone, {
                    "mode": 3,
                    "is_online": True,
                    "last_seen_hidden": False,
                    "status": "online_temporary"
                })
                logger.info(f"✅ Mode 3 applied for {phone}: will go offline in 120s")
                # Schedule going offline after 120 seconds
                asyncio.create_task(self._scheduled_offline(phone, session_string, 120))

            return True

        except Exception as e:
            logger.error(f"❌ Failed to apply mode {mode} for {phone}: {e}")
            return False

    async def _scheduled_offline(self, phone: str, session_string: str, delay: int):
        """Go offline after a delay (for Mode 3)."""
        from database import db

        await asyncio.sleep(delay)
        try:
            # Create a fresh client just for going offline
            session = StringSession(session_string)
            async with TelegramClient(session, API_ID, API_HASH) as client:
                await client.connect()
                if await client.is_user_authorized():
                    await client(functions.account.UpdateStatusRequest(offline=True))
                    await db.update_account(phone, {
                        "is_online": False,
                        "status": "offline"
                    })
                    await self.stop_online_ping(phone)
                    logger.info(f"✅ Mode 3 complete for {phone}: now offline")
        except Exception as e:
            logger.error(f"Failed to set {phone} offline after delay: {e}")

    async def enforce_modes_for_all_accounts(self):
        """
        Re-apply mode conditions for ALL accounts in DB.
        Used by "All Accounts Online" button.
        """
        from database import db
        all_accounts = await db.get_all_accounts()
        results = {"mode1": 0, "mode2": 0, "mode3": 0, "failed": 0}

        for acc in all_accounts:
            mode = acc.get("mode")
            if not mode:
                results["failed"] += 1
                continue

            phone = acc["_id"]
            ss = acc["session_string"]
            ok = await self.enforce_mode(phone, ss, mode)
            if ok:
                results[f"mode{mode}"] += 1
            else:
                results["failed"] += 1
            await asyncio.sleep(1.5)  # Be polite

        return results

    # ───────────── Online Keep-Alive ─────────────

    async def start_online_ping(self, phone: str):
        """Start background task to keep account online (ping every 25s)."""
        async with self._lock:
            if phone in self.online_tasks and not self.online_tasks[phone].done():
                return

            async def _ping_loop():
                from database import db
                try:
                    while True:
                        try:
                            client = await self.get_client(phone)
                            await client(functions.account.UpdateStatusRequest(offline=False))
                        except (KeyError, ConnectionError):
                            # Client gone, try recreating from DB
                            acc = await db.get_account(phone)
                            if acc and acc.get("session_string"):
                                try:
                                    client = await self.create_client(acc["session_string"], phone)
                                    await client(functions.account.UpdateStatusRequest(offline=False))
                                except Exception as e2:
                                    logger.error(f"Ping reconnect failed for {phone}: {e2}")
                        except Exception as e:
                            logger.error(f"Ping error for {phone}: {e}")
                        await asyncio.sleep(25)
                except asyncio.CancelledError:
                    logger.info(f"Online ping cancelled for {phone}")
                except Exception as e:
                    logger.error(f"Ping fatal for {phone}: {e}")

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

    async def set_online(self, phone: str):
        """Set account online one-time."""
        try:
            client = await self.get_client(phone)
            await client(functions.account.UpdateStatusRequest(offline=False))
        except Exception as e:
            logger.error(f"Failed to set {phone} online: {e}")

    async def set_hide_last_seen(self, phone: str):
        """Hide last seen timestamp from everyone."""
        try:
            client = await self.get_client(phone)
            await client(functions.account.SetPrivacyRequest(
                key=types.InputPrivacyKeyStatusTimestamp(),
                rules=[types.InputPrivacyValueDisallowAll()]
            ))
            logger.info(f"✅ Hide last seen set for {phone}")
            return True
        except Exception as e:
            logger.error(f"Failed to hide last seen for {phone}: {e}")
            return False

    # ───────────── Session Validation ─────────────

    async def validate_session(self, session_string: str) -> tuple:
        """Validate a session string. Returns (success, phone, error)."""
        client = None
        try:
            session = StringSession(session_string)
            client = TelegramClient(session, API_ID, API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                return False, None, "Session is not authorized"
            me = await client.get_me()
            phone = me.phone or str(me.id)
            await client.disconnect()
            return True, phone, None
        except Exception as e:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            return False, None, str(e)[:200]

    # ───────────── Phone Login Flow ─────────────

    async def start_phone_login(self, phone: str) -> tuple:
        """Send code request to phone."""
        phone = phone.strip()
        if not phone.startswith("+"):
            phone = "+" + phone

        session = StringSession()
        client = TelegramClient(session, API_ID, API_HASH)
        await client.connect()

        if await client.is_user_authorized():
            await client.disconnect()
            raise ValueError(f"Phone {phone} already has a session. Use session string.")

        try:
            result = await client.send_code_request(phone, force_sms=False)
        except PhoneNumberInvalidError:
            await client.disconnect()
            raise ValueError("Invalid phone number. Use +1234567890 format")
        except PhoneNumberBannedError:
            await client.disconnect()
            raise ValueError("Phone number is banned from Telegram")
        except ApiIdInvalidError:
            await client.disconnect()
            raise ValueError("Invalid API_ID/API_HASH in config.py")
        except RPCError as e:
            await client.disconnect()
            raise ValueError(f"Telegram says: {str(e)[:200]}")
        except Exception as e:
            await client.disconnect()
            raise ValueError(str(e)[:200])

        self._pending_logins[phone] = {
            "client": client,
            "phone_code_hash": result.phone_code_hash,
        }
        return client, result.phone_code_hash

    async def submit_otp(self, phone: str, code: str) -> tuple:
        """Submit OTP. Returns (success, session_string or None, error)."""
        if not phone.startswith("+"):
            phone = "+" + phone

        pending = self._pending_logins.get(phone)
        if not pending:
            return False, None, "No pending login. Use /cancel and start again."

        client = pending["client"]
        phone_code_hash = pending["phone_code_hash"]

        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            session_string = client.session.save()
            self.clients[phone] = client
            self._pending_logins.pop(phone, None)
            return True, session_string, None
        except SessionPasswordNeededError:
            pending["awaiting_2fa"] = True
            return False, None, "2FA_REQUIRED"
        except PhoneCodeInvalidError:
            return False, None, "Invalid OTP code."
        except PhoneCodeExpiredError:
            await client.disconnect()
            self._pending_logins.pop(phone, None)
            return False, None, "OTP expired. Restart."
        except Exception as e:
            await client.disconnect()
            self._pending_logins.pop(phone, None)
            return False, None, str(e)[:200]

    async def submit_2fa(self, phone: str, password: str) -> tuple:
        """Submit 2FA password."""
        if not phone.startswith("+"):
            phone = "+" + phone

        pending = self._pending_logins.get(phone)
        if not pending or not pending.get("awaiting_2fa"):
            return False, None, "No pending 2FA. Restart login."

        client = pending["client"]
        try:
            await client.sign_in(password=password)
            session_string = client.session.save()
            self.clients[phone] = client
            self._pending_logins.pop(phone, None)
            return True, session_string, None
        except Exception as e:
            return False, None, f"Wrong 2FA: {str(e)[:200]}"

    async def cancel_pending_login(self, phone: str):
        """Cancel in-progress phone login."""
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
