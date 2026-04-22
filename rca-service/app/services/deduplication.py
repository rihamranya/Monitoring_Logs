import json
import logging
import hashlib
from typing import Optional
import redis.asyncio as redis

from app.models.alert import AlertPayload
from app.utils.config import settings

logger = logging.getLogger(__name__)

class DeduplicationService:

    def __init__(self):
        self.dedup_window = settings.DEDUP_WINDOW_SECONDS
        self.redis_url = f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}"
        self.client: Optional[redis.Redis] = None

    async def connect(self) -> None:
        self.client = redis.from_url(self.redis_url, decode_responses=True)
        await self.client.ping()
        logger.info("Redis connected for deduplication")

    async def disconnect(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None
            logger.info("Redis disconnected")

    def _key_parts(self, alert: AlertPayload) -> tuple[str, str]:
        return alert.alert_name, (alert.hostname or "unknown")

    def _state_key(self, alert: AlertPayload) -> str:
        alert_name, hostname = self._key_parts(alert)
        raw = f"{alert_name}|{hostname}"
        digest = hashlib.md5(raw.encode()).hexdigest()
        return f"dedup:state:{digest}"

    async def should_process(self, alert: AlertPayload, status: str = "firing") -> tuple[bool, int, int]:
        """
        Retourne:
        - should_process: False si duplicate dans fenêtre
        - duplicate_count: nombre de duplicates accumulés
        - window_remaining_seconds
        """
        if self.client is None:
            await self.connect()

        alert_name, hostname = self._key_parts(alert)
        key = self._state_key(alert)

        raw_state = await self.client.get(key)
        ttl_seconds = await self.client.ttl(key)
        remaining = max(0, int(ttl_seconds if ttl_seconds and ttl_seconds > 0 else 0))

        if raw_state:
            state = json.loads(raw_state)
            duplicate_count = int(state.get("duplicate_count", 0))
            last_status = state.get("last_status", "firing")

            if last_status == "resolved" and status == "firing":
                state["last_status"] = "firing"
                await self.client.setex(key, self.dedup_window, json.dumps(state))
                logger.info(
                    "dedup_event alertname=%s instance=%s action=reprocess_after_resolve duplicate_count=%s",
                    alert_name, hostname, duplicate_count
                )
                return True, duplicate_count, self.dedup_window

            duplicate_count += 1
            state["duplicate_count"] = duplicate_count
            state["last_status"] = status
            await self.client.setex(key, remaining or self.dedup_window, json.dumps(state))
            logger.info(
                "dedup_event alertname=%s instance=%s action=deduplicated duplicate_count=%s window_remaining_seconds=%s",
                alert_name, hostname, duplicate_count, remaining
            )
            return False, duplicate_count, remaining

        new_state = {"last_status": status, "duplicate_count": 0}
        await self.client.setex(key, self.dedup_window, json.dumps(new_state))
        logger.info(
            "dedup_event alertname=%s instance=%s action=process_new duplicate_count=0",
            alert_name, hostname
        )
        return True, 0, self.dedup_window

    async def record_resolved(self, alert: AlertPayload) -> None:
        if self.client is None:
            await self.connect()

        key = self._state_key(alert)
        raw_state = await self.client.get(key)
        if raw_state:
            state = json.loads(raw_state)
            state["last_status"] = "resolved"
            await self.client.setex(key, self.dedup_window, json.dumps(state))

    async def mark_llm_invocation(self) -> None:
        if self.client is None:
            await self.connect()
        await self.client.incr("dedup:llm_invocations")

    async def llm_invocations(self) -> int:
        if self.client is None:
            await self.connect()
        count = await self.client.get("dedup:llm_invocations")
        return int(count or 0)


# Instance globale
dedup_service = DeduplicationService()
