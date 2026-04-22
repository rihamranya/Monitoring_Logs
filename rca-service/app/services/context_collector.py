"""
Service pour collecter le contexte des alertes depuis Prometheus, Loki et Jaeger
"""
import httpx
import asyncio
import logging
from typing import Optional
from datetime import datetime, timedelta, timezone
import time
import json

from app.models.alert import (
    AlertPayload, AlertContext, MetricsContext, 
    LogsContext, TracesContext
)
from app.utils.config import settings

logger = logging.getLogger(__name__)


class ContextCollectorService:
    """Collecte le contexte multi-source pour une alerte"""
    
    def __init__(self):
        self.prometheus_url = settings.PROMETHEUS_URL
        self.loki_url = settings.LOKI_URL
        self.jaeger_url = settings.JAEGER_URL
        self.timeout = httpx.Timeout(settings.SOURCE_TIMEOUT_SECONDS)
    
    async def collect_context(self, alert: AlertPayload) -> AlertContext:
        """
        Collecter le contexte en parallèle depuis toutes les sources
        """
        logger.info(f"Collecting context for alert: {alert.alert_name}")
        total_start = time.perf_counter()
        notes: list[str] = []
        source_latencies_ms: dict[str, int] = {}

        async def timed_call(name: str, coro):
            start = time.perf_counter()
            try:
                result = await asyncio.wait_for(coro, timeout=settings.SOURCE_TIMEOUT_SECONDS)
                source_latencies_ms[name] = int((time.perf_counter() - start) * 1000)
                return result
            except asyncio.TimeoutError:
                source_latencies_ms[name] = int((time.perf_counter() - start) * 1000)
                notes.append(f"{name} timeout after {settings.SOURCE_TIMEOUT_SECONDS}s")
                return None
            except Exception as exc:
                source_latencies_ms[name] = int((time.perf_counter() - start) * 1000)
                notes.append(f"{name} failed: {exc}")
                return None

        metrics_task = timed_call("prometheus", self._fetch_metrics(alert))
        logs_task = timed_call("loki", self._fetch_logs(alert))
        traces_task = timed_call("jaeger", self._fetch_traces(alert))
        metrics, logs, traces = await asyncio.gather(metrics_task, logs_task, traces_task)
        source_latencies_ms["total"] = int((time.perf_counter() - total_start) * 1000)

        if not any([metrics, logs, traces]):
            notes.append("all_sources_unavailable_degraded_mode")

        logger.info(
            "context_latency alert=%s prometheus_ms=%s loki_ms=%s jaeger_ms=%s total_ms=%s",
            alert.alert_name,
            source_latencies_ms.get("prometheus", -1),
            source_latencies_ms.get("loki", -1),
            source_latencies_ms.get("jaeger", -1),
            source_latencies_ms.get("total", -1),
        )

        return AlertContext(
            alert=alert,
            metrics=metrics,
            logs=logs,
            traces=traces,
            drain3_templates=self._extract_drain3_templates(alert),
            source_latencies_ms=source_latencies_ms,
            context_notes=notes,
        )

    def _extract_drain3_templates(self, alert: AlertPayload) -> list[str]:
        """
        Drain3 correlation is attached during triage into alert.tags["drain3_templates_json"].
        Keep it out of labels to avoid Loki selector issues.
        """
        try:
            raw = (alert.tags or {}).get("drain3_templates_json")
            if not raw:
                return []
            items = json.loads(raw)
            if not isinstance(items, list):
                return []
            templates = [str(x).strip() for x in items if str(x).strip()]
            return templates[: settings.DRAIN3_MAX_TEMPLATES]
        except Exception:
            return []
    
    async def _fetch_metrics(self, alert: AlertPayload) -> Optional[MetricsContext]:
        """
        Récupérer les métriques Prometheus pour l'alerte
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                tags = alert.tags or {}
                candidates: list[str] = []
                instance = tags.get("instance") or alert.hostname
                if instance:
                    candidates.append(f'up{{instance=~"{instance}.*"}}')
                job = tags.get("job")
                if job:
                    candidates.append(f'up{{job=~"{job}.*"}}')
                service_name = tags.get("service_name")
                if service_name:
                    candidates.append(f'up{{service_name=~"{service_name}.*"}}')
                service = tags.get("service")
                if service:
                    candidates.append(f'up{{service=~"{service}.*"}}')
                candidates.append("up")

                end = datetime.now(timezone.utc)
                start = end - timedelta(minutes=settings.CONTEXT_LOOKBACK_MINUTES)
                attempted: list[str] = []
                for query in candidates:
                    attempted.append(query)
                    response = await client.get(
                        f"{self.prometheus_url}/api/v1/query_range",
                        params={
                            "query": query,
                            "start": start.timestamp(),
                            "end": end.timestamp(),
                            "step": "30s",
                        },
                    )
                    response.raise_for_status()
                    data = response.json()
                    if data.get("status") == "success" and data.get("data", {}).get("result"):
                        result = data["data"]["result"][0]
                        return MetricsContext(
                            query=query,
                            values=[
                                {
                                    "metric": result.get("metric", {}),
                                    "timestamps_values": result.get("values", []),
                                }
                            ],
                            notes=[f"attempted_queries={attempted}"],
                        )
                return None
        except Exception as e:
            logger.error(f"Error fetching metrics from Prometheus: {e}")
            raise
    
    async def _fetch_logs(self, alert: AlertPayload) -> Optional[LogsContext]:
        """
        Récupérer les logs Loki pour l'alerte
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                tags = alert.tags or {}
                candidates: list[str] = []
                host_name = tags.get("host_name")
                if host_name:
                    candidates.append(f'{{host_name="{host_name}"}}')
                if alert.hostname:
                    candidates.append(f'{{hostname="{alert.hostname}"}}')
                    candidates.append(f'{{host_name="{alert.hostname}"}}')
                service_name = tags.get("service_name")
                if service_name:
                    candidates.append(f'{{service_name="{service_name}"}}')
                service = tags.get("service")
                if service:
                    candidates.append(f'{{service="{service}"}}')

                now_ms = int(datetime.utcnow().timestamp() * 1e9)
                start_ms = now_ms - int(settings.CONTEXT_LOOKBACK_MINUTES * 60 * 1e9)
                attempted: list[str] = []
                for label_query in candidates:
                    attempted.append(label_query)
                    response = await client.get(
                        f"{self.loki_url}/loki/api/v1/query_range",
                        params={
                            "query": label_query,
                            "start": start_ms,
                            "end": now_ms,
                            "limit": 50,
                        },
                    )
                    response.raise_for_status()
                    data = response.json()
                    if data.get("status") == "success":
                        streams = data.get("data", {}).get("result", [])
                        logs_list = []
                        for stream in streams:
                            for value in stream.get("values", []):
                                raw_line = value[1] if len(value) > 1 else ""
                                logs_list.append(
                                    {
                                        "ts": value[0],
                                        "line": raw_line,
                                        "drain3_annotation": self._drain3_annotate(raw_line),
                                    }
                                )
                        if logs_list:
                            return LogsContext(
                                logs=logs_list[:50],
                                total_entries=len(logs_list),
                                notes=[f"query={label_query}", f"attempted_queries={attempted}"],
                            )
                if attempted:
                    return LogsContext(logs=[], total_entries=0, notes=[f"attempted_queries={attempted}"])
                return None
        except Exception as e:
            logger.error(f"Error fetching logs from Loki: {e}")
            raise
    
    async def _fetch_traces(self, alert: AlertPayload) -> Optional[TracesContext]:
        """
        Récupérer les traces Jaeger associées à l'alerte
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.jaeger_url}/api/traces",
                    params={
                        "service": alert.tags.get("service") or alert.hostname or "unknown",
                        "limit": 20,
                    },
                )
                response.raise_for_status()
                data = response.json()
                if data.get("data"):
                    traces = data["data"]
                    if traces:
                        trace_id = traces[0].get("traceID")
                        raw_spans = traces[0].get("spans", [])
                        spans = [
                            {
                                "trace_id": trace_id,
                                "span_name": span.get("operationName"),
                                "duration_us": span.get("duration"),
                                "error": bool(span.get("tags")) and any(
                                    t.get("key") == "error" and str(t.get("value")).lower() in {"true", "1"}
                                    for t in span.get("tags", [])
                                ),
                            }
                            for span in raw_spans
                        ]
                        errors = sum(1 for span in spans if span["error"])
                        error_rate = (errors / len(spans)) if spans else 0.0
                        return TracesContext(
                            trace_id=trace_id,
                            spans=spans,
                            error_rate=error_rate,
                        )
                return None
        except Exception as e:
            logger.error(f"Error fetching traces from Jaeger: {e}")
            raise

    def _drain3_annotate(self, line: str) -> str:
        # Placeholder Drain3: normalise les chiffres pour produire un template.
        chars = ["<NUM>" if ch.isdigit() else ch for ch in line]
        return "".join(chars)


# Instance globale
context_collector = ContextCollectorService()
