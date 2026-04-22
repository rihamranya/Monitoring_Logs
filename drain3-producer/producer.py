import asyncio
import json
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

import httpx
from drain3.template_miner import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


@dataclass(frozen=True)
class Settings:
    loki_url: str
    loki_query: str
    lookback_seconds: int
    poll_interval_seconds: float
    window_seconds: int
    notify_threshold: int
    notify_cooldown_seconds: int
    rca_webhook_url: str
    request_timeout_seconds: float
    max_lines_per_poll: int


def load_settings() -> Settings:
    return Settings(
        loki_url=os.getenv("LOKI_URL", "http://loki:3100").rstrip("/"),
        # IMPORTANT: Loki requires a selector like {label="value"}.
        loki_query=os.getenv("LOKI_QUERY", '{job=~".+"}'),
        lookback_seconds=_env_int("LOOKBACK_SECONDS", 45),
        poll_interval_seconds=_env_float("POLL_INTERVAL_SECONDS", 10.0),
        window_seconds=_env_int("WINDOW_SECONDS", 120),
        notify_threshold=_env_int("NOTIFY_THRESHOLD", 5),
        notify_cooldown_seconds=_env_int("NOTIFY_COOLDOWN_SECONDS", 180),
        rca_webhook_url=os.getenv("RCA_WEBHOOK_URL", "http://rca-service:8000/webhook/drain3"),
        request_timeout_seconds=_env_float("REQUEST_TIMEOUT_SECONDS", 8.0),
        max_lines_per_poll=_env_int("MAX_LINES_PER_POLL", 300),
    )


def build_template_miner() -> TemplateMiner:
    cfg = TemplateMinerConfig()
    # Drain3 provides sensible defaults out of the box in TemplateMinerConfig()
    cfg.profiling_enabled = False
    return TemplateMiner(config=cfg)


async def query_loki_lines(
    client: httpx.AsyncClient,
    *,
    loki_url: str,
    query: str,
    start_ns: int,
    end_ns: int,
    limit: int,
) -> list[dict[str, Any]]:
    # Loki API: /loki/api/v1/query_range
    params = {
        "query": query,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(limit),
        "direction": "forward",
    }
    r = await client.get(f"{loki_url}/loki/api/v1/query_range", params=params)
    r.raise_for_status()
    payload: dict[str, Any] = r.json()
    data = payload.get("data", {})
    result = data.get("result", [])
    items: list[dict[str, Any]] = []
    for stream in result:
        labels = stream.get("stream", {}) or {}
        values = stream.get("values", [])
        for item in values:
            if isinstance(item, list) and len(item) >= 2:
                items.append({"line": str(item[1]), "labels": labels})
    return items


async def send_webhook(
    client: httpx.AsyncClient,
    *,
    url: str,
    cluster_id: str,
    template: str,
    metadata: dict[str, Any],
) -> None:
    body = {"cluster_id": cluster_id, "template": template, "metadata": metadata}
    r = await client.post(url, json=body)
    r.raise_for_status()


async def run() -> None:
    s = load_settings()
    miner = build_template_miner()

    # Track occurrences per cluster within a sliding time window.
    occurrences: dict[str, deque[float]] = defaultdict(deque)
    last_notified_at: dict[str, float] = {}

    print(
        json.dumps(
            {
                "event": "drain3_producer_started",
                "loki_url": s.loki_url,
                "loki_query": s.loki_query,
                "poll_interval_seconds": s.poll_interval_seconds,
                "lookback_seconds": s.lookback_seconds,
                "window_seconds": s.window_seconds,
                "notify_threshold": s.notify_threshold,
                "notify_cooldown_seconds": s.notify_cooldown_seconds,
                "rca_webhook_url": s.rca_webhook_url,
            }
        ),
        flush=True,
    )

    async with httpx.AsyncClient(timeout=s.request_timeout_seconds) as client:
        while True:
            now = time.time()
            end_ns = int(now * 1_000_000_000)
            start_ns = int((now - s.lookback_seconds) * 1_000_000_000)
            try:
                items = await query_loki_lines(
                    client,
                    loki_url=s.loki_url,
                    query=s.loki_query,
                    start_ns=start_ns,
                    end_ns=end_ns,
                    limit=s.max_lines_per_poll,
                )
            except Exception as e:
                print(json.dumps({"event": "loki_query_error", "error": str(e)}), flush=True)
                await asyncio.sleep(s.poll_interval_seconds)
                continue

            if not items:
                await asyncio.sleep(s.poll_interval_seconds)
                continue

            notified = 0
            for it in items:
                line = str(it.get("line") or "")
                labels = it.get("labels") or {}
                service_name = labels.get("service_name") or labels.get("service") or "unknown-service"
                # Drain3 expects raw strings; it returns a cluster_id and template.
                result = miner.add_log_message(line)
                cluster_id = str(result.get("cluster_id", "unknown"))
                template = str(result.get("template_mined", "")).strip() or line[:200]

                dq = occurrences[cluster_id]
                dq.append(now)

                # Drop events outside the sliding window.
                cutoff = now - s.window_seconds
                while dq and dq[0] < cutoff:
                    dq.popleft()

                count = len(dq)
                if count < s.notify_threshold:
                    continue

                last = last_notified_at.get(cluster_id, 0.0)
                if now - last < s.notify_cooldown_seconds:
                    continue

                try:
                    await send_webhook(
                        client,
                        url=s.rca_webhook_url,
                        cluster_id=cluster_id,
                        template=template,
                        metadata={"service_name": service_name},
                    )
                    last_notified_at[cluster_id] = now
                    notified += 1
                    print(
                        json.dumps(
                            {
                                "event": "drain3_webhook_sent",
                                "cluster_id": cluster_id,
                                "count_in_window": count,
                                "template": template,
                                "service_name": service_name,
                            }
                        ),
                        flush=True,
                    )
                except Exception as e:
                    print(
                        json.dumps(
                            {
                                "event": "drain3_webhook_error",
                                "cluster_id": cluster_id,
                                "error": str(e),
                            }
                        ),
                        flush=True,
                    )

            if notified:
                print(json.dumps({"event": "poll_summary", "lines": len(items), "webhooks": notified}), flush=True)

            await asyncio.sleep(s.poll_interval_seconds)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()

