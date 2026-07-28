import motor.motor_asyncio
from datetime import datetime, timezone
from config import MONGO_URI, DB_NAME


class Database:
    def __init__(self):
        self.client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        self.db = self.client[DB_NAME]
        self.accounts = self.db["accounts"]
        self.process_state = self.db["process_state"]

    # ───────────── Account CRUD ─────────────

    async def add_account(self, phone: str, session_string: str) -> bool:
        """Add a new account. Returns False if phone already exists."""
        existing = await self.accounts.find_one({"_id": phone})
        if existing:
            return False
        await self.accounts.insert_one({
            "_id": phone,
            "session_string": session_string,
            "phone": phone,
            "added_at": datetime.now(timezone.utc),
            "is_online": False,
            "status": "idle",       # idle | joining | reacting | viewing | online
            "mode": None,           # 1 | 2 | 3 (join mode assignment)
            "last_seen_hidden": False,
            "currently_busy": False,
            "error": None,
        })
        return True

    async def remove_account(self, phone: str) -> bool:
        result = await self.accounts.delete_one({"_id": phone})
        return result.deleted_count > 0

    async def get_account(self, phone: str):
        return await self.accounts.find_one({"_id": phone})

    async def get_all_accounts(self):
        cursor = self.accounts.find({})
        return await cursor.to_list(length=None)

    async def get_accounts_by_status(self, status: str):
        cursor = self.accounts.find({"status": status})
        return await cursor.to_list(length=None)

    async def get_idle_accounts(self):
        cursor = self.accounts.find({"currently_busy": False})
        return await cursor.to_list(length=None)

    async def get_accounts_count(self):
        return await self.accounts.count_documents({})

    async def get_active_accounts_count(self):
        """Accounts that are online and working."""
        return await self.accounts.count_documents({"is_online": True})

    async def update_account(self, phone: str, update_data: dict):
        await self.accounts.update_one(
            {"_id": phone},
            {"$set": update_data}
        )

    async def mark_busy(self, phone: str, busy: bool = True):
        await self.accounts.update_one(
            {"_id": phone},
            {"$set": {"currently_busy": busy}}
        )

    async def mark_online(self, phone: str, online: bool = True):
        await self.accounts.update_one(
            {"_id": phone},
            {"$set": {"is_online": online}}
        )

    async def set_status(self, phone: str, status: str):
        await self.accounts.update_one(
            {"_id": phone},
            {"$set": {"status": status}}
        )

    async def reset_all_busy(self):
        await self.accounts.update_many(
            {},
            {"$set": {"currently_busy": False, "status": "idle"}}
        )

    async def set_all_offline(self):
        await self.accounts.update_many(
            {},
            {"$set": {"is_online": False, "status": "idle", "currently_busy": False}}
        )

    async def set_all_online_flag(self, online: bool = True):
        await self.accounts.update_many(
            {},
            {"$set": {"is_online": online}}
        )

    # ───────────── Process State ─────────────

    async def set_process_state(self, key: str, value):
        await self.process_state.update_one(
            {"_id": key},
            {"$set": {"value": value}},
            upsert=True
        )

    async def get_process_state(self, key: str):
        doc = await self.process_state.find_one({"_id": key})
        return doc["value"] if doc else None

    async def delete_process_state(self, key: str):
        await self.process_state.delete_one({"_id": key})

    async def is_process_running(self, process_name: str) -> bool:
        val = await self.get_process_state(f"running:{process_name}")
        return val is True

    async def set_process_running(self, process_name: str, running: bool):
        await self.set_process_state(f"running:{process_name}", running)

    async def stop_all_processes(self):
        """Reset all process running flags."""
        await self.process_state.delete_many({"_id": {"$regex": "^running:"}})

    async def cleanup(self):
        await self.client.close()


# Global instance
db = Database()
