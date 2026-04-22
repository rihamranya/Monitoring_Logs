import json
import logging
import time
from typing import Optional

import redis.asyncio as redis

from app.models.alert import Drain3Webhook
from app.utils.config import settings

logger = logging.getLogger(__name__)


class Drain3Store:
    """
    Store drain3 template events in Redis to correlate with Grafana alerts.
    We keep a short window of events per service_name.
    """

    def __init__(self) -> None:
        self.redis_url = f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}"
        self.client: Optional[redis.Redis] = None
        self.ttl_seconds = settings.DRAIN3_EVENT_TTL_SECONDS

    async def connect(self) -> None:
        if self.client is None:
            self.client = redis.from_url(self.redis_url, decode_responses=True)
            await self.client.ping()
            logger.info("Redis connected for drain3_store")

    async def disconnect(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None
            logger.info("Redis disconnected for drain3_store")

    def _key(self, service_name: str) -> str:
        return f"drain3:events:{service_name}"

    async def record(self, event: Drain3Webhook) -> None:
        if self.client is None:
            await self.connect()

        service_name = str(event.metadata.get("service_name") or event.metadata.get("service") or "unknown-service")
        now = time.time()
        payload = {
            "cluster_id": event.cluster_id,
            "template": event.template,
            "ts": (event.timestamp.isoformat() if event.timestamp else None),
            "metadata": event.metadata or {},
            "recorded_at": now,
        }
        key = self._key(service_name)
        # We use a ZSET score=epoch seconds for efficient range queries.
        await self.client.zadd(key, {json.dumps(payload, ensure_ascii=True): now})
        await self.client.expire(key, self.ttl_seconds)

    async def recent_templates(self, service_name: str, lookback_seconds: int) -> list[str]:
        if self.client is None:
            await self.connect()

        now = time.time()
        start = now - float(lookback_seconds)
        key = self._key(service_name)
        raw_items = await self.client.zrangebyscore(key, min=start, max=now)
        templates: list[str] = []
        for raw in raw_items:
            try:
                data = json.loads(raw)
                template = str(data.get("template") or "").strip()
                if template:
                    templates.append(template)
            except Exception:
                continue
        # Deduplicate while preserving order.
        seen: set[str] = set()
        unique: list[str] = []
        for t in templates:
            if t in seen:
                continue
            seen.add(t)
            unique.append(t)
        return unique[: settings.DRAIN3_MAX_TEMPLATES]


drain3_store = Drain3Store()

