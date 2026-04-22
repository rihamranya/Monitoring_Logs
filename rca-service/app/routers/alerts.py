"""
Routeur FastAPI pour les webhooks d'alertes
"""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
import logging
import hashlib
from datetime import datetime, timezone
import asyncio
import time
import json

from app.models.alert import (
    AlertPayload,
    AlertContext,
    RCADecision,
    GrafanaWebhook,
    Drain3Webhook,
)
from app.services.deduplication import dedup_service
from app.services.history_store import history_store
from app.services.drain3_store import drain3_store
from app.utils.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


@router.post("/webhook/grafana", status_code=202)
async def receive_grafana_webhook(payload: GrafanaWebhook, background_tasks: BackgroundTasks):
    start = time.perf_counter()
    alert = _grafana_to_alert(payload)
    logger.info(f"Received alert: {alert.alert_name} from {alert.source}")
    try:
        status_text = payload.status.lower()
        if status_text == "resolved":
            await dedup_service.record_resolved(alert)
            return {"status": "resolved_recorded", "alert_id": alert.alert_id}

        should_process, duplicate_count, remaining = await dedup_service.should_process(alert, status=status_text)
        if not should_process:
            return {
                "status": "duplicate_ignored",
                "alert_id": alert.alert_id,
                "window_remaining_seconds": remaining,
                "duplicate_count": duplicate_count,
            }

        # Step 2: Correlate Grafana alert with recent Drain3 templates for same service.
        service_key = (
            alert.tags.get("service_name")
            or alert.tags.get("service")
            or alert.hostname
            or "unknown-service"
        )
        drain3_templates = await drain3_store.recent_templates(
            service_name=service_key,
            lookback_seconds=settings.DRAIN3_CORRELATION_WINDOW_SECONDS,
        )
        if drain3_templates:
            # Keep as JSON string to avoid breaking label selectors.
            alert.tags["drain3_templates_json"] = json.dumps(drain3_templates, ensure_ascii=True)

        # Step 3: Pre-LLM decision (noise suppression).
        suppression_reason = _triage_suppression_reason(alert=alert, drain3_templates=drain3_templates)
        if suppression_reason:
            triage_decision = RCADecision(
                decision="DISMISS",
                severity="low",
                reason=suppression_reason,
                rca="Suppressed by Layer2 triage (no LLM call)",
                anomaly_summary="triage_suppressed",
                suggested_actions=[],
                evidence=[f"service_key={service_key}"],
                confidence=1.0,
            )
            history_store.store_decision(
                alert=alert,
                decision=triage_decision,
                raw_llm_response="",
                pipeline_stage="triage_suppressed",
                timed_out=False,
            )
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            logger.info("suppressed_in_ms=%s reason=%s", elapsed_ms, suppression_reason)
            return {
                "status": "suppressed",
                "alert_id": alert.alert_id,
                "reason": suppression_reason,
                "accepted_in_ms": elapsed_ms,
            }

        background_tasks.add_task(process_alert_background, alert)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        logger.info("accepted_in_ms=%s", elapsed_ms)
        return {
            "status": "accepted",
            "alert_id": alert.alert_id,
            "accepted_in_ms": elapsed_ms,
        }
    except Exception as e:
        logger.error(f"Error processing alert: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error processing alert"
        )


@router.post("/webhook/drain3", status_code=202)
async def receive_drain3_webhook(payload: Drain3Webhook, background_tasks: BackgroundTasks):
    # Persist Drain3 event for correlation (Step 2).
    await drain3_store.record(payload)
    background_tasks.add_task(_process_drain3_background, payload)
    return {"status": "accepted", "cluster_id": payload.cluster_id}


@router.get("/decisions")
async def get_decisions(limit: int = Query(default=50, ge=1, le=500)):
    return {"items": history_store.get_last_decisions(limit=limit), "count": limit}


def _grafana_to_alert(payload: GrafanaWebhook) -> AlertPayload:
    grafana_alert = payload.alerts[0]
    labels = grafana_alert.labels
    annotations = grafana_alert.annotations
    alert_name = labels.get("alertname") or payload.title or "GrafanaAlert"
    severity = (labels.get("severity") or payload.status or "warning").lower()
    hostname = labels.get("instance") or labels.get("host") or labels.get("hostname")
    description = annotations.get("description") or annotations.get("summary") or payload.message or payload.title or alert_name
    raw_fingerprint = grafana_alert.fingerprint or payload.groupKey or f"{alert_name}-{hostname}-{severity}"
    alert_id = hashlib.md5(str(raw_fingerprint).encode()).hexdigest()
    timestamp = grafana_alert.startsAt or datetime.now(timezone.utc)
    tags = {k: str(v) for k, v in labels.items()}
    # Keep useful Grafana links in tags so downstream email can render clickable actions.
    source_link = annotations.get("Source") or annotations.get("source")
    silence_link = annotations.get("Silence") or annotations.get("silence")
    dashboard_link = annotations.get("DashboardURL") or annotations.get("dashboardURL")
    panel_link = annotations.get("PanelURL") or annotations.get("panelURL")
    if source_link:
        tags["grafana_source_url"] = str(source_link)
    if silence_link:
        tags["grafana_silence_url"] = str(silence_link)
    if dashboard_link:
        tags["grafana_dashboard_url"] = str(dashboard_link)
    if panel_link:
        tags["grafana_panel_url"] = str(panel_link)
    return AlertPayload(
        alert_id=alert_id,
        alert_name=alert_name,
        severity=severity,
        hostname=hostname,
        description=description,
        timestamp=timestamp,
        source="grafana",
        tags=tags
    )


async def _process_drain3_background(payload: Drain3Webhook) -> None:
    logger.info("drain3_notification cluster_id=%s template=%s", payload.cluster_id, payload.template)


def _triage_suppression_reason(*, alert: AlertPayload, drain3_templates: list[str]) -> str | None:
    """
    Step 3: Decision
    - suppress if this alert+service was DISMISSed recently
    - suppress if Drain3 templates match known noise patterns
    """
    affected_service = alert.tags.get("service") or alert.tags.get("service_name") or (alert.hostname or "")
    recent = history_store.get_recent_decision_for_alert(
        alert_name=alert.alert_name,
        affected_service=affected_service,
        lookback_minutes=settings.TRIAGE_HISTORY_LOOKBACK_MINUTES,
    )
    if recent and str(recent.get("triage_decision")) == "DISMISS":
        return "recent_dismissed_history"

    patterns = [p.strip() for p in (settings.TRIAGE_NOISE_PATTERNS or "").split(",") if p.strip()]
    if patterns and drain3_templates:
        haystack = "\n".join(drain3_templates).lower()
        for pat in patterns:
            if pat.lower() in haystack:
                return f"noise_pattern:{pat}"
    return None


async def process_alert_background(alert: AlertPayload):
    """
    Traitement en arrière-plan:
    1. Collecter le contexte (SCRUM-77)
    2. Analyser avec LLM (SCRUM-78)
    3. Envoyer l'escalade si nécessaire (SCRUM-79)
    """
    logger.info(f"Starting background processing for alert: {alert.alert_name}")
    pipeline_state = {"stage": "context_collection"}
    started_at = time.perf_counter()
    try:
        await asyncio.wait_for(
            _run_alert_pipeline(alert, pipeline_state),
            timeout=settings.ALERT_PROCESSING_TIMEOUT
        )
    except asyncio.TimeoutError:
        elapsed = int((time.perf_counter() - started_at) * 1000)
        logger.error(
            "Global alert processing timeout (%ss) alertname=%s elapsed_ms=%s pipeline_stage=%s",
            settings.ALERT_PROCESSING_TIMEOUT,
            alert.alert_name,
            elapsed,
            pipeline_state["stage"],
        )
        await _fallback_passthrough_on_timeout(alert, pipeline_state["stage"])
    except Exception as e:
        logger.error(f"Error in background processing: {e}", exc_info=True)


async def _run_alert_pipeline(alert: AlertPayload, pipeline_state: dict[str, str]) -> None:
    """Pipeline RCA principal (context -> LLM -> escalation si nécessaire)."""
    from app.services.context_collector import context_collector
    from app.services.llm_analyzer import llm_analyzer
    from app.services.escalation import escalation_service

    pipeline_state["stage"] = "context_collection"
    alert_context = await context_collector.collect_context(alert)
    logger.info(f"Context collected for alert {alert.alert_id}")
    logger.debug(f"Alert context: {alert_context}")

    pipeline_state["stage"] = "llm_analysis"
    rca_decision, raw_llm_response, _duration_ms = await llm_analyzer.analyze(alert_context)
    await dedup_service.mark_llm_invocation()
    logger.info(f"RCA Decision: {rca_decision.decision} (confidence: {rca_decision.confidence})")

    if rca_decision.decision == "ESCALATE":
        pipeline_state["stage"] = "email_escalation"
        recipients = [settings.NOTIFICATION_EMAIL]
        success = await escalation_service.escalate(alert_context, rca_decision, recipients)
        if success:
            logger.info(f"Alert escalated successfully for {alert.alert_name}")
        else:
            logger.warning(f"Failed to escalate alert {alert.alert_name}")
    else:
        logger.info(f"Alert {alert.alert_name} dismissed by RCA analysis")

    pipeline_state["stage"] = "history_store"
    history_store.store_decision(
        alert=alert,
        decision=rca_decision,
        raw_llm_response=raw_llm_response,
        pipeline_stage="completed",
        timed_out=False,
    )


async def _fallback_passthrough_on_timeout(alert: AlertPayload, stage: str) -> None:
    """
    Safety gate E2-09:
    En cas de timeout global pipeline, escalader l'alerte brute.
    """
    from app.services.escalation import escalation_service

    fallback_context = AlertContext(alert=alert)
    recipients = [settings.NOTIFICATION_EMAIL]
    await escalation_service.send_timeout_passthrough(fallback_context, recipients)
    history_store.store_timeout_passthrough(alert=alert, pipeline_stage=stage)
